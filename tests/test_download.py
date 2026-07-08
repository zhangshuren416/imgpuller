"""Tests for DownloadManager progress reporting and blob download flow."""
from __future__ import annotations

import hashlib
from unittest import mock

import pytest
from rich.progress import Progress

from imgpuller.config import Platform
from imgpuller.download.manager import DownloadManager
from imgpuller.manifest.resolver import (
    Descriptor,
    ImageManifest,
    ResolvedImage,
)


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _make_resolved(
    config_data: bytes, layers: list[tuple[str, bytes, int]]
) -> tuple[ResolvedImage, str, list[str]]:
    config_digest = _digest(config_data)
    layer_digests = [d for d, _, _ in layers]

    manifest = ImageManifest(
        schema_version=2,
        media_type="application/vnd.oci.image.manifest.v1+json",
        config=Descriptor(
            media_type="application/vnd.oci.image.config.v1+json",
            digest=config_digest,
            size=len(config_data),
        ),
        layers=[
            Descriptor(
                media_type="application/vnd.oci.image.layer.v1.tar+gzip",
                digest=d,
                size=s,
            )
            for d, _, s in layers
        ],
        raw_bytes=b"",
    )
    resolved = ResolvedImage(
        manifest=manifest,
        config_digest=config_digest,
        layer_digests=layer_digests,
        platform=Platform(os="linux", architecture="amd64"),
        annotations={},
    )
    return resolved, config_digest, layer_digests


def _make_mock_client(blob_data: dict[str, bytes]) -> mock.AsyncMock:
    """Return a mock RegistryClient that serves each digest's bytes."""

    async def fake_get_blob(name, digest, offset=0, chunk_callback=None):
        data = blob_data[digest]
        if offset > 0:
            data = data[offset:]
        yield data

    client = mock.AsyncMock()
    client.get_blob = fake_get_blob
    return client


def _layer_tasks(progress: Progress) -> list:
    """Per-blob tasks (config + layers), excluding the overall summary task."""
    return [
        t for t in progress.tasks
        if t.description and "sha256:" in t.description
        and "Downloading" not in t.description
    ]


class TestDownloadManagerProgress:
    @pytest.mark.asyncio
    async def test_progress_totals_match_layer_sizes(self, tmp_path):
        config_data = b'{"architecture":"amd64","os":"linux","rootfs":{}}'
        layer1_data = b"layer-one-content" * 100
        layer2_data = b"layer-two-content!!" * 50

        resolved, config_digest, layer_digests = _make_resolved(
            config_data,
            [
                (_digest(layer1_data), layer1_data, len(layer1_data)),
                (_digest(layer2_data), layer2_data, len(layer2_data)),
            ],
        )

        blob_data = {
            config_digest: config_data,
            layer_digests[0]: layer1_data,
            layer_digests[1]: layer2_data,
        }
        client = _make_mock_client(blob_data)

        mgr = DownloadManager(
            client=client,
            image_name="library/test",
            output_dir=tmp_path,
            concurrency=2,
            verify=True,
        )

        progress = Progress(transient=False)
        result = await mgr.download_all(resolved, progress=progress)

        assert result is True

        # Every blob landed in blobs/sha256/ with correct content.
        blobs_dir = tmp_path / "blobs" / "sha256"
        for digest, data in blob_data.items():
            hex_d = digest.split(":", 1)[1]
            assert (blobs_dir / hex_d).read_bytes() == data

        # Each per-blob progress task must carry its declared size as the
        # total (the bug being fixed: total used to be None / indeterminate).
        tasks = _layer_tasks(progress)
        assert len(tasks) == 3  # config + 2 layers

        expected_sizes = {
            len(config_data), len(layer1_data), len(layer2_data),
        }
        for task in tasks:
            assert task.total in expected_sizes, (
                f"task total {task.total!r} not in expected sizes "
                f"{expected_sizes}"
            )
            # Finished: completed either equals total or is flagged True.
            assert task.completed in (task.total, True)

    @pytest.mark.asyncio
    async def test_resume_sets_completed_offset(self, tmp_path):
        """A pre-existing .partial file should set the progress start offset."""
        layer_data = b"resumable-layer-data" * 200
        layer_digest = _digest(layer_data)
        config_data = b'{"rootfs":{}}'
        config_digest = _digest(config_data)

        resolved, config_digest, _ = _make_resolved(
            config_data,
            [(layer_digest, layer_data, len(layer_data))],
        )

        # Pretend we already downloaded the first 100 bytes of the layer:
        # write both the partial file and the matching resume state, since
        # the worker only resumes when state + partial file are both present.
        blobs_dir = tmp_path / "blobs" / "sha256"
        blobs_dir.mkdir(parents=True)
        layer_hex = layer_digest.split(":", 1)[1]
        partial_path = blobs_dir / f"{layer_hex}.partial"
        partial_path.write_bytes(layer_data[:100])

        from imgpuller.download.state import DownloadState
        state = DownloadState(tmp_path)
        state.save_blob_state(
            layer_digest,
            completed_bytes=100,
            temp_path=partial_path,
            expected_size=len(layer_data),
        )

        blob_data = {
            config_digest: config_data,
            layer_digest: layer_data,
        }
        captured_offset: dict[str, int] = {}

        async def fake_get_blob(name, digest, offset=0, chunk_callback=None):
            captured_offset[digest] = offset
            data = blob_data[digest]
            yield data[offset:]

        client = mock.AsyncMock()
        client.get_blob = fake_get_blob

        mgr = DownloadManager(
            client=client,
            image_name="library/test",
            output_dir=tmp_path,
            concurrency=1,
            verify=True,
        )

        progress = Progress(transient=False)
        result = await mgr.download_all(resolved, progress=progress)

        assert result is True

        # The layer worker resumed from byte 100; the config from 0.
        assert captured_offset[layer_digest] == 100
        assert captured_offset[config_digest] == 0

        # The layer's progress bar total equals the declared layer size.
        layer_task = next(
            t for t in _layer_tasks(progress)
            if layer_digest[:19] in (t.description or "")
        )
        assert layer_task.total == len(layer_data)

        # Final file is complete and correct.
        assert (blobs_dir / layer_hex).read_bytes() == layer_data
