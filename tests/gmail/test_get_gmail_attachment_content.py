"""Tests for portable Gmail attachment delivery and text extraction."""

import base64
import io
import zipfile
from typing import Any, Callable
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import BlobResourceContents, EmbeddedResource, ResourceLink, TextContent

from core.server import server
from core.tool_registry import get_tool_components
from gmail.gmail_tools import (
    EXTRACTED_TEXT_CHAR_LIMIT,
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


def _result_text(result: ToolResult | str) -> str:
    """Read the human-facing text block from a mixed MCP tool result."""
    if isinstance(result, str):
        return result
    return "\n".join(
        block.text for block in result.content if isinstance(block, TextContent)
    )


def _result_resource(result: ToolResult) -> EmbeddedResource:
    """Return the single embedded attachment resource from a tool result."""
    resources = [
        block for block in result.content if isinstance(block, EmbeddedResource)
    ]
    assert len(resources) == 1
    return resources[0]


def _build_mock_service(
    payload: bytes,
    *,
    filename: str = "attachment.bin",
    mime_type: str = "application/octet-stream",
    strip_base64_padding: bool = False,
) -> Mock:
    """Build a Mock google-api service returning ``payload`` as an attachment."""
    urlsafe_b64 = base64.urlsafe_b64encode(payload).decode("ascii")
    if strip_base64_padding:
        urlsafe_b64 = urlsafe_b64.rstrip("=")

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


def test_get_gmail_attachment_content_schema_has_no_base64_escape_hatch():
    """Published MCP schema must keep attachment bytes out of tool arguments."""
    components = get_tool_components(server)
    schema = components[get_gmail_attachment_content.__name__].parameters["properties"]

    assert "return_base64" not in schema
    assert schema["include_file"]["type"] == "boolean"
    assert schema["include_file"]["default"] is True


@pytest.mark.asyncio
async def test_default_call_delivers_attachment(isolated_attachment_env):
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

    assert "Attachment downloaded successfully!" in _result_text(result)
    assert isinstance(result, ToolResult)
    resource = _result_resource(result)
    assert isinstance(resource.resource, BlobResourceContents)
    assert str(resource.resource.uri).endswith("/test.png")
    assert resource.resource.mimeType == "image/png"
    assert base64.b64decode(resource.resource.blob) == payload
    assert result.structured_content["result"] == _result_text(result)
    assert result.structured_content["filename"] == "test.png"
    assert result.structured_content["mime_type"] == "image/png"
    assert result.structured_content["size"] == len(payload)
    assert result.structured_content["workspace_file_id"]


@pytest.mark.asyncio
async def test_include_file_false_omits_embedded_resource(isolated_attachment_env):
    """Callers can opt out when they only need extracted text or metadata."""
    payload = b"plain attachment text"
    mock_service = _build_mock_service(
        payload, filename="note.txt", mime_type="text/plain"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
        include_file=False,
    )

    assert isinstance(result, ToolResult)
    assert not any(isinstance(block, EmbeddedResource) for block in result.content)
    assert "plain attachment text" in _result_text(result)


@pytest.mark.asyncio
async def test_stateless_mode_still_returns_portable_file(monkeypatch):
    """Diskless deployments should return the attachment through MCP itself."""
    monkeypatch.setattr("gmail.gmail_tools.is_stateless_mode", lambda: True)
    payload = b"portable stateless attachment"
    mock_service = _build_mock_service(
        payload, filename="portable.txt", mime_type="text/plain"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert isinstance(result, ToolResult)
    resource = _result_resource(result)
    assert isinstance(resource.resource, BlobResourceContents)
    assert str(resource.resource.uri).endswith("/portable.txt")
    assert base64.b64decode(resource.resource.blob) == payload
    assert "Portable MCP file: included" in _result_text(result)
    assert "workspace_file_id" not in result.structured_content


@pytest.mark.asyncio
async def test_stdio_path_is_labeled_server_local(isolated_attachment_env, monkeypatch):
    """A bridged stdio server must not claim its path exists on the client."""
    import core.config as core_config_module

    monkeypatch.setattr(core_config_module, "get_transport_mode", lambda: "stdio")
    mock_service = _build_mock_service(
        b"remote file", filename="remote.txt", mime_type="text/plain"
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    text = _result_text(result)
    assert "Server-local backup:" in text
    assert "may not exist on the client machine" in text
    assert "can be accessed directly" not in text


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

    assert "Filename: RE: Foo?.eml" in _result_text(result)
    assert "Saved filename: RE_ Foo_" in _result_text(result)

    saved_files = list(isolated_attachment_env.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].name.startswith("RE_ Foo_")
    assert ":" not in saved_files[0].name
    assert "?" not in saved_files[0].name
    assert saved_files[0].read_bytes() == payload


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

    assert "--- EXTRACTED TEXT ---" in _result_text(result)
    assert "Sveiki, this is the archive certificate" in _result_text(result)


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

    assert "--- EXTRACTED TEXT ---" in _result_text(result)
    assert "Sniffed content" in _result_text(result)


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

    assert "--- EXTRACTED TEXT ---" in _result_text(result)
    assert "Extracted PDF text" in _result_text(result)


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

    assert "--- EXTRACTED TEXT ---" in _result_text(result)
    assert "name,age" in _result_text(result)


@pytest.mark.asyncio
async def test_unpadded_base64_attachment_is_extracted(isolated_attachment_env):
    """Text extraction should work when Gmail omits base64url padding."""
    payload = b"unpadded attachment text"
    mock_service = _build_mock_service(
        payload,
        filename="note.txt",
        mime_type="text/plain",
        strip_base64_padding=True,
    )

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert "--- EXTRACTED TEXT ---" in _result_text(result)
    assert "unpadded attachment text" in _result_text(result)


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

    assert "--- EXTRACTED TEXT ---" not in _result_text(result)


def test_format_extracted_text_block_truncates_long_text():
    """Huge documents must be capped so they can't flood the client context."""
    text = "x" * (EXTRACTED_TEXT_CHAR_LIMIT + 500)

    lines = _format_extracted_text_block(text)

    assert lines[0] == "\n--- EXTRACTED TEXT ---"
    assert len(lines[1]) == EXTRACTED_TEXT_CHAR_LIMIT
    assert "truncated" in lines[2]


@pytest.mark.asyncio
async def test_resolves_correct_filename_for_nested_smime_attachment(
    isolated_attachment_env,
):
    """Resolve a nested attachment instead of its top-level S/MIME signature."""
    payload = b"%PDF-1.4 fake pdf bytes" + bytes(range(200))
    mock_service = _build_mock_service(payload)

    def messages_get(**kwargs):
        mixed_part = {"mimeType": "multipart/mixed"}
        if kwargs["fields"].count("parts(") >= 2:
            mixed_part["parts"] = [
                {
                    "filename": "statement.pdf",
                    "mimeType": "application/pdf",
                    "body": {
                        "attachmentId": "att-pdf-123",
                        "size": len(payload),
                    },
                }
            ]
        return Mock(
            execute=Mock(
                return_value={
                    "payload": {
                        "mimeType": "multipart/signed",
                        "parts": [
                            mixed_part,
                            {
                                "filename": "smime.p7s",
                                "mimeType": "application/pkcs7-signature",
                                "body": {"attachmentId": "sig-456", "size": 4771},
                            },
                        ],
                    }
                }
            )
        )

    metadata_get = Mock(side_effect=messages_get)
    mock_service.users().messages().get = metadata_get

    result = await _unwrap(get_gmail_attachment_content)(
        service=mock_service,
        message_id="msg-1",
        attachment_id="att-pdf-123",
        user_google_email="user@example.com",
    )

    assert "Filename: statement.pdf" in _result_text(result)
    assert "smime.p7s" not in _result_text(result)
    fields = metadata_get.call_args.kwargs["fields"]
    assert fields.count("parts(") == 6
    assert "data" not in fields

    saved_files = list(isolated_attachment_env.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].suffix == ".pdf"
    assert saved_files[0].read_bytes() == payload


@pytest.mark.asyncio
async def test_stateless_attachment_over_inline_limit_errors(monkeypatch):
    monkeypatch.setattr("gmail.gmail_tools.is_stateless_mode", lambda: True)
    monkeypatch.setenv("WORKSPACE_MCP_INLINE_FILE_MAX_BYTES", "4")
    service = _build_mock_service(b"abcde", filename="large.bin")

    with pytest.raises(ToolError, match="stateless mode cannot stage"):
        await _unwrap(get_gmail_attachment_content)(
            service=service,
            message_id="msg-1",
            attachment_id="att-123",
            user_google_email="user@example.com",
        )


@pytest.mark.asyncio
async def test_stateful_attachment_over_inline_limit_returns_link(
    isolated_attachment_env, monkeypatch
):
    monkeypatch.setenv("WORKSPACE_MCP_INLINE_FILE_MAX_BYTES", "4")
    monkeypatch.setattr(
        "core.file_delivery.get_attachment_url",
        lambda file_id: f"https://files.example/attachments/{file_id}",
    )
    service = _build_mock_service(b"abcde", filename="large.bin")

    result = await _unwrap(get_gmail_attachment_content)(
        service=service,
        message_id="msg-1",
        attachment_id="att-123",
        user_google_email="user@example.com",
    )

    assert any(isinstance(item, ResourceLink) for item in result.content)
    assert not any(isinstance(item, EmbeddedResource) for item in result.content)
    assert result.structured_content["workspace_file_id"]
