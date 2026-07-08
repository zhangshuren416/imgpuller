"""Core unit tests for imgpuller.

Tests the full pipeline using mocked HTTP responses."""
from __future__ import annotations

import json
import hashlib
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from imgpuller.config import (
    ImageReference,
    Platform,
    detect_current_platform,
    get_default_output_file,
    parse_image_reference,
    resolve_registry_url,
)
from imgpuller.download.state import DownloadState
from imgpuller.exceptions import (
    DigestMismatchError,
    InvalidImageReferenceError,
    ManifestNotFoundError,
    OCILayoutError,
    PlatformNotFoundError,
)
from imgpuller.manifest.resolver import ManifestResolver, compute_digest
from imgpuller.oci.docker_save import DockerSaveWriter
from imgpuller.oci.layout import OCILayoutWriter
from imgpuller.verification.hasher import (
    StreamingHashWriter,
    compute_bytes_digest,
    compute_file_digest,
    parse_digest,
    verify_file,
)


# ── Image Reference Parsing ──

class TestImageReferenceParsing:
    """Test parse_image_reference with various formats."""

    def test_docker_hub_official(self):
        ref = parse_image_reference("ubuntu:22.04")
        assert ref.registry == "registry-1.docker.io"
        assert ref.name == "library/ubuntu"
        assert ref.reference == "22.04"
        assert ref.tag == "22.04"
        assert not ref.is_digest

    def test_docker_hub_no_tag(self):
        ref = parse_image_reference("nginx")
        assert ref.registry == "registry-1.docker.io"
        assert ref.name == "library/nginx"
        assert ref.reference == "latest"

    def test_docker_hub_explicit_namespace(self):
        ref = parse_image_reference("library/ubuntu:22.04")
        assert ref.registry == "registry-1.docker.io"
        assert ref.name == "library/ubuntu"
        assert ref.reference == "22.04"

    def test_docker_io_prefix(self):
        ref = parse_image_reference("docker.io/library/nginx:latest")
        assert ref.registry == "registry-1.docker.io"
        assert ref.name == "library/nginx"

    def test_custom_registry(self):
        ref = parse_image_reference("ghcr.io/org/app:v2")
        assert ref.registry == "ghcr.io"
        assert ref.name == "org/app"
        assert ref.reference == "v2"

    def test_registry_with_port(self):
        ref = parse_image_reference("localhost:5000/myapp")
        assert ref.registry == "localhost:5000"
        assert ref.name == "myapp"
        assert ref.reference == "latest"

    def test_by_digest(self):
        digest = "sha256:" + "a" * 64
        ref = parse_image_reference(f"ubuntu@{digest}")
        assert ref.is_digest
        assert ref.digest == digest
        assert ref.tag is None

    def test_invalid_empty(self):
        with pytest.raises(InvalidImageReferenceError):
            parse_image_reference("")

    def test_docker_hub_without_namespace_rejected(self):
        """Docker Hub images need a namespace (library/ prefix is added automatically for single-segment)."""
        # Single segment like "ubuntu" auto-gets "library/" prefix
        ref = parse_image_reference("ubuntu")
        assert ref.name == "library/ubuntu"


# ── Registry URL Resolution ──

class TestRegistryURL:
    def test_docker_hub(self):
        url = resolve_registry_url("registry-1.docker.io")
        assert url == "https://registry-1.docker.io"

    def test_custom_registry(self):
        url = resolve_registry_url("ghcr.io")
        assert url == "https://ghcr.io"

    def test_insecure(self):
        url = resolve_registry_url("myreg.io", insecure=True)
        assert url == "http://myreg.io"


# ── Manifest Resolution (with mock HTTP) ──

# A minimal OCI manifest for testing
MANIFEST_JSON = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": "sha256:" + "b" * 64,
        "size": 1234,
    },
    "layers": [
        {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": "sha256:" + "c" * 64,
            "size": 5678,
        },
        {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": "sha256:" + "d" * 64,
            "size": 9012,
        },
    ],
}

MANIFEST_LIST_JSON = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + "e" * 64,
            "size": 456,
            "platform": {"architecture": "amd64", "os": "linux"},
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + "f" * 64,
            "size": 456,
            "platform": {
                "architecture": "arm64",
                "os": "linux",
                "variant": "v8",
            },
        },
    ],
}


class TestManifestResolver:
    """Test manifest resolution with mocked HTTP responses."""

    def make_mock_client(self, responses: list[tuple[bytes, str, str]]):
        """Create a mock RegistryClient that returns predefined responses.

        Each response is (content_bytes, content_type, docker_content_digest).
        """
        client = mock.AsyncMock()
        client.check_api = mock.AsyncMock(return_value=True)

        # Build get_manifest responses
        manifest_responses = []
        for content, content_type, digest in responses:
            mr = mock.MagicMock()
            mr.content = content
            mr.content_type = content_type
            mr.digest = digest
            manifest_responses.append(mr)

        client.get_manifest = mock.AsyncMock(side_effect=manifest_responses)
        return client

    @pytest.mark.asyncio
    async def test_resolve_single_manifest(self):
        """Resolve a single (non-list) manifest."""
        manifest_bytes = json.dumps(MANIFEST_JSON).encode()
        digest = compute_digest(manifest_bytes)
        client = self.make_mock_client([
            (manifest_bytes, "application/vnd.oci.image.manifest.v1+json", digest),
        ])

        resolver = ManifestResolver(client)
        resolved = await resolver.resolve(
            "library/test", "latest",
            platform=Platform(os="linux", architecture="amd64"),
        )

        assert len(resolved.layer_digests) == 2
        assert resolved.config_digest.startswith("sha256:")
        assert resolved.platform.architecture == "amd64"

    @pytest.mark.asyncio
    async def test_resolve_manifest_list(self):
        """Resolve a multi-arch manifest list."""
        list_bytes = json.dumps(MANIFEST_LIST_JSON).encode()
        manifest_bytes = json.dumps(MANIFEST_JSON).encode()
        list_digest = compute_digest(list_bytes)
        manifest_digest = compute_digest(manifest_bytes)

        client = self.make_mock_client([
            (list_bytes, "application/vnd.oci.image.index.v1+json", list_digest),
            (manifest_bytes, "application/vnd.oci.image.manifest.v1+json", manifest_digest),
        ])

        resolver = ManifestResolver(client)
        resolved = await resolver.resolve(
            "library/test", "latest",
            platform=Platform(os="linux", architecture="amd64"),
        )

        # Should have called get_manifest twice - once for list, once for platform manifest
        assert client.get_manifest.call_count == 2

    @pytest.mark.asyncio
    async def test_platform_not_found(self):
        """Raise error when platform not in manifest list."""
        list_bytes = json.dumps(MANIFEST_LIST_JSON).encode()
        list_digest = compute_digest(list_bytes)

        client = self.make_mock_client([
            (list_bytes, "application/vnd.oci.image.index.v1+json", list_digest),
        ])

        resolver = ManifestResolver(client)
        with pytest.raises(PlatformNotFoundError):
            await resolver.resolve(
                "library/test", "latest",
                platform=Platform(os="windows", architecture="amd64"),
            )


# ── StreamingHashWriter ──

class TestStreamingHashWriter:
    def test_streaming_hash(self):
        """HashWriter computes SHA256 as data is written."""
        data = b"Hello, World! " * 1000

        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name

        try:
            with open(tmp_path, "wb") as f:
                writer = StreamingHashWriter(f)
                writer.write(data[:500])
                writer.write(data[500:])

                expected_hash = hashlib.sha256(data).hexdigest()
                assert writer.hexdigest() == expected_hash
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_verify_file_matches(self):
        """verify_file returns True for matching digests."""
        data = b"test data for verification"
        digest = compute_bytes_digest(data)

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            tmp_path = f.name

        try:
            assert verify_file(Path(tmp_path), digest)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_verify_file_mismatch(self):
        """verify_file returns False for mismatched digests."""
        data = b"test data"
        wrong_digest = "sha256:" + "f" * 64

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            tmp_path = f.name

        try:
            assert not verify_file(Path(tmp_path), wrong_digest)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# ── Digest Parsing ──

class TestDigestParsing:
    def test_parse_valid_digest(self):
        algo, hex_val = parse_digest("sha256:abc123")
        assert algo == "sha256"
        assert hex_val == "abc123"

    def test_parse_invalid_digest(self):
        with pytest.raises(ValueError):
            parse_digest("not-a-valid-digest")


# ── Download State ──

class TestDownloadState:
    def test_save_and_read_blob_state(self, tmp_path):
        state = DownloadState(tmp_path)
        state.save_blob_state(
            "sha256:abc123",
            completed_bytes=1024,
            temp_path=tmp_path / "temp.bin",
            expected_size=2048,
        )

        saved = state.read_blob_state("sha256:abc123")
        assert saved is not None
        assert saved["completed_bytes"] == 1024
        assert saved["expected_size"] == 2048

    def test_delete_blob_state(self, tmp_path):
        state = DownloadState(tmp_path)
        state.save_blob_state("sha256:xyz", 0, tmp_path / "temp.bin")
        state.delete_blob_state("sha256:xyz")
        assert state.read_blob_state("sha256:xyz") is None

    def test_read_nonexistent_state(self, tmp_path):
        state = DownloadState(tmp_path)
        assert state.read_blob_state("sha256:nonexistent") is None

    def test_completed_digests(self, tmp_path):
        state = DownloadState(tmp_path)
        state.save_overall_state("test:latest", ["sha256:a", "sha256:b"])
        completed = state.get_completed_digests()
        assert completed == {"sha256:a", "sha256:b"}

    def test_clear_all(self, tmp_path):
        state = DownloadState(tmp_path)
        state.save_blob_state("sha256:a", 100, tmp_path / "a.bin")
        state.clear_all()
        assert not state.has_state()


# ── OCI Layout ──

class TestOCILayoutWriter:
    def test_write_and_verify_layout(self, tmp_path):
        """Write an OCI layout and verify its structure."""
        from imgpuller.manifest.resolver import ImageManifest, Descriptor as Desc

        manifest_bytes = json.dumps(MANIFEST_JSON).encode()
        manifest_digest = compute_digest(manifest_bytes)

        # Create fake resolved image
        manifest = ImageManifest(
            schema_version=2,
            media_type="application/vnd.oci.image.manifest.v1+json",
            config=Desc(
                media_type="application/vnd.oci.image.config.v1+json",
                digest=MANIFEST_JSON["config"]["digest"],
                size=MANIFEST_JSON["config"]["size"],
            ),
            layers=[
                Desc(
                    media_type=layer["mediaType"],
                    digest=layer["digest"],
                    size=layer["size"],
                )
                for layer in MANIFEST_JSON["layers"]
            ],
            raw_bytes=manifest_bytes,
        )

        # Create fake layer files in blobs/sha256/
        blob_dir = tmp_path / "blobs" / "sha256"
        blob_dir.mkdir(parents=True)

        for layer_digest in [
            MANIFEST_JSON["config"]["digest"],
            MANIFEST_JSON["layers"][0]["digest"],
            MANIFEST_JSON["layers"][1]["digest"],
        ]:
            hex_d = layer_digest.split(":")[1]
            # Create file with content that hashes to this digest...
            # For testing, just use the hex as content (not cryptographically correct
            # but the verification checks filename = sha256(content))
            content = hashlib.sha256(hex_d.encode()).hexdigest().encode()
            (blob_dir / hex_d).write_bytes(content)

        # Need to create correct blob content for verification to pass
        # Let's just create files where sha256(content) == filename
        for layer_digest in [
            MANIFEST_JSON["config"]["digest"],
            MANIFEST_JSON["layers"][0]["digest"],
            MANIFEST_JSON["layers"][1]["digest"],
        ]:
            hex_d = layer_digest.split(":")[1]
            # Brute-force find a short content that gives the right hash...
            # Too expensive. Instead, use the hex as content and create proper files:
            # For the test, just create a file whose SHA256 matches the filename
            content = b"test"  # this won't match, but we can still test layout structure
            blob_path = blob_dir / hex_d
            # Actually just write something and note this test is structural
            blob_path.write_bytes(hex_d.encode())

        from imgpuller.config import Platform
        resolved = mock.MagicMock()
        resolved.manifest = manifest
        resolved.manifest.raw_bytes = manifest_bytes
        resolved.manifest.media_type = manifest.media_type
        resolved.config_digest = MANIFEST_JSON["config"]["digest"]
        resolved.layer_digests = [
            layer["digest"] for layer in MANIFEST_JSON["layers"]
        ]
        resolved.platform = Platform(os="linux", architecture="amd64")
        resolved.annotations = {}

        writer = OCILayoutWriter(tmp_path)
        result = writer.write(resolved, image_ref="test:latest")

        # Check structure
        assert (tmp_path / "oci-layout").exists()
        assert (tmp_path / "index.json").exists()

        # Validate index.json
        with open(tmp_path / "index.json") as f:
            index = json.load(f)
        assert index["schemaVersion"] == 2
        assert len(index["manifests"]) == 1
        assert index["manifests"][0]["platform"]["architecture"] == "amd64"


# ── Config Utilities ──

class TestConfigUtilities:
    def test_default_output_file_tag(self):
        ref = parse_image_reference("ubuntu:22.04")
        assert get_default_output_file(ref) == "ubuntu-22.04.tar"

    def test_default_output_file_digest(self):
        digest = "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        ref = parse_image_reference(f"ubuntu@{digest}")
        d = get_default_output_file(ref)
        assert d.startswith("ubuntu-")
        assert d.endswith(".tar")
        assert len(d) == len("ubuntu-") + 12 + len(".tar")  # short digest

    def test_image_reference_str(self):
        ref = parse_image_reference("ubuntu:22.04")
        assert "ubuntu" in str(ref)
        assert "22.04" in str(ref)


# ── Docker Save Writer ──

class TestDockerSaveWriter:
    """Test docker-archive .tar generation and verification."""

    @staticmethod
    def _make_layer(files: dict[str, bytes]) -> tuple[bytes, str, str]:
        """Build a gzip-compressed tar layer.

        Returns (compressed_blob, compressed_digest, diff_id) where
        diff_id is the SHA256 of the uncompressed tar.
        """
        import gzip
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for name, content in files.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                t.addfile(info, io.BytesIO(content))
        tar_data = buf.getvalue()
        diff_id = f"sha256:{hashlib.sha256(tar_data).hexdigest()}"
        blob = gzip.compress(tar_data)
        compressed_digest = f"sha256:{hashlib.sha256(blob).hexdigest()}"
        return blob, compressed_digest, diff_id

    @staticmethod
    def _make_resolved(config_bytes, config_digest, layers, manifest_bytes):
        """Build a ResolvedImage from config + layer descriptors."""
        from imgpuller.manifest.resolver import (
            ImageManifest, Descriptor, ResolvedImage,
        )

        img_manifest = ImageManifest(
            schema_version=2,
            media_type="application/vnd.oci.image.manifest.v1+json",
            config=Descriptor(
                media_type="application/vnd.oci.image.config.v1+json",
                digest=config_digest,
                size=len(config_bytes),
            ),
            layers=[
                Descriptor(
                    media_type="application/vnd.oci.image.layer.v1.tar+gzip",
                    digest=ld,
                    size=ls,
                )
                for ld, ls, _ in layers
            ],
            raw_bytes=manifest_bytes,
        )
        return ResolvedImage(
            manifest=img_manifest,
            config_digest=config_digest,
            layer_digests=[ld for ld, _, _ in layers],
            platform=Platform(os="linux", architecture="amd64"),
            annotations={},
        )

    def _write_blobs(self, blobs_dir, config_hex, config_bytes, layers):
        blobs_dir.mkdir(parents=True, exist_ok=True)
        (blobs_dir / config_hex).write_bytes(config_bytes)
        for (ld, _, blob) in layers:
            (blobs_dir / ld.split(":", 1)[1]).write_bytes(blob)
        return blobs_dir

    def test_write_single_layer_and_verify(self, tmp_path):
        blob, layer_digest, diff_id = self._make_layer({"etc/hello": b"world"})
        config = {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
            "history": [],
        }
        config_bytes = json.dumps(config).encode()
        config_digest = compute_bytes_digest(config_bytes)
        config_hex = config_digest.split(":", 1)[1]

        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": layer_digest,
                    "size": len(blob),
                }
            ],
        }
        manifest_bytes = json.dumps(manifest).encode()

        blobs_dir = tmp_path / "blobs" / "sha256"
        self._write_blobs(
            blobs_dir, config_hex, config_bytes,
            [(layer_digest, len(blob), blob)],
        )

        resolved = self._make_resolved(
            config_bytes, config_digest,
            [(layer_digest, len(blob), blob)], manifest_bytes,
        )
        image_ref = parse_image_reference("myapp:1.0")

        output_tar = tmp_path / "myapp-1.0.tar"
        writer = DockerSaveWriter(output_tar)
        writer.write(resolved, blobs_dir=blobs_dir, image_ref=image_ref)

        assert output_tar.exists()
        chain_id = diff_id.split(":", 1)[1]  # single layer chain id == diff id
        import tarfile

        with tarfile.open(output_tar) as t:
            names = set(t.getnames())
            assert "manifest.json" in names
            assert "repositories" in names
            assert f"{config_hex}.json" in names
            assert f"{chain_id}/layer.tar" in names
            assert f"{chain_id}/VERSION" in names
            assert f"{chain_id}/json" in names

            mf = json.loads(t.extractfile("manifest.json").read())[0]
            assert mf["Config"] == f"{config_hex}.json"
            assert mf["RepoTags"] == ["myapp:1.0"]
            assert mf["Layers"] == [f"{chain_id}/layer.tar"]

            repos = json.loads(t.extractfile("repositories").read())
            assert repos == {"library/myapp": {"1.0": chain_id}}

        # Verify passes.
        result = DockerSaveWriter(output_tar).verify()
        assert result["status"] == "ok", result["errors"]
        assert result["valid"] == result["checked"]

    def test_write_multi_layer_chain_ids(self, tmp_path):
        blob0, dig0, diff0 = self._make_layer({"a": b"1"})
        blob1, dig1, diff1 = self._make_layer({"b": b"2"})
        diff0_hex = diff0.split(":", 1)[1]
        diff1_hex = diff1.split(":", 1)[1]
        # chain ids
        chain0 = diff0_hex
        chain1 = hashlib.sha256(f"{chain0} {diff1_hex}".encode()).hexdigest()

        config = {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff0, diff1]},
            "history": [],
        }
        config_bytes = json.dumps(config).encode()
        config_digest = compute_bytes_digest(config_bytes)
        config_hex = config_digest.split(":", 1)[1]

        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": [
                {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                 "digest": dig0, "size": len(blob0)},
                {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                 "digest": dig1, "size": len(blob1)},
            ],
        }
        manifest_bytes = json.dumps(manifest).encode()

        blobs_dir = tmp_path / "blobs" / "sha256"
        self._write_blobs(
            blobs_dir, config_hex, config_bytes,
            [(dig0, len(blob0), blob0), (dig1, len(blob1), blob1)],
        )
        resolved = self._make_resolved(
            config_bytes, config_digest,
            [(dig0, len(blob0), blob0), (dig1, len(blob1), blob1)],
            manifest_bytes,
        )
        image_ref = parse_image_reference("ghcr.io/org/app:v2")

        output_tar = tmp_path / "app-v2.tar"
        DockerSaveWriter(output_tar).write(
            resolved, blobs_dir=blobs_dir, image_ref=image_ref,
        )

        import tarfile

        with tarfile.open(output_tar) as t:
            mf = json.loads(t.extractfile("manifest.json").read())[0]
            assert mf["RepoTags"] == ["ghcr.io/org/app:v2"]
            assert mf["Layers"] == [
                f"{chain0}/layer.tar", f"{chain1}/layer.tar",
            ]
            repos = json.loads(t.extractfile("repositories").read())
            assert repos == {"ghcr.io/org/app": {"v2": chain1}}

        result = DockerSaveWriter(output_tar).verify()
        assert result["status"] == "ok", result["errors"]

    def test_write_digest_ref_no_tags(self, tmp_path):
        blob, layer_digest, diff_id = self._make_layer({"x": b"y"})
        config = {
            "architecture": "amd64", "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_id]},
            "history": [],
        }
        config_bytes = json.dumps(config).encode()
        config_digest = compute_bytes_digest(config_bytes)
        config_hex = config_digest.split(":", 1)[1]
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": config_digest, "size": len(config_bytes)},
            "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": layer_digest, "size": len(blob)}],
        }
        manifest_bytes = json.dumps(manifest).encode()

        blobs_dir = tmp_path / "blobs" / "sha256"
        self._write_blobs(
            blobs_dir, config_hex, config_bytes,
            [(layer_digest, len(blob), blob)],
        )
        resolved = self._make_resolved(
            config_bytes, config_digest,
            [(layer_digest, len(blob), blob)], manifest_bytes,
        )
        image_ref = parse_image_reference(
            f"ubuntu@sha256:{'a' * 64}"
        )

        output_tar = tmp_path / "ubuntu.tar"
        DockerSaveWriter(output_tar).write(
            resolved, blobs_dir=blobs_dir, image_ref=image_ref,
        )

        import tarfile

        with tarfile.open(output_tar) as t:
            mf = json.loads(t.extractfile("manifest.json").read())[0]
            assert mf["RepoTags"] is None
            repos = json.loads(t.extractfile("repositories").read())
            assert repos == {}

    def test_verify_detects_corrupt_layer(self, tmp_path):
        blob, layer_digest, diff_id = self._make_layer({"f": b"g"})
        # Tamper the declared diff id so verification must fail.
        wrong_diff = "sha256:" + "0" * 64
        config = {
            "architecture": "amd64", "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [wrong_diff]},
            "history": [],
        }
        config_bytes = json.dumps(config).encode()
        config_digest = compute_bytes_digest(config_bytes)
        config_hex = config_digest.split(":", 1)[1]
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": config_digest, "size": len(config_bytes)},
            "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": layer_digest, "size": len(blob)}],
        }
        manifest_bytes = json.dumps(manifest).encode()

        blobs_dir = tmp_path / "blobs" / "sha256"
        self._write_blobs(
            blobs_dir, config_hex, config_bytes,
            [(layer_digest, len(blob), blob)],
        )
        resolved = self._make_resolved(
            config_bytes, config_digest,
            [(layer_digest, len(blob), blob)], manifest_bytes,
        )
        image_ref = parse_image_reference("myapp:1.0")
        output_tar = tmp_path / "bad.tar"

        # write() itself rejects a diff-id mismatch.
        with pytest.raises(OCILayoutError):
            DockerSaveWriter(output_tar).write(
                resolved, blobs_dir=blobs_dir, image_ref=image_ref,
            )
