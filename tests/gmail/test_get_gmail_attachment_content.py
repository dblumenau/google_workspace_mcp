"""
Tests for ``get_gmail_attachment_content``, in particular the ``return_base64``
option added for sandboxed clients that cannot reach the MCP server's
localhost download URLs or local file paths.
"""

import base64
import io
import zipfile
from typing import Any, Callable
from unittest.mock import Mock

import pytest

from core.server import server
from core.tool_registry import get_tool_components
from gmail.gmail_tools import (
    EXTRACTED_TEXT_CHAR_LIMIT,
    _format_base64_content_block,
    _format_extracted_text_block,
    get_gmail_attachment_content,
)


def _build_docx_bytes(text: str) -> bytes:
    """Build a minimal but valid .docx containing ``text``."""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _unwrap(tool: Any) -> Callable[..., Any]:
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _build_mock_service(
    payload: bytes,
    *,
    filename: str = "attachment.bin",
    mime_type: str = "application/octet-stream",
) -> Mock:
    """Build a Mock google-api service returning ``payload`` as an attachment."""
    urlsafe_b64 = base64.urlsafe_b64encode(payload).decode("ascii")

    mock_service = Mock()

    # attachments().get(...).execute() returns the raw attachment dict
    mock_service.users().messages().attachments().get().execute.return_value = {
        "size": len(payload),
        "data": urlsafe_b64,
    }

    # messages().get(...).execute() is called to resolve filename/mime;
    # return a payload with a single matching part.
    mock_service.users().messages().get().execute.return_value = {
        "payload": {
            "parts": [
                {
                    "filename": filename,
                    "mimeType": mime_type,
                    "body": {"attachmentId": "att-123", "size": len(payload)},
                }
            ],
        },
    }

    return mock_service


@pytest.fixture
def isolated_attachment_env(tmp_path, monkeypatch):
    """Route attachment storage to a temp dir and force HTTP (not stateless) mode."""
    import core.attachment_storage as storage_module
    import auth.oauth_config as oauth_config_module
    import core.config as core_config_module

    monkeypatch.setattr(storage_module, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(oauth_config_module, "is_stateless_mode", lambda: False)
    monkeypatch.setattr(core_config_module, "get_transport_mode", lambda: "http")

    # Reset the cached module-level storage singleton so our patched
    # STORAGE_DIR actually takes effect.
    monkeypatch.setattr(storage_module, "_attachment_storage", None, raising=False)

    return tmp_path


def test_get_gmail_attachment_content_schema_includes_return_base64():
    """Published MCP schema should expose the public return_base64 parameter."""
    components = get_tool_components(server)
    schema = components[get_gmail_attachment_content.__name__].parameters["properties"]

    assert "return_base64" in schema
    assert schema["return_base64"]["type"] == "boolean"
    assert schema["return_base64"]["default"] is False


def test_format_base64_content_block_converts_urlsafe_to_standard():
    """Helper should convert URL-safe base64 (Gmail API) to standard base64."""
    # Payload whose base64 produces characters that differ between alphabets
    # (the '+' vs '-' and '/' vs '_' substitutions kick in for certain bytes).
    payload = bytes(range(256))
    urlsafe_b64 = base64.urlsafe_b64encode(payload).decode("ascii")

    lines = _format_base64_content_block(urlsafe_b64)

    assert len(lines) == 2
    assert "Base64 content" in lines[0]
    assert "standard base64" in lines[0]

    standard_b64 = lines[1]
    # Standard alphabet must round-trip back to the original bytes.
    assert base64.b64decode(standard_b64) == payload


def test_format_base64_content_block_handles_invalid_input_gracefully():
    """Invalid base64 shouldn't raise — it should return a warning line."""
    lines = _format_base64_content_block("not valid base64 !!!")

    assert len(lines) == 1
    assert "Could not include base64 content" in lines[0]


@pytest.mark.asyncio
async def test_default_call_omits_base64_content(isolated_attachment_env):
    """Without return_base64, the response should not contain the base64 block (backwards compat)."""
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    mock_service = _build_mock_service(
        payload, filename="test.png", mime_type="image/png"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert "Attachment downloaded successfully!" in result
    assert "📦 Base64 content" not in result
    assert "standard base64" not in result


@pytest.mark.asyncio
async def test_download_response_reports_sanitized_saved_filename(
    isolated_attachment_env,
):
    """Windows-reserved filename characters should be sanitized before saving."""
    payload = b"attached email bytes"
    mock_service = _build_mock_service(
        payload, filename="RE: Foo?.eml", mime_type="message/rfc822"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert "Filename: RE: Foo?.eml" in result
    assert "Saved filename: RE_ Foo_" in result

    saved_files = list(isolated_attachment_env.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].name.startswith("RE_ Foo_")
    assert ":" not in saved_files[0].name
    assert "?" not in saved_files[0].name
    assert saved_files[0].read_bytes() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "filename", "mime_type"),
    [
        (
            b"\x89PNG\r\n\x1a\n" + b"\xff" * 50 + bytes(range(200)),
            "test.png",
            "image/png",
        ),
        (b"PDF-ish\x00\x01\x02" + b"\xfe\xfd" * 128, "doc.pdf", "application/pdf"),
        (bytes(range(256)), "full-range.bin", "application/octet-stream"),
    ],
)
async def test_return_base64_true_includes_standard_base64_block(
    isolated_attachment_env,
    payload: bytes,
    filename: str,
    mime_type: str,
):
    """With return_base64=True, the response must contain decoded standard base64."""
    mock_service = _build_mock_service(payload, filename=filename, mime_type=mime_type)

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
        return_base64=True,
    )

    assert "📦 Base64 content" in result
    assert "standard base64" in result

    # Extract the base64 line (the one right after the header) and verify round-trip.
    lines = result.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines) if "📦 Base64 content" in line), None
    )
    assert header_idx is not None, (
        "Expected _format_base64_content_block to include the '📦 Base64 content' header"
    )
    standard_b64 = lines[header_idx + 1].strip()

    assert base64.b64decode(standard_b64) == payload


@pytest.mark.asyncio
async def test_return_base64_preserves_file_save_behavior(isolated_attachment_env):
    """return_base64 should be additive: file is still saved and path/URL still returned."""
    payload = b"additive behavior check " + bytes(range(100))
    mock_service = _build_mock_service(payload, filename="doc.bin")

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
        return_base64=True,
    )

    # Still returns the normal HTTP-mode output...
    assert "Attachment downloaded successfully!" in result
    assert "Download URL" in result
    # ...and includes the base64 block.
    assert "📦 Base64 content" in result


@pytest.mark.asyncio
async def test_docx_attachment_includes_extracted_text(isolated_attachment_env):
    """A .docx attachment should get its text extracted server-side by default."""
    payload = _build_docx_bytes("Sveiki, this is the archive certificate")
    mock_service = _build_mock_service(
        payload,
        filename="certificate.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert "--- EXTRACTED TEXT ---" in result
    assert "Sveiki, this is the archive certificate" in result


@pytest.mark.asyncio
async def test_docx_extraction_sniffs_zip_when_mime_is_generic(
    isolated_attachment_env,
):
    """Extraction must survive missing/generic MIME metadata via zip sniffing."""
    payload = _build_docx_bytes("Sniffed content")
    mock_service = _build_mock_service(
        payload, filename="unknown.bin", mime_type="application/octet-stream"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert "--- EXTRACTED TEXT ---" in result
    assert "Sniffed content" in result


@pytest.mark.asyncio
async def test_pdf_attachment_routes_to_pdf_extractor(
    isolated_attachment_env, monkeypatch
):
    """%PDF payloads should be routed to the PDF text extractor."""
    import gmail.gmail_tools as gmail_tools_module

    monkeypatch.setattr(
        gmail_tools_module, "extract_pdf_text", lambda _b: "Extracted PDF text"
    )
    payload = b"%PDF-1.4 fake pdf bytes \x00\x01"
    mock_service = _build_mock_service(
        payload, filename="doc.pdf", mime_type="application/pdf"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert "--- EXTRACTED TEXT ---" in result
    assert "Extracted PDF text" in result


@pytest.mark.asyncio
async def test_utf8_text_attachment_is_included_inline(isolated_attachment_env):
    """Plain UTF-8 attachments (txt/csv/json) should be readable inline."""
    payload = "name,age\nDavid,42\n".encode("utf-8")
    mock_service = _build_mock_service(
        payload, filename="data.csv", mime_type="text/csv"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert "--- EXTRACTED TEXT ---" in result
    assert "name,age" in result


@pytest.mark.asyncio
async def test_binary_attachment_has_no_extracted_text_block(
    isolated_attachment_env,
):
    """Non-text binary (e.g. PNG) must not produce an extracted-text block."""
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    mock_service = _build_mock_service(
        payload, filename="image.png", mime_type="image/png"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert "--- EXTRACTED TEXT ---" not in result


def test_format_extracted_text_block_truncates_long_text():
    """Huge documents must be capped so they can't flood the client context."""
    text = "x" * (EXTRACTED_TEXT_CHAR_LIMIT + 500)

    lines = _format_extracted_text_block(text)

    assert lines[0] == "\n--- EXTRACTED TEXT ---"
    assert len(lines[1]) == EXTRACTED_TEXT_CHAR_LIMIT
    assert "truncated" in lines[2]
