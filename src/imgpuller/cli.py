"""CLI entry point for imgpuller.

Commands:
    pull    - Pull an image from a registry and save as a docker-archive .tar
    verify  - Verify integrity of a docker-archive .tar
    inspect - Show manifest details without downloading
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
)

from imgpuller import __version__
from imgpuller.config import (
    Platform,
    detect_current_platform,
    get_credentials_for_registry,
    get_default_output_file,
    load_docker_config,
    parse_image_reference,
    resolve_registry_url,
)
from imgpuller.download.manager import DownloadManager
from imgpuller.exceptions import ImgpullerError
from imgpuller.manifest.resolver import ManifestResolver
from imgpuller.oci.docker_save import DockerSaveWriter
from imgpuller.registry.auth import create_auth_provider
from imgpuller.registry.client import RegistryClient

console = Console()


def setup_logging(verbose: int = 0) -> None:
    """Configure logging with rich handler.

    Args:
        verbose: 0=WARNING, 1=INFO, 2=DEBUG
    """
    levels = [logging.WARNING, logging.INFO, logging.DEBUG]
    level = levels[min(verbose, 2)]

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@click.group()
@click.version_option(version=__version__, prog_name="imgpuller")
@click.option(
    "-v", "--verbose", count=True,
    help="Increase verbosity (-v for INFO, -vv for DEBUG)",
)
def main(verbose: int = 0):
    """imgpuller - Pull OCI/Docker images via HTTP and save as a tar archive.

    Downloads image layers directly from OCI-compliant registries via the
    Docker Registry HTTP API V2. Supports resumable downloads, parallel
    layer fetching, and SHA256 integrity verification. Output is a single
    docker-archive .tar file that can be loaded with `docker load -i`.

    \b
    Examples:
      imgpuller pull ubuntu:22.04
      imgpuller pull --platform linux/arm64 nginx:alpine
      imgpuller pull -o ./my-image.tar ubuntu:22.04
      imgpuller pull --username user --password-stdin ghcr.io/org/app:v1
      imgpuller verify ./ubuntu-22.04.tar
    """
    setup_logging(verbose)


@main.command()
@click.argument("image", required=True)
@click.option(
    "--platform", default=None,
    help='Target platform (e.g. "linux/amd64", "linux/arm64/v8"). Default: current system.',
)
@click.option(
    "--output", "-o", default=None,
    help="Output .tar file path. Default: ./<image>-<tag>.tar",
    type=click.Path(path_type=Path),
)
@click.option(
    "--username", "-u", default=None,
    help="Registry username.",
)
@click.option(
    "--password-stdin", is_flag=True, default=False,
    help="Read password from stdin.",
)
@click.option(
    "--jobs", "-j", default=4, type=click.IntRange(1, 16),
    help="Parallel download jobs (1-16).",
)
@click.option(
    "--insecure", is_flag=True, default=False,
    help="Allow HTTP connections (not recommended).",
)
@click.option(
    "--no-verify", is_flag=True, default=False,
    help="Skip SHA256 verification after download.",
)
@click.option(
    "--no-resume", is_flag=True, default=False,
    help="Do not resume from previous partial downloads.",
)
@click.option(
    "--keep-blobs", is_flag=True, default=False,
    help="Keep the intermediate blob download directory after tarring.",
)
@click.option(
    "--registry", default=None,
    help="Explicit registry URL (overrides auto-detection).",
)
@click.option(
    "--proxy", default=None,
    help="HTTP proxy URL (e.g. http://proxy:8080). Also respects HTTP_PROXY env var.",
)
def pull(
    image: str,
    platform: str | None,
    output: Path | None,
    username: str | None,
    password_stdin: bool,
    jobs: int,
    insecure: bool,
    no_verify: bool,
    no_resume: bool,
    keep_blobs: bool,
    registry: str | None,
    proxy: str | None,
):
    """Pull an image from a registry and save as a docker-archive .tar.

    IMAGE is the image reference. Supported formats:

    \b
      ubuntu:22.04           Docker Hub official image
      nginx                   Docker Hub (defaults to :latest)
      library/ubuntu:22.04    Docker Hub with explicit namespace
      ghcr.io/org/app:v1     GitHub Container Registry
      registry.example.com:5000/myapp:v1   Custom registry with port
      ubuntu@sha256:abc...  By digest

    The output is a single .tar file (docker-archive format) that can be
    loaded directly with:

    \b
      docker load -i <output>.tar
      podman load -i <output>.tar
    """
    # Parse image reference
    try:
        if registry:
            # Use explicit registry
            image_ref = parse_image_reference(f"{registry}/{image}")
        else:
            image_ref = parse_image_reference(image)
    except Exception as e:
        console.print(f"[red]Error:[/] Invalid image reference: {e}")
        sys.exit(2)

    # Parse platform
    target_platform: Platform | None = None
    if platform:
        try:
            parts = platform.split("/")
            if len(parts) == 2:
                target_platform = Platform(os=parts[0], architecture=parts[1])
            elif len(parts) == 3:
                target_platform = Platform(
                    os=parts[0], architecture=parts[1], variant=parts[2]
                )
            else:
                console.print(f"[red]Error:[/] Invalid platform format: {platform}")
                console.print("  Expected: os/arch[/variant], e.g. linux/amd64")
                sys.exit(2)
        except Exception as e:
            console.print(f"[red]Error:[/] Invalid platform: {e}")
            sys.exit(2)

    # Output file (.tar) and intermediate blob work directory.
    output_file = output or Path(get_default_output_file(image_ref))
    # Work dir holds blobs/sha256/* and .imgpuller-state/ for resume support.
    work_dir = output_file.parent / (output_file.stem + ".blobs")

    # Get password
    password = None
    if password_stdin:
        password = sys.stdin.readline().strip()
        if not password:
            console.print("[yellow]Warning:[/] No password read from stdin")

    # Load credentials
    docker_config = load_docker_config()
    docker_creds = get_credentials_for_registry(image_ref.registry, docker_config)

    # Build auth provider
    auth_provider = create_auth_provider(
        registry=image_ref.registry,
        credentials=docker_creds,
        username=username,
        password=password,
    )

    # Resolve registry URL
    registry_url = resolve_registry_url(image_ref.registry, insecure=insecure)

    console.print(f"[bold]Pulling image:[/] {image_ref}")
    console.print(f"  Registry:  {registry_url}")
    console.print(f"  Platform:  {target_platform or detect_current_platform()}")
    console.print(f"  Output:    {output_file.absolute()}")
    console.print(f"  Work dir:  {work_dir.absolute()}")

    if target_platform:
        console.print(f"  Platform:  {target_platform}")

    # Main async workflow
    async def run():
        client = RegistryClient(
            registry_url=registry_url,
            auth_provider=auth_provider,
            insecure=insecure,
            proxy=proxy,
        )

        try:
            # Check API
            if not await client.check_api():
                console.print(
                    f"[red]Error:[/] Registry at {registry_url} does not support "
                    f"the V2 API"
                )
                sys.exit(3)

            # Resolve manifest
            resolver = ManifestResolver(client)

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task(
                    "Resolving manifest...", total=None
                )
                resolved = await resolver.resolve(
                    name=image_ref.name,
                    reference=image_ref.reference,
                    platform=target_platform,
                )
                progress.update(task, description="[green]✓[/] Manifest resolved")

            console.print(
                f"  Manifest: {resolved.manifest.media_type}"
            )
            console.print(
                f"  Layers:   {len(resolved.layer_digests)}"
            )

            # Download blobs into the work directory
            download_mgr = DownloadManager(
                client=client,
                image_name=image_ref.name,
                output_dir=work_dir,
                concurrency=jobs,
                verify=not no_verify,
            )

            if no_resume:
                download_mgr.cleanup_state()

            downloaded = await download_mgr.download_all(resolved)

            if not downloaded:
                console.print("[red]Download failed[/]")
                sys.exit(4)

            # Write docker-archive .tar from downloaded blobs
            writer = DockerSaveWriter(output_file)
            tar_path = writer.write(
                resolved,
                blobs_dir=work_dir / "blobs" / "sha256",
                image_ref=image_ref,
            )

            # Clean up state on success
            download_mgr.cleanup_state()

            # Remove intermediate blob directory unless asked to keep it.
            if not keep_blobs:
                import shutil

                shutil.rmtree(work_dir, ignore_errors=True)
            else:
                console.print(f"  Blobs:    {work_dir.absolute()} (kept)")

            console.print()
            console.print("[bold green]✓ Image pulled successfully[/]")
            console.print(f"  Location: {tar_path.absolute()}")
            console.print()
            console.print("[bold]Load with:[/]")
            console.print(
                f"  docker load -i {tar_path}"
            )

        finally:
            await client.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Interrupted. Resume state saved - run again to continue.[/]")
        sys.exit(130)
    except ImgpullerError as e:
        console.print(f"[red]Error:[/] {e}")
        sys.exit(e.exit_code)
    except Exception as e:
        console.print(f"[red]Unexpected error:[/] {e}")
        if logging.getLogger().level <= logging.DEBUG:
            console.print_exception()
        sys.exit(1)


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def verify(path: Path):
    """Verify the integrity of a docker-archive .tar file.

    Checks that the config and every layer.tar match the SHA256 diff IDs
    declared in the image config, and that layer chain IDs are consistent.

    \b
    PATH is the .tar file produced by `imgpuller pull`.
    """
    writer = DockerSaveWriter(path)
    result = writer.verify()

    if result["status"] == "ok":
        console.print(
            f"[green]✓ docker-archive is valid[/] "
            f"({result['valid']}/{result['checked']} entries verified)"
        )
    else:
        console.print(
            f"[red]✗ docker-archive has issues:[/] "
            f"({result['valid']}/{result['checked']} entries valid)"
        )
        for error in result["errors"]:
            console.print(f"  [red]- {error}[/]")
        sys.exit(1)


@main.command()
@click.argument("image", required=True)
@click.option(
    "--platform", default=None,
    help="Target platform.",
)
@click.option(
    "--username", "-u", default=None,
    help="Registry username.",
)
@click.option(
    "--password-stdin", is_flag=True, default=False,
    help="Read password from stdin.",
)
@click.option(
    "--insecure", is_flag=True, default=False,
    help="Allow HTTP connections.",
)
@click.option(
    "--registry", default=None,
    help="Explicit registry URL.",
)
@click.option(
    "--proxy", default=None,
    help="HTTP proxy URL (e.g. http://proxy:8080). Also respects HTTP_PROXY env var.",
)
def inspect(
    image: str,
    platform: str | None,
    username: str | None,
    password_stdin: bool,
    insecure: bool,
    registry: str | None,
    proxy: str | None,
):
    """Inspect an image manifest without downloading layers.

    Shows manifest type, layers, platform info, and sizes.
    """
    # Parse image reference
    try:
        if registry:
            image_ref = parse_image_reference(f"{registry}/{image}")
        else:
            image_ref = parse_image_reference(image)
    except Exception as e:
        console.print(f"[red]Error:[/] Invalid image reference: {e}")
        sys.exit(2)

    # Parse platform
    target_platform = None
    if platform:
        parts = platform.split("/")
        target_platform = Platform(
            os=parts[0],
            architecture=parts[1],
            variant=parts[2] if len(parts) > 2 else None,
        )

    # Auth
    password = None
    if password_stdin:
        password = sys.stdin.readline().strip()

    docker_config = load_docker_config()
    docker_creds = get_credentials_for_registry(image_ref.registry, docker_config)
    auth_provider = create_auth_provider(
        registry=image_ref.registry,
        credentials=docker_creds,
        username=username,
        password=password,
    )

    registry_url = resolve_registry_url(image_ref.registry, insecure=insecure)

    async def run():
        client = RegistryClient(
            registry_url=registry_url,
            auth_provider=auth_provider,
            insecure=insecure,
            proxy=proxy,
        )
        try:
            if not await client.check_api():
                console.print("[red]Error:[/] Registry does not support V2 API")
                sys.exit(3)

            resolver = ManifestResolver(client)
            resolved = await resolver.resolve(
                name=image_ref.name,
                reference=image_ref.reference,
                platform=target_platform,
            )

            # Display info
            console.print(f"[bold]Image:[/] {image_ref}")
            console.print(f"[bold]Media type:[/] {resolved.manifest.media_type}")
            console.print(f"[bold]Platform:[/] {resolved.platform}")
            console.print(f"[bold]Schema version:[/] {resolved.manifest.schema_version}")
            console.print()

            # Config
            config = resolved.manifest.config
            console.print("[bold]Config:[/]")
            console.print(f"  Digest: {config.digest}")
            console.print(f"  Size:   {_format_size(config.size)}")
            console.print(f"  Type:   {config.media_type}")
            console.print()

            # Layers
            console.print(f"[bold]Layers ({len(resolved.manifest.layers)}):[/]")
            total_size = 0
            for i, layer in enumerate(resolved.manifest.layers):
                console.print(f"  [{i}] {layer.digest[:19]}...")
                console.print(f"      Size: {_format_size(layer.size)}")
                console.print(f"      Type: {layer.media_type}")
                total_size += layer.size

            console.print()
            console.print(f"[bold]Total layer size:[/] {_format_size(total_size)}")
            console.print(f"[bold]Config + layers:[/] {_format_size(total_size + config.size)}")

        finally:
            await client.close()

    try:
        asyncio.run(run())
    except ImgpullerError as e:
        console.print(f"[red]Error:[/] {e}")
        sys.exit(e.exit_code)


def _format_size(size: int) -> str:
    """Format bytes as human-readable size."""
    if size == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    fsize = float(size)
    while fsize >= 1024 and i < len(units) - 1:
        fsize /= 1024
        i += 1
    if i == 0:
        return f"{size} B"
    return f"{fsize:.1f} {units[i]}"


if __name__ == "__main__":
    main()
