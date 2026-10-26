"""Tests for PDF and image handling in get_drive_file_content."""

import base64
import io
from contextlib import ExitStack, contextmanager
from unittest.mock import Mock, patch

import pytest
from fastmcp.tools import ToolResult
from mcp.types import EmbeddedResource, ImageContent, ResourceLink

from tests.helpers import _make_minimal_pdf
from gdrive.drive_helpers import _download_file_bytes
from gdrive.drive_tools import get_drive_file_content


@pytest.fixture(autouse=True)
def _no_ambient_office_xml_limit(monkeypatch):
    """The expansion limit is read from the environment on every extraction."""
    monkeypatch.delenv("WORKSPACE_MCP_MAX_OFFICE_XML_BYTES", raising=False)


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


@pytest.fixture
def mock_resolve():
    with patch("gdrive.drive_tools.resolve_drive_item") as m:
        m.return_value = (
            "file123",
            {
                "name": "test_file",
                "mimeType": "application/pdf",
                "webViewLink": "https://drive.google.com/file/file123",
            },
        )
        yield m


@pytest.fixture
def mock_resolve_image():
    with patch("gdrive.drive_tools.resolve_drive_item") as m:
        m.return_value = (
            "img456",
            {
                "name": "photo.png",
                "mimeType": "image/png",
                "webViewLink": "https://drive.google.com/file/img456",
            },
        )
        yield m


class _FakeDownloader:
    def __init__(self, fh, data):
        fh.write(data)
        fh.seek(0)

    def next_chunk(self):
        return None, True


@contextmanager
def _patch_downloader(content_bytes):
    """Patch every MediaIoBaseDownload import to write content_bytes into the handle."""
    with ExitStack() as stack:
        for target in (
            "core.file_limits.MediaIoBaseDownload",
            "gdrive.drive_helpers.MediaIoBaseDownload",
            "gdrive.drive_tools.MediaIoBaseDownload",
        ):
            stack.enter_context(
                patch(
                    target,
                    side_effect=lambda fh, req, **_kwargs: _FakeDownloader(
                        fh, content_bytes
                    ),
                )
            )
        yield


@pytest.mark.asyncio
async def test_download_file_bytes_supports_shared_drives():
    mock_service = Mock()
    mock_service.files().get_media.return_value = "req"

    with _patch_downloader(b"content"):
        result = await _download_file_bytes(mock_service, "file123")

    assert result == b"content"
    mock_service.files.return_value.get_media.assert_called_once_with(
        fileId="file123", supportsAllDrives=True
    )


@pytest.mark.asyncio
async def test_download_file_bytes_leaves_export_request_unchanged():
    mock_service = Mock()
    mock_service.files().export_media.return_value = "req"

    with _patch_downloader(b"exported"):
        result = await _download_file_bytes(mock_service, "doc123", "text/plain")

    assert result == b"exported"
    mock_service.files.return_value.export_media.assert_called_once_with(
        fileId="doc123", mimeType="text/plain"
    )


# ---------------------------------------------------------------------------
# PDF tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_drive_file_content_pdf(mock_resolve):
    pdf_bytes = _make_minimal_pdf("Contract clause 1")
    mock_service = Mock()
    mock_service.files().get_media.return_value = "req"

    with _patch_downloader(pdf_bytes):
        result = await _unwrap(get_drive_file_content)(
            service=mock_service,
            user_google_email="user@example.com",
            file_id="file123",
        )

    assert "Contract clause 1" in result
    assert "--- CONTENT ---" in result


@pytest.mark.asyncio
async def test_get_drive_file_content_pdf_empty(mock_resolve):
    """Empty/scanned PDF falls back to guidance message."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    empty_pdf = buf.getvalue()

    mock_service = Mock()
    mock_service.files().get_media.return_value = "req"

    with (
        _patch_downloader(empty_pdf),
        patch("gdrive.drive_tools.is_stateless_mode", return_value=True),
    ):
        result = await _unwrap(get_drive_file_content)(
            service=mock_service,
            user_google_email="user@example.com",
            file_id="file123",
        )

    assert isinstance(result, ToolResult)
    text = next(item.text for item in result.content if item.type == "text")
    assert "may be scanned" in text
    assert any(isinstance(item, EmbeddedResource) for item in result.content)


@pytest.mark.asyncio
async def test_get_drive_file_content_reports_invalid_docx(mock_resolve):
    mime_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    mock_resolve.return_value[1]["mimeType"] = mime_type
    mock_service = Mock()
    mock_service.files().get_media.return_value = "req"

    with _patch_downloader(b"not a zip"):
        result = await _unwrap(get_drive_file_content)(
            service=mock_service,
            user_google_email="user@example.com",
            file_id="file123",
        )

    assert "appears damaged" in result
    assert "unsupported text encoding" not in result


@pytest.mark.asyncio
async def test_get_drive_file_content_reports_oversized_expansion(
    mock_resolve, monkeypatch
):
    """A file that expands past the limit is reported as that — not as damaged,
    and not handed on to the raw-bytes fallback."""
    import zipfile

    mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    mock_resolve.return_value[1]["mimeType"] = mime_type
    monkeypatch.setenv("WORKSPACE_MCP_MAX_OFFICE_XML_BYTES", "1000")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "<workbook>" + " " * 20_000 + "</workbook>")
    mock_service = Mock()
    mock_service.files().get_media.return_value = "req"

    with _patch_downloader(buf.getvalue()):
        result = await _unwrap(get_drive_file_content)(
            service=mock_service,
            user_google_email="user@example.com",
            file_id="file123",
        )

    assert "expands beyond the extraction limit" in result
    assert "WORKSPACE_MCP_MAX_OFFICE_XML_BYTES" in result
    assert "convert it to a native Google file" in result
    assert "appears damaged" not in result
    assert "unsupported text encoding" not in result


# ---------------------------------------------------------------------------
# Image tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime_type, magic",
    [
        ("image/png", b"\x89PNG\r\n\x1a\n"),
        ("image/jpeg", b"\xff\xd8\xff\xe0"),
        ("image/gif", b"GIF89a"),
        ("image/webp", b"RIFF\x00\x00\x00\x00WEBP"),
    ],
    ids=["png", "jpeg", "gif", "webp"],
)
async def test_get_drive_file_content_image(mock_resolve_image, mime_type, magic):
    mock_resolve_image.return_value[1]["mimeType"] = mime_type
    image_bytes = magic + bytes(range(256)) * 4
    mock_service = Mock()
    mock_service.files().get_media.return_value = "req"

    with (
        _patch_downloader(image_bytes),
        patch("gdrive.drive_tools.is_stateless_mode", return_value=True),
    ):
        result = await _unwrap(get_drive_file_content)(
            service=mock_service,
            user_google_email="user@example.com",
            file_id="img456",
        )

    assert isinstance(result, ToolResult)
    image = next(item for item in result.content if isinstance(item, ImageContent))
    assert image.mime_type == mime_type
    assert base64.b64decode(image.data) == image_bytes


@pytest.mark.asyncio
async def test_get_drive_file_content_caps_text():
    service = Mock()
    service.files().get_media.return_value = "req"
    metadata = {
        "name": "large.txt",
        "mimeType": "text/plain",
        "webViewLink": "https://drive.google.com/file/text123",
    }
    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item", return_value=("text123", metadata)
        ),
        _patch_downloader(b"x" * 60_000),
    ):
        result = await _unwrap(get_drive_file_content)(
            service=service,
            user_google_email="user@example.com",
            file_id="text123",
        )
    assert "Text truncated to 50000" in result
    body = result.split("--- CONTENT ---\n", 1)[1].split("\n\n[", 1)[0]
    assert body == "x" * 50_000


@pytest.mark.asyncio
async def test_office_above_extraction_limit_returns_file(monkeypatch):
    payload = b"PK\x03\x04" + b"x" * 20
    mime_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    metadata = {
        "name": "report.docx",
        "mimeType": mime_type,
        "webViewLink": "https://drive.google.com/file/doc123",
    }
    service = Mock()
    service.files().get_media.return_value = "req"
    monkeypatch.setenv("WORKSPACE_MCP_EXTRACT_MAX_BYTES", "4")
    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item", return_value=("doc123", metadata)
        ),
        patch("gdrive.drive_tools.is_stateless_mode", return_value=True),
        _patch_downloader(payload),
    ):
        result = await _unwrap(get_drive_file_content)(
            service=service,
            user_google_email="user@example.com",
            file_id="doc123",
        )
    text = next(item.text for item in result.content if item.type == "text")
    assert "exceeds the 4-byte extraction limit" in text
    assert any(isinstance(item, EmbeddedResource) for item in result.content)


@pytest.mark.asyncio
async def test_large_image_uses_drive_link(monkeypatch, mock_resolve_image):
    monkeypatch.setenv("WORKSPACE_MCP_INLINE_FILE_MAX_BYTES", "4")
    service = Mock()
    service.files().get_media.return_value = "req"
    with (
        patch("gdrive.drive_tools.is_stateless_mode", return_value=True),
        _patch_downloader(b"large-image"),
    ):
        result = await _unwrap(get_drive_file_content)(
            service=service,
            user_google_email="user@example.com",
            file_id="img456",
        )
    assert any(isinstance(item, ResourceLink) for item in result.content)
    assert not any(isinstance(item, ImageContent) for item in result.content)


@pytest.mark.asyncio
async def test_get_drive_file_content_returns_svg_markup():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
    service = Mock()
    service.files().get_media.return_value = "req"
    metadata = {
        "name": "dot.svg",
        "mimeType": "image/svg+xml",
        "webViewLink": "https://drive.google.com/file/svg123",
    }
    with (
        patch(
            "gdrive.drive_tools.resolve_drive_item", return_value=("svg123", metadata)
        ),
        _patch_downloader(svg),
    ):
        result = await _unwrap(get_drive_file_content)(
            service=service,
            user_google_email="user@example.com",
            file_id="svg123",
        )
    assert isinstance(result, str)
    assert result.endswith(svg.decode())
