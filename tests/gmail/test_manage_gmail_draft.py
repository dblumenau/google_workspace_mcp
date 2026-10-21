import base64
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.server import server
from core.tool_registry import get_tool_components
from core.tool_tier_loader import get_tools_for_tier
from core.utils import UserInputError
from gmail.gmail_tools import manage_gmail_draft


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _draft_message(*, raw: str | None = None):
    message = {
        "id": "message456",
        "threadId": "thread789",
        "snippet": "A short preview",
    }
    if raw is not None:
        message["raw"] = _encoded(raw)
        return message
    message["payload"] = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": "Draft subject"},
            {"name": "From", "value": "user@example.com"},
            {"name": "To", "value": "recipient@example.com"},
            {"name": "Bcc", "value": "archive@example.com"},
            {"name": "Date", "value": "Sat, 8 Aug 2026 12:00:00 +0200"},
        ],
        "body": {"data": _encoded("Draft body")},
    }
    return message


@pytest.mark.asyncio
async def test_manage_gmail_draft_lists_enriched_drafts_and_pagination():
    mock_service = Mock()
    mock_service.users().drafts().list().execute.return_value = {
        "drafts": [
            {
                "id": "draft123",
                "message": {"id": "message456", "threadId": "thread789"},
            }
        ],
        "nextPageToken": "next-token",
        "resultSizeEstimate": 3,
    }
    mock_service.users().messages().get().execute.return_value = _draft_message()

    result = await _unwrap(manage_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        action="list",
        query="subject:draft",
        page_size=5,
        page_token="current-token",
    )

    list_kwargs = (
        mock_service.users.return_value.drafts.return_value.list.call_args.kwargs
    )
    assert list_kwargs == {
        "userId": "me",
        "maxResults": 5,
        "q": "subject:draft",
        "pageToken": "current-token",
    }
    assert "Draft ID: draft123" in result
    assert "Message ID: message456" in result
    assert "Thread ID: thread789" in result
    assert "Subject: Draft subject" in result
    assert "Bcc: archive@example.com" in result
    assert "Next page token: next-token" in result


@pytest.mark.asyncio
async def test_manage_gmail_draft_list_handles_empty_page():
    mock_service = Mock()
    mock_service.users().drafts().list().execute.return_value = {"drafts": []}

    result = await _unwrap(manage_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        action="list",
    )

    assert result == "No Gmail drafts found."
    assert mock_service.users.return_value.messages.return_value.get.call_count == 0


@pytest.mark.asyncio
async def test_manage_gmail_draft_get_returns_readable_content():
    mock_service = Mock()
    mock_service.users().drafts().get().execute.return_value = {
        "id": "draft123",
        "message": _draft_message(),
    }

    result = await _unwrap(manage_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        action="get",
        draft_id="draft123",
        body_format="text",
    )

    get_kwargs = (
        mock_service.users.return_value.drafts.return_value.get.call_args.kwargs
    )
    assert get_kwargs == {"userId": "me", "id": "draft123", "format": "full"}
    assert "Draft ID: draft123" in result
    assert "Subject: Draft subject" in result
    assert "Bcc: archive@example.com" in result
    assert "Draft body" in result


@pytest.mark.asyncio
async def test_manage_gmail_draft_get_decodes_raw_mime():
    mock_service = Mock()
    mock_service.users().drafts().get().execute.return_value = {
        "id": "draft123",
        "message": _draft_message(raw="Subject: Raw draft\n\nRaw body"),
    }

    result = await _unwrap(manage_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        action="get",
        draft_id="draft123",
        body_format="raw",
    )

    get_kwargs = (
        mock_service.users.return_value.drafts.return_value.get.call_args.kwargs
    )
    assert get_kwargs["format"] == "raw"
    assert "Subject: Raw draft" in result
    assert "Raw body" in result


@pytest.mark.asyncio
async def test_manage_gmail_draft_delete_is_permanent_and_confirmed():
    mock_service = Mock()
    mock_service.users().drafts().delete().execute.return_value = {}

    result = await _unwrap(manage_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        action="delete",
        draft_id="draft123",
    )

    delete_kwargs = (
        mock_service.users.return_value.drafts.return_value.delete.call_args.kwargs
    )
    assert delete_kwargs == {"userId": "me", "id": "draft123"}
    assert "permanently deleted" in result
    assert "not moved to Trash" in result


@pytest.mark.asyncio
async def test_manage_gmail_draft_send_sends_once_and_returns_ids():
    mock_service = Mock()
    mock_service.users().drafts().send().execute.return_value = {
        "id": "sentmsg1",
        "threadId": "thread789",
    }

    result = await _unwrap(manage_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        action="send",
        draft_id="draft123",
    )

    drafts = mock_service.users.return_value.drafts.return_value
    assert drafts.send.call_args.kwargs == {"userId": "me", "body": {"id": "draft123"}}
    assert drafts.delete.call_count == 0
    # Non-idempotent: the execute call must not request retries.
    execute = drafts.send.return_value.execute
    assert execute.call_count == 1
    assert execute.call_args.kwargs.get("num_retries", 0) == 0
    assert "Draft sent." in result
    assert "Message ID: sentmsg1" in result
    assert "Thread ID: thread789" in result
    assert "https://mail.google.com/" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        HttpError(resp=SimpleNamespace(status=503, reason="Unavailable"), content=b""),
        TimeoutError("timed out"),
    ],
)
async def test_manage_gmail_draft_send_reports_unknown_delivery_on_ambiguous_failure(
    error,
):
    mock_service = Mock()
    mock_service.users().drafts().send().execute.side_effect = error

    with pytest.raises(ToolError, match="Check the Sent folder before sending again"):
        await _unwrap(manage_gmail_draft)(
            service=mock_service,
            user_google_email="user@example.com",
            action="send",
            draft_id="draft123",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["get", "delete", "send"])
async def test_manage_gmail_draft_requires_id_for_single_draft_actions(action):
    with pytest.raises(UserInputError, match=f"draft_id is required for the {action}"):
        await _unwrap(manage_gmail_draft)(
            service=Mock(),
            user_google_email="user@example.com",
            action=action,
        )


def test_manage_gmail_draft_is_extended_and_publishes_safe_schema():
    assert "manage_gmail_draft" in get_tools_for_tier("extended", ["gmail"])

    tool = get_tool_components(server)["manage_gmail_draft"]
    schema = tool.parameters
    assert set(schema["properties"]["action"]["enum"]) == {
        "list",
        "get",
        "delete",
        "send",
    }
    assert schema["properties"]["page_size"]["minimum"] == 1
    assert schema["properties"]["page_size"]["maximum"] == 50
