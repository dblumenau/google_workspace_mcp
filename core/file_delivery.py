"""Portable MCP file delivery shared by Gmail, Drive, and Chat tools."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)

from auth.oauth_config import is_stateless_mode
from core.attachment_storage import (
    SavedAttachment,
    get_attachment_storage,
    get_attachment_url,
    sanitize_attachment_filename,
)
from core.config import get_transport_mode
from core.staging_s3 import StagedFile, stage_bytes, stage_path, staging_is_configured

DEFAULT_INLINE_FILE_MAX_BYTES = 8 * 1024 * 1024
INLINE_FILE_MAX_BYTES_ENV = "WORKSPACE_MCP_INLINE_FILE_MAX_BYTES"


def get_inline_file_max_bytes() -> int:
    """Return the configured maximum embedded-file size."""
    raw_value = os.getenv(INLINE_FILE_MAX_BYTES_ENV, str(DEFAULT_INLINE_FILE_MAX_BYTES))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{INLINE_FILE_MAX_BYTES_ENV} must be a non-negative integer, "
            f"got {raw_value!r}."
        ) from exc
    if value < 0:
        raise ValueError(
            f"{INLINE_FILE_MAX_BYTES_ENV} must be a non-negative integer, got {value}."
        )
    return value


def _format_expiry(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _delivery_result(
    *,
    summary: str,
    filename: str,
    mime_type: str,
    size: int,
    include_file: bool,
    file_bytes: Optional[bytes] = None,
    saved: Optional[SavedAttachment] = None,
    fallback_url: Optional[str] = None,
    staged: Optional[StagedFile] = None,
    content_kind: str = "resource",
) -> ToolResult:
    """Build a mixed-content result after any storage work is complete."""
    safe_filename = sanitize_attachment_filename(filename)
    content: list[Any] = [TextContent(type="text", text=summary)]

    structured: dict[str, Any] = {
        "result": summary,
        "filename": safe_filename,
        "mime_type": mime_type,
        "size": size,
    }

    if staged is not None:
        structured.update(
            {
                "staged_key": staged.key,
                "download_url": staged.download_url,
                "expires_in_seconds": staged.expires_in_seconds,
            }
        )
        if include_file:
            content.append(
                ResourceLink(
                    type="resource_link",
                    name=safe_filename,
                    uri=staged.download_url,
                    mimeType=mime_type,
                    size=size,
                    description="Temporary signed download link.",
                )
            )
    elif saved is not None:
        storage = get_attachment_storage()
        metadata = storage.get_attachment_metadata(saved.file_id) or {}
        download_url = get_attachment_url(saved.file_id)
        structured.update(
            {
                "workspace_file_id": saved.file_id,
                "download_url": download_url,
            }
        )
        expires_at = _format_expiry(metadata.get("expires_at"))
        if expires_at:
            structured["expires_at"] = expires_at

        if include_file and file_bytes is None:
            content.append(
                ResourceLink(
                    type="resource_link",
                    name=safe_filename,
                    uri=download_url,
                    mimeType=mime_type,
                    size=size,
                    description="Temporary download link; expires after 1 hour.",
                )
            )
    elif fallback_url:
        structured["download_url"] = fallback_url
        if include_file:
            content.append(
                ResourceLink(
                    type="resource_link",
                    name=safe_filename,
                    uri=fallback_url,
                    mimeType=mime_type,
                    size=size,
                    description="Google-hosted file link; authentication may be required.",
                )
            )

    if include_file and file_bytes is not None:
        encoded = base64.b64encode(file_bytes).decode("ascii")
        if content_kind == "image":
            content.append(ImageContent(type="image", data=encoded, mimeType=mime_type))
        else:
            content.append(
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri=f"file:///{quote(safe_filename)}",
                        mimeType=mime_type,
                        blob=encoded,
                    ),
                )
            )

    return ToolResult(content=content, structured_content=structured)


def _append_delivery_summary(
    summary: str,
    *,
    saved: Optional[SavedAttachment],
    fallback_url: Optional[str],
    embedded: bool,
    include_file: bool,
    limit: int,
    size: int,
    transport: str,
    staged: Optional[StagedFile] = None,
) -> str:
    lines = [summary]
    if embedded and include_file:
        lines.append("\nPortable MCP file: included with this result.")
    elif not include_file:
        lines.append("\nPortable MCP file: omitted because include_file=False.")

    if staged is not None:
        lines.extend(
            [
                f"\nStaged key: {staged.key}",
                f"Download URL: {staged.download_url}",
                f"The signed download URL expires in {staged.expires_in_seconds} seconds.",
            ]
        )
    elif saved is not None:
        url = get_attachment_url(saved.file_id)
        lines.extend(
            [
                f"Saved filename: {sanitize_attachment_filename(Path(saved.path).name)}",
                f"\nWorkspace file ID: {saved.file_id}",
                f"Download URL: {url}",
                "The temporary file and workspace file ID expire after 1 hour.",
            ]
        )
        if transport == "stdio":
            lines.extend(
                [
                    f"Server-local backup: {saved.path}",
                    "The server-local backup may not exist on the client machine.",
                ]
            )
        if not os.getenv("WORKSPACE_EXTERNAL_URL"):
            lines.append(
                "The URL uses the server's configured local address and may not be "
                "reachable from a bridged or remote client."
            )
    elif fallback_url:
        lines.extend(
            [
                f"\nGoogle-hosted file link: {fallback_url}",
                "Authentication may be required to open this link.",
            ]
        )
    elif size > limit:
        lines.append(
            f"\nFile size {size} bytes exceeds the inline limit of {limit} bytes."
        )
    return "\n".join(lines)


async def deliver_file_bytes(
    *,
    summary: str,
    file_bytes: bytes,
    filename: str,
    mime_type: Optional[str],
    include_file: bool = True,
    fallback_url: Optional[str] = None,
    stateless: Optional[bool] = None,
    transport: Optional[str] = None,
    content_kind: str = "resource",
) -> ToolResult:
    """Deliver bytes inline when small and through attachment storage otherwise."""
    resolved_mime = mime_type or "application/octet-stream"
    size = len(file_bytes)
    limit = get_inline_file_max_bytes()
    embed = include_file and limit > 0 and size <= limit
    saved: Optional[SavedAttachment] = None
    staged: Optional[StagedFile] = None

    stateless = is_stateless_mode() if stateless is None else stateless
    transport = transport or get_transport_mode()
    if staging_is_configured() and include_file:
        staged = await asyncio.to_thread(
            stage_bytes, file_bytes, filename, resolved_mime
        )
        embed = False
    elif not stateless:
        storage = get_attachment_storage()

        def _save() -> SavedAttachment:
            return storage.save_attachment(
                base64_data=base64.urlsafe_b64encode(file_bytes).decode("ascii"),
                filename=filename,
                mime_type=resolved_mime,
            )

        try:
            saved = await asyncio.to_thread(_save)
        except Exception as exc:
            if not embed and not fallback_url:
                raise ToolError(
                    f"The file is {size} bytes and could not be staged for download: {exc}"
                ) from exc
    elif not embed and include_file and not fallback_url:
        raise ToolError(
            f"The file is {size} bytes, which exceeds the inline limit of {limit} "
            "bytes, and stateless mode cannot stage a download link."
        )

    effective_fallback = fallback_url if saved is None and not embed else None
    rendered_summary = _append_delivery_summary(
        summary,
        saved=saved,
        fallback_url=effective_fallback,
        embedded=embed,
        include_file=include_file,
        limit=limit,
        size=size,
        transport=transport,
        staged=staged,
    )
    return _delivery_result(
        summary=rendered_summary,
        filename=filename,
        mime_type=resolved_mime,
        size=size,
        include_file=include_file,
        file_bytes=file_bytes if embed else None,
        saved=saved,
        fallback_url=effective_fallback,
        staged=staged,
        content_kind=content_kind,
    )


async def deliver_file_path(
    *,
    summary: str,
    file_path: Path,
    filename: str,
    mime_type: Optional[str],
    include_file: bool = True,
    fallback_url: Optional[str] = None,
    stateless: Optional[bool] = None,
    transport: Optional[str] = None,
    content_kind: str = "resource",
) -> ToolResult:
    """Deliver a streamed temporary file without reading large files into memory."""
    resolved_mime = mime_type or "application/octet-stream"
    size = file_path.stat().st_size
    limit = get_inline_file_max_bytes()
    embed = include_file and limit > 0 and size <= limit
    file_bytes: Optional[bytes] = None
    saved: Optional[SavedAttachment] = None
    staged: Optional[StagedFile] = None

    stateless = is_stateless_mode() if stateless is None else stateless
    transport = transport or get_transport_mode()
    try:
        if staging_is_configured() and include_file:
            staged = await asyncio.to_thread(
                stage_path, file_path, filename, resolved_mime
            )
            embed = False
        elif embed:
            file_bytes = await asyncio.to_thread(file_path.read_bytes)

        if staged is None and not stateless:
            storage = get_attachment_storage()
            try:
                saved = await asyncio.to_thread(
                    storage.save_attachment_from_path,
                    src_path=str(file_path),
                    filename=filename,
                    mime_type=resolved_mime,
                )
            except Exception as exc:
                if not embed and not fallback_url:
                    raise ToolError(
                        f"The file is {size} bytes and could not be staged for "
                        f"download: {exc}"
                    ) from exc
        elif staged is None and not embed and include_file and not fallback_url:
            raise ToolError(
                f"The file is {size} bytes, which exceeds the inline limit of "
                f"{limit} bytes, and stateless mode cannot stage a download link."
            )

        effective_fallback = fallback_url if saved is None and not embed else None
        rendered_summary = _append_delivery_summary(
            summary,
            saved=saved,
            fallback_url=effective_fallback,
            embedded=embed,
            include_file=include_file,
            limit=limit,
            size=size,
            transport=transport,
            staged=staged,
        )
        return _delivery_result(
            summary=rendered_summary,
            filename=filename,
            mime_type=resolved_mime,
            size=size,
            include_file=include_file,
            file_bytes=file_bytes,
            saved=saved,
            fallback_url=effective_fallback,
            staged=staged,
            content_kind=content_kind,
        )
    finally:
        # save_attachment_from_path consumes the file on success. In every other
        # path it remains the caller-owned temporary and must be removed.
        file_path.unlink(missing_ok=True)
