"""
Unit tests for Google Chat MCP tools — attachment support
"""

import asyncio
import inspect
import ssl
from urllib.parse import urlparse

import pytest
from unittest.mock import AsyncMock, Mock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))


def _make_message(text="Hello", attachments=None, msg_name="spaces/S/messages/M"):
    """Build a minimal Chat API message dict for testing."""
    msg = {
        "name": msg_name,
        "text": text,
        "createTime": "2025-01-01T00:00:00Z",
        "sender": {"name": "users/123", "displayName": "Test User"},
    }
    if attachments is not None:
        msg["attachment"] = attachments
    return msg


def _make_attachment(
    name="spaces/S/messages/M/attachments/A",
    content_name="image.png",
    content_type="image/png",
    resource_name="spaces/S/attachments/A",
):
    att = {
        "name": name,
        "contentName": content_name,
        "contentType": content_type,
        "source": "UPLOADED_CONTENT",
    }
    if resource_name:
        att["attachmentDataRef"] = {"resourceName": resource_name}
    return att


def _unwrap(tool):
    """Unwrap a FunctionTool + decorator chain to the original async function."""
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


# ---------------------------------------------------------------------------
# get_messages: attachment metadata appears in output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_get_messages_shows_attachment_metadata(mock_resolve):
    """When a message has attachments, get_messages should surface their metadata."""
    mock_resolve.return_value = "Test User"

    att = _make_attachment()
    msg = _make_message(attachments=[att])

    chat_service = Mock()
    chat_service.spaces().get().execute.return_value = {"displayName": "Test Space"}
    chat_service.spaces().messages().list().execute.return_value = {"messages": [msg]}

    people_service = Mock()

    from gchat.chat_tools import get_messages

    result = await _unwrap(get_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        space_id="spaces/S",
    )

    assert "[attachment 0: image.png (image/png)]" in result
    assert "download_chat_attachment" in result


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_get_messages_no_attachments_unchanged(mock_resolve):
    """Messages without attachments should not include attachment lines."""
    mock_resolve.return_value = "Test User"

    msg = _make_message(text="Plain text message")

    chat_service = Mock()
    chat_service.spaces().get().execute.return_value = {"displayName": "Test Space"}
    chat_service.spaces().messages().list().execute.return_value = {"messages": [msg]}

    people_service = Mock()

    from gchat.chat_tools import get_messages

    result = await _unwrap(get_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        space_id="spaces/S",
    )

    assert "Plain text message" in result
    assert "[attachment" not in result


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_get_messages_multiple_attachments(mock_resolve):
    """Multiple attachments should each appear with their index."""
    mock_resolve.return_value = "Test User"

    attachments = [
        _make_attachment(content_name="photo.jpg", content_type="image/jpeg"),
        _make_attachment(
            name="spaces/S/messages/M/attachments/B",
            content_name="doc.pdf",
            content_type="application/pdf",
        ),
    ]
    msg = _make_message(attachments=attachments)

    chat_service = Mock()
    chat_service.spaces().get().execute.return_value = {"displayName": "Test Space"}
    chat_service.spaces().messages().list().execute.return_value = {"messages": [msg]}

    people_service = Mock()

    from gchat.chat_tools import get_messages

    result = await _unwrap(get_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        space_id="spaces/S",
    )

    assert "[attachment 0: photo.jpg (image/jpeg)]" in result
    assert "[attachment 1: doc.pdf (application/pdf)]" in result


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_get_messages_exposes_message_filter_and_forwards_it(mock_resolve):
    """get_messages should expose message_filter publicly and pass it to the Chat API."""
    mock_resolve.return_value = "Test User"

    msg = _make_message(text="Filtered message")
    chat_service = Mock()
    chat_service.spaces().get().execute.return_value = {"displayName": "Test Space"}
    chat_service.spaces().messages().list().execute.return_value = {"messages": [msg]}
    people_service = Mock()

    from gchat.chat_tools import get_messages

    public_fn = getattr(get_messages, "fn", get_messages)
    params = inspect.signature(public_fn).parameters

    assert "message_filter" in params
    assert "filter" not in params

    result = await _unwrap(get_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        space_id="spaces/S",
        message_filter="thread.name = spaces/S/threads/T",
    )

    assert "Filtered message" in result
    list_kwargs = chat_service.spaces().messages().list.call_args.kwargs
    assert list_kwargs["filter"] == "thread.name = spaces/S/threads/T"


@pytest.mark.asyncio
async def test_get_messages_resolves_senders_sequentially(monkeypatch):
    """get_messages should avoid concurrent People API sender resolution."""
    state = {"current": 0, "max": 0}

    async def fake_resolve(_people_service, sender_obj):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.01)
        try:
            return f"Resolved {sender_obj['name']}"
        finally:
            state["current"] -= 1

    msg_one = _make_message(text="First message", msg_name="spaces/S/messages/M1")
    msg_one["sender"] = {"name": "users/1"}
    msg_two = _make_message(text="Second message", msg_name="spaces/S/messages/M2")
    msg_two["sender"] = {"name": "users/2"}

    chat_service = Mock()
    chat_service.spaces().get().execute.return_value = {"displayName": "Test Space"}
    chat_service.spaces().messages().list().execute.return_value = {
        "messages": [msg_one, msg_two]
    }
    people_service = Mock()

    monkeypatch.setattr("gchat.chat_tools._resolve_sender", fake_resolve)

    from gchat.chat_tools import get_messages

    result = await _unwrap(get_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        space_id="spaces/S",
    )

    assert "Resolved users/1" in result
    assert "Resolved users/2" in result
    assert state["max"] == 1


# ---------------------------------------------------------------------------
# search_messages: attachment indicator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_search_messages_shows_attachment_indicator(mock_resolve):
    """search_messages should show [attachment: filename] for messages with attachments."""
    mock_resolve.return_value = "Test User"

    att = _make_attachment(content_name="report.pdf", content_type="application/pdf")
    msg = _make_message(text="Here is the report", attachments=[att])
    msg["_space_name"] = "General"

    chat_service = Mock()
    chat_service.spaces().list().execute.return_value = {
        "spaces": [{"name": "spaces/S", "displayName": "General"}]
    }
    chat_service.spaces().messages().list().execute.return_value = {"messages": [msg]}

    people_service = Mock()

    from gchat.chat_tools import search_messages

    result = await _unwrap(search_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        query="report",
    )

    assert "[attachment: report.pdf (application/pdf)]" in result


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_search_messages_combines_filters_and_uses_page_size(mock_resolve):
    """Cross-space search should honor page_size and only send supported API filters."""
    mock_resolve.return_value = "Test User"

    msg = _make_message(text="Deploy finished")
    msg["_space_name"] = "General"

    chat_service = Mock()
    chat_service.spaces().list().execute.return_value = {
        "spaces": [{"name": "spaces/S", "displayName": "General"}]
    }
    chat_service.spaces().messages().list().execute.return_value = {"messages": [msg]}
    people_service = Mock()

    from gchat.chat_tools import search_messages

    result = await _unwrap(search_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        query="deploy",
        time_filter='createTime > "2026-03-18T00:00:00-03:00"',
        page_size=7,
    )

    assert 'text "deploy" and createTime > "2026-03-18T00:00:00-03:00"' in result
    list_kwargs = chat_service.spaces().messages().list.call_args.kwargs
    assert list_kwargs["pageSize"] == 7
    assert list_kwargs["filter"] == 'createTime > "2026-03-18T00:00:00-03:00"'


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_search_messages_query_only_filters_client_side_without_api_filter(
    mock_resolve,
):
    """Query-only search should avoid unsupported Chat API text filters."""
    mock_resolve.return_value = "Test User"

    matching = _make_message(text="Deploy finished")
    matching["_space_name"] = "General"
    non_matching = _make_message(text="Lunch plans")
    non_matching["_space_name"] = "General"

    chat_service = Mock()
    chat_service.spaces().list().execute.return_value = {
        "spaces": [{"name": "spaces/S", "displayName": "General"}]
    }
    chat_service.spaces().messages().list().execute.return_value = {
        "messages": [matching, non_matching]
    }
    people_service = Mock()

    from gchat.chat_tools import search_messages

    result = await _unwrap(search_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        query="deploy",
    )

    assert "Deploy finished" in result
    assert "Lunch plans" not in result
    list_kwargs = chat_service.spaces().messages().list.call_args.kwargs
    assert list_kwargs["pageSize"] == 25
    assert "filter" not in list_kwargs


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_search_messages_limits_parallel_space_fetches(mock_resolve, monkeypatch):
    """Cross-space search should cap concurrent threaded Chat API calls."""
    mock_resolve.return_value = "Test User"

    from gchat.chat_tools import _SEARCH_MESSAGES_MAX_CONCURRENT_SPACE_FETCHES
    from gchat.chat_tools import search_messages

    state = {"current": 0, "max": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.01)
        try:
            return fn(*args, **kwargs)
        finally:
            state["current"] -= 1

    monkeypatch.setattr("gchat.chat_tools.asyncio.to_thread", fake_to_thread)

    spaces = [{"name": f"spaces/S{i}", "displayName": f"Space {i}"} for i in range(5)]

    def list_messages(**kwargs):
        parent = kwargs["parent"]
        request = Mock()
        request.execute.return_value = {
            "messages": [_make_message(text=f"message from {parent}")]
        }
        return request

    chat_service = Mock()
    chat_service.spaces().list().execute.return_value = {"spaces": spaces}
    chat_service.spaces().messages().list.side_effect = list_messages
    people_service = Mock()

    result = await _unwrap(search_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        query="message",
        max_spaces=len(spaces),
    )

    assert "Found 5 messages matching 'text \"message\"'" in result
    assert state["max"] <= _SEARCH_MESSAGES_MAX_CONCURRENT_SPACE_FETCHES


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_search_messages_retries_ssl_per_space_without_restarting_search(
    mock_resolve, monkeypatch
):
    """A transient SSL failure in one space should retry only that space."""
    mock_resolve.return_value = "Test User"

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("gchat.chat_tools.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("gchat.chat_tools.asyncio.sleep", AsyncMock())

    from gchat.chat_tools import search_messages

    attempt_counts = {"spaces/S1": 0, "spaces/S2": 0}

    def list_messages(**kwargs):
        parent = kwargs["parent"]
        request = Mock()

        def execute():
            attempt_counts[parent] += 1
            if parent == "spaces/S1" and attempt_counts[parent] < 3:
                raise ssl.SSLError("read operation timed out")
            return {"messages": [_make_message(text=f"message from {parent}")]}

        request.execute.side_effect = execute
        return request

    chat_service = Mock()
    chat_service.spaces().list().execute.return_value = {
        "spaces": [
            {"name": "spaces/S1", "displayName": "Space 1"},
            {"name": "spaces/S2", "displayName": "Space 2"},
        ]
    }
    chat_service.spaces().messages().list.side_effect = list_messages
    people_service = Mock()

    result = await _unwrap(search_messages)(
        chat_service=chat_service,
        people_service=people_service,
        user_google_email="test@example.com",
        query="message",
        max_spaces=2,
    )

    assert "Found 2 messages matching 'text \"message\"'" in result
    assert attempt_counts == {"spaces/S1": 3, "spaces/S2": 1}
    assert chat_service.spaces().list().execute.call_count == 1


@pytest.mark.asyncio
@patch("gchat.chat_tools._resolve_sender", new_callable=AsyncMock)
async def test_search_messages_raises_transient_error_when_all_spaces_ssl_fail(
    mock_resolve, monkeypatch
):
    """If every searched space fails transiently, surface a transient network error."""
    mock_resolve.return_value = "Test User"

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("gchat.chat_tools.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("gchat.chat_tools.asyncio.sleep", AsyncMock())

    from core.utils import TransientNetworkError
    from gchat.chat_tools import search_messages

    def list_messages(**kwargs):  # noqa: ARG001
        request = Mock()
        request.execute.side_effect = ssl.SSLError("connection reset")
        return request

    chat_service = Mock()
    chat_service.spaces().list().execute.return_value = {
        "spaces": [{"name": "spaces/S1", "displayName": "Space 1"}]
    }
    chat_service.spaces().messages().list.side_effect = list_messages
    people_service = Mock()

    with pytest.raises(TransientNetworkError, match="transient SSL error"):
        await _unwrap(search_messages)(
            chat_service=chat_service,
            people_service=people_service,
            user_google_email="test@example.com",
            query="message",
        )


# ---------------------------------------------------------------------------
# download_chat_attachment: edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_no_attachments():
    """Should return a clear message when the message has no attachments."""
    service = Mock()
    service.spaces().messages().get().execute.return_value = _make_message()

    from gchat.chat_tools import download_chat_attachment

    result = await _unwrap(download_chat_attachment)(
        service=service,
        user_google_email="test@example.com",
        message_id="spaces/S/messages/M",
    )

    assert "No attachments found" in result


@pytest.mark.asyncio
async def test_download_invalid_index():
    """Should reject an out-of-range attachment_index as user input."""
    msg = _make_message(attachments=[_make_attachment()])
    service = Mock()
    service.spaces().messages().get().execute.return_value = msg

    from gchat.chat_tools import download_chat_attachment

    from core.utils import UserInputError

    with pytest.raises(UserInputError, match="Invalid attachment_index"):
        await _unwrap(download_chat_attachment)(
            service=service,
            user_google_email="test@example.com",
            message_id="spaces/S/messages/M",
            attachment_index=5,
        )


class _StreamResponse:
    def __init__(self, data=b"", status_code=200, headers=None):
        self.data = data
        self.status_code = status_code
        self.headers = headers or {"content-length": str(len(data))}

    async def aread(self):
        return self.data

    async def aiter_bytes(self):
        yield self.data


class _StreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _StreamClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.stream_call = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, **kwargs):
        self.stream_call = (method, url, kwargs)
        if self.error:
            raise self.error
        return _StreamContext(self.response)


def _tool_result_text(result):
    return next(item.text for item in result.content if item.type == "text")


@pytest.mark.asyncio
async def test_download_uses_api_media_endpoint(tmp_path, monkeypatch):
    """Should always use chat.googleapis.com media endpoint, not downloadUri."""
    fake_bytes = b"fake image content"
    att = _make_attachment()
    # Even with a downloadUri present, we should use the API endpoint
    att["downloadUri"] = "https://chat.google.com/api/get_attachment_url?bad=url"
    msg = _make_message(attachments=[att])

    service = Mock()
    service.spaces().messages().get().execute.return_value = msg
    service._http.credentials.token = "fake-access-token"

    from gchat.chat_tools import download_chat_attachment

    import core.attachment_storage as attachment_storage
    from core.attachment_storage import AttachmentStorage

    monkeypatch.setattr(attachment_storage, "STORAGE_DIR", tmp_path)
    storage = AttachmentStorage()
    mock_client = _StreamClient(_StreamResponse(fake_bytes))

    with (
        patch("gchat.chat_tools.httpx.AsyncClient", return_value=mock_client),
        patch("gchat.chat_tools.is_stateless_mode", return_value=False),
        patch("gchat.chat_tools.get_transport_mode", return_value="stdio"),
        patch("core.file_delivery.get_attachment_storage", return_value=storage),
        patch(
            "core.file_delivery.get_attachment_url",
            side_effect=lambda file_id: f"https://files.test/attachments/{file_id}",
        ),
    ):
        result = await _unwrap(download_chat_attachment)(
            service=service,
            user_google_email="test@example.com",
            message_id="spaces/S/messages/M",
            attachment_index=0,
        )

    assert "image.png" in _tool_result_text(result)
    assert "Server-local backup:" in _tool_result_text(result)
    assert result.structured_content["workspace_file_id"]

    # Verify we used the API endpoint with attachmentDataRef.resourceName
    method, url_used, call_kwargs = mock_client.stream_call
    assert method == "GET"
    parsed = urlparse(url_used)
    assert parsed.scheme == "https"
    assert parsed.hostname == "chat.googleapis.com"
    assert "alt=media" in url_used
    assert "spaces/S/attachments/A" in parsed.path
    assert "/messages/" not in parsed.path

    # Verify Bearer token
    assert call_kwargs["headers"]["Authorization"] == "Bearer fake-access-token"
    saved_path = storage.get_attachment_path(
        result.structured_content["workspace_file_id"]
    )
    assert saved_path and saved_path.read_bytes() == fake_bytes


@pytest.mark.asyncio
async def test_download_falls_back_to_att_name(monkeypatch):
    """When attachmentDataRef is missing, should fall back to attachment name."""
    fake_bytes = b"fetched content"
    att = _make_attachment(name="spaces/S/messages/M/attachments/A", resource_name=None)
    msg = _make_message(attachments=[att])

    service = Mock()
    service.spaces().messages().get().execute.return_value = msg
    service._http.credentials.token = "fake-access-token"

    mock_client = _StreamClient(_StreamResponse(fake_bytes))

    from gchat.chat_tools import download_chat_attachment

    with (
        patch("gchat.chat_tools.httpx.AsyncClient", return_value=mock_client),
        patch("gchat.chat_tools.is_stateless_mode", return_value=True),
    ):
        result = await _unwrap(download_chat_attachment)(
            service=service,
            user_google_email="test@example.com",
            message_id="spaces/S/messages/M",
            attachment_index=0,
        )

    assert "image.png" in _tool_result_text(result)

    # Falls back to attachment name when no attachmentDataRef
    assert "spaces/S/messages/M/attachments/A" in mock_client.stream_call[1]


@pytest.mark.asyncio
async def test_download_http_mode_returns_url(tmp_path, monkeypatch):
    """In HTTP mode, should return a download URL instead of file path."""
    fake_bytes = b"image data"
    att = _make_attachment()
    msg = _make_message(attachments=[att])

    service = Mock()
    service.spaces().messages().get().execute.return_value = msg
    service._http.credentials.token = "fake-token"

    import core.attachment_storage as attachment_storage
    from core.attachment_storage import AttachmentStorage

    monkeypatch.setattr(attachment_storage, "STORAGE_DIR", tmp_path)
    storage = AttachmentStorage()
    mock_client = _StreamClient(_StreamResponse(fake_bytes))

    from gchat.chat_tools import download_chat_attachment

    with (
        patch("gchat.chat_tools.httpx.AsyncClient", return_value=mock_client),
        patch("gchat.chat_tools.is_stateless_mode", return_value=False),
        patch("gchat.chat_tools.get_transport_mode", return_value="streamable-http"),
        patch("core.file_delivery.get_attachment_storage", return_value=storage),
        patch(
            "core.file_delivery.get_attachment_url",
            side_effect=lambda file_id: f"http://localhost:8005/attachments/{file_id}",
        ),
    ):
        result = await _unwrap(download_chat_attachment)(
            service=service,
            user_google_email="test@example.com",
            message_id="spaces/S/messages/M",
            attachment_index=0,
        )

    assert "Download URL:" in _tool_result_text(result)
    assert "expire after 1 hour" in _tool_result_text(result)


@pytest.mark.asyncio
async def test_download_returns_error_on_failure():
    """When download fails, should return a clear error message."""
    att = _make_attachment()
    att["downloadUri"] = "https://storage.googleapis.com/fake?alt=media"
    msg = _make_message(attachments=[att])

    service = Mock()
    service.spaces().messages().get().execute.return_value = msg
    service._http.credentials.token = "fake-token"

    mock_client = _StreamClient(error=Exception("connection refused"))

    from gchat.chat_tools import download_chat_attachment

    from fastmcp.exceptions import ToolError

    with (
        patch("gchat.chat_tools.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(ToolError, match="connection refused"),
    ):
        await _unwrap(download_chat_attachment)(
            service=service,
            user_google_email="test@example.com",
            message_id="spaces/S/messages/M",
            attachment_index=0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{"content-length": "5"}, {}])
async def test_stateless_large_download_rejected_and_temp_removed(
    tmp_path, monkeypatch, headers
):
    from fastmcp.exceptions import ToolError
    from gchat import chat_tools
    from gchat.chat_tools import download_chat_attachment

    temp_path = tmp_path / "chat-download.tmp"
    handle = temp_path.open("w+b")
    handle.close()
    fake_handle = Mock(name=str(temp_path))
    fake_handle.name = str(temp_path)
    fake_handle.close = Mock()
    monkeypatch.setattr(
        chat_tools.tempfile, "NamedTemporaryFile", lambda **_kwargs: fake_handle
    )
    monkeypatch.setenv("WORKSPACE_MCP_INLINE_FILE_MAX_BYTES", "4")

    service = Mock()
    service.spaces().messages().get().execute.return_value = _make_message(
        attachments=[_make_attachment()]
    )
    service._http.credentials.token = "fake-token"
    client = _StreamClient(_StreamResponse(b"abcde", headers=headers))

    with (
        patch("gchat.chat_tools.httpx.AsyncClient", return_value=client),
        patch("gchat.chat_tools.is_stateless_mode", return_value=True),
        pytest.raises(ToolError, match="inline limit"),
    ):
        await _unwrap(download_chat_attachment)(
            service=service,
            user_google_email="test@example.com",
            message_id="spaces/S/messages/M",
        )

    assert not temp_path.exists()
