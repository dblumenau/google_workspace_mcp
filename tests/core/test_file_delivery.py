import base64
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from mcp.types import BlobResourceContents, EmbeddedResource, ImageContent, ResourceLink

import core.attachment_storage as attachment_storage
import core.file_delivery as file_delivery
from core.attachment_storage import AttachmentStorage
from core.staging_s3 import StagedFile
import core.staging_s3 as staging_s3


@pytest.fixture
def isolated_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(attachment_storage, "STORAGE_DIR", tmp_path / "attachments")
    storage = AttachmentStorage()
    monkeypatch.setattr(file_delivery, "get_attachment_storage", lambda: storage)
    monkeypatch.setattr(
        file_delivery,
        "get_attachment_url",
        lambda file_id: f"https://files.example/attachments/{file_id}",
    )
    return storage


@pytest.mark.asyncio
async def test_exact_inline_limit_embeds_exact_bytes(monkeypatch):
    monkeypatch.setenv(file_delivery.INLINE_FILE_MAX_BYTES_ENV, "4")
    result = await file_delivery.deliver_file_bytes(
        summary="ready",
        file_bytes=b"abcd",
        filename="test.bin",
        mime_type="application/octet-stream",
        stateless=True,
    )

    resource = next(
        item for item in result.content if isinstance(item, EmbeddedResource)
    )
    assert isinstance(resource.resource, BlobResourceContents)
    assert base64.b64decode(resource.resource.blob) == b"abcd"
    assert result.structured_content["size"] == 4


@pytest.mark.asyncio
async def test_threshold_plus_one_errors_in_stateless_mode(monkeypatch):
    monkeypatch.setenv(file_delivery.INLINE_FILE_MAX_BYTES_ENV, "4")
    with pytest.raises(ToolError, match="exceeds the inline limit"):
        await file_delivery.deliver_file_bytes(
            summary="ready",
            file_bytes=b"abcde",
            filename="test.bin",
            mime_type="application/octet-stream",
            stateless=True,
        )


@pytest.mark.asyncio
async def test_large_stateful_path_returns_link_without_reading(
    monkeypatch, tmp_path, isolated_storage
):
    monkeypatch.setenv(file_delivery.INLINE_FILE_MAX_BYTES_ENV, "4")
    source = tmp_path / "large.bin"
    source.write_bytes(b"abcde")
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: pytest.fail("large files must not be read into memory"),
    )

    result = await file_delivery.deliver_file_path(
        summary="ready",
        file_path=source,
        filename="original.bin",
        mime_type="application/octet-stream",
        stateless=False,
        transport="streamable-http",
    )

    assert any(isinstance(item, ResourceLink) for item in result.content)
    assert result.structured_content["workspace_file_id"]
    assert result.structured_content["download_url"].startswith(
        "https://files.example/"
    )
    assert not source.exists()


@pytest.mark.asyncio
async def test_small_image_uses_image_content(monkeypatch):
    monkeypatch.setenv(file_delivery.INLINE_FILE_MAX_BYTES_ENV, "100")
    result = await file_delivery.deliver_file_bytes(
        summary="image",
        file_bytes=b"png-bytes",
        filename="photo.png",
        mime_type="image/png",
        stateless=True,
        content_kind="image",
    )
    image = next(item for item in result.content if isinstance(item, ImageContent))
    assert base64.b64decode(image.data) == b"png-bytes"


def test_invalid_inline_limit(monkeypatch):
    monkeypatch.setenv(file_delivery.INLINE_FILE_MAX_BYTES_ENV, "-1")
    with pytest.raises(ValueError, match="non-negative integer"):
        file_delivery.get_inline_file_max_bytes()


@pytest.mark.asyncio
async def test_configured_staging_avoids_embedded_base64(monkeypatch):
    monkeypatch.setattr(file_delivery, "staging_is_configured", lambda: True)
    monkeypatch.setattr(
        file_delivery,
        "stage_bytes",
        lambda *_args: StagedFile(
            key="google-workspace/2026-08-31/id/report.pdf",
            download_url="https://staging.example/report.pdf?signature=short-lived",
            expires_in_seconds=3600,
        ),
    )

    result = await file_delivery.deliver_file_bytes(
        summary="ready",
        file_bytes=b"pdf bytes that must not enter the MCP response",
        filename="report.pdf",
        mime_type="application/pdf",
        stateless=False,
    )

    assert not any(isinstance(item, EmbeddedResource) for item in result.content)
    assert any(isinstance(item, ResourceLink) for item in result.content)
    assert result.structured_content["staged_key"].endswith("/report.pdf")
    assert result.structured_content["download_url"].startswith(
        "https://staging.example/"
    )


def test_partial_staging_configuration_fails_closed(monkeypatch):
    for name in (
        "WORKSPACE_STAGING_S3_ENDPOINT",
        "WORKSPACE_STAGING_S3_BUCKET",
        "WORKSPACE_STAGING_S3_ACCESS_KEY_ID",
        "WORKSPACE_STAGING_S3_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WORKSPACE_STAGING_S3_BUCKET", "mcp-staging")

    with pytest.raises(ValueError, match="Incomplete Workspace staging"):
        staging_s3.staging_is_configured()


@pytest.mark.asyncio
async def test_configured_staging_delivers_path_and_removes_temporary_file(
    tmp_path, monkeypatch
):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"streamed pdf bytes")
    monkeypatch.setattr(file_delivery, "staging_is_configured", lambda: True)
    monkeypatch.setattr(
        file_delivery,
        "stage_path",
        lambda *_args: StagedFile(
            key="google-workspace/2026-08-31/id/report.pdf",
            download_url="https://staging.example/report.pdf?signature=short-lived",
            expires_in_seconds=3600,
        ),
    )

    result = await file_delivery.deliver_file_path(
        summary="ready",
        file_path=source,
        filename="report.pdf",
        mime_type="application/pdf",
        stateless=True,
    )

    assert result.structured_content["staged_key"].endswith("/report.pdf")
    assert any(isinstance(item, ResourceLink) for item in result.content)
    assert not source.exists()
