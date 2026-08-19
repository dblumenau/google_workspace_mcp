"""Portable client-to-server file source models and local resolution."""

from __future__ import annotations

import base64
import binascii
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, List, Optional
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, BeforeValidator, Field, model_validator

from core.attachment_storage import get_attachment_storage
from core.utils import UserInputError, _coerce_json_str_to_list, validate_file_path


class PortableFileSource(BaseModel):
    """A file source that works when the MCP client and server are remote."""

    workspace_file_id: Optional[str] = Field(
        default=None,
        description="Temporary workspace file ID returned by a download tool.",
    )
    url: Optional[str] = Field(
        default=None, description="HTTP(S) URL that the MCP server can fetch."
    )
    path: Optional[str] = Field(
        default=None,
        description="Path on the MCP SERVER filesystem, not the client computer.",
    )
    base64_content: Optional[str] = Field(
        default=None, description="Standard RFC 4648 base64-encoded file bytes."
    )
    filename: Optional[str] = Field(
        default=None, description="Optional source filename or override."
    )
    mime_type: Optional[str] = Field(
        default=None, description="Optional source MIME type or override."
    )

    @model_validator(mode="after")
    def _validate_source(self) -> "PortableFileSource":
        sources = {
            "workspace_file_id": self.workspace_file_id,
            "url": self.url,
            "path": self.path,
            "base64_content": self.base64_content,
        }
        present = [name for name, value in sources.items() if value is not None]
        if len(present) != 1:
            raise ValueError(
                "Provide exactly one file source: workspace_file_id, url, path, "
                "or base64_content."
            )
        if self.url and urlparse(self.url).scheme.lower() not in {"http", "https"}:
            raise ValueError("url must use http:// or https://.")
        return self


class EmailAttachmentSource(PortableFileSource):
    """Portable file source with Gmail inline-attachment metadata."""

    base64_content: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("base64_content", "content"),
        description=(
            "Standard RFC 4648 base64 file bytes. Legacy field name 'content' "
            "is also accepted."
        ),
    )
    content_id: Optional[str] = Field(
        default=None,
        description="Optional Content-ID for an inline MIME attachment.",
    )


EmailAttachmentList = Annotated[
    List[EmailAttachmentSource], BeforeValidator(_coerce_json_str_to_list)
]


@dataclass(frozen=True)
class ResolvedPortableFile:
    data: bytes
    filename: str
    mime_type: str


def _resolved_metadata(
    *,
    explicit_filename: Optional[str],
    explicit_mime_type: Optional[str],
    source_filename: Optional[str],
    source_mime_type: Optional[str],
) -> tuple[str, str]:
    filename = explicit_filename or source_filename or "attachment"
    guessed_mime, _ = mimetypes.guess_type(filename)
    mime_type = (
        explicit_mime_type
        or source_mime_type
        or guessed_mime
        or "application/octet-stream"
    )
    return filename, mime_type


def resolve_local_file_source(
    source: PortableFileSource | dict[str, Any],
    *,
    max_bytes: Optional[int] = None,
) -> ResolvedPortableFile:
    """Resolve a workspace ID, server path, or base64 source into exact bytes."""
    if not isinstance(source, PortableFileSource):
        source = PortableFileSource.model_validate(source)
    if source.url:
        raise ValueError(
            "URL sources must be resolved by the caller's SSRF-safe stream."
        )

    source_filename: Optional[str] = None
    source_mime_type: Optional[str] = None

    if source.workspace_file_id:
        storage = get_attachment_storage()
        metadata = storage.get_attachment_metadata(source.workspace_file_id)
        file_path = storage.get_attachment_path(source.workspace_file_id)
        if metadata is None or file_path is None:
            raise UserInputError(
                f"Workspace file ID '{source.workspace_file_id}' is missing or "
                "expired. Download or stage the file again and retry."
            )
        path_obj = Path(file_path)
        source_filename = metadata.get("original_filename") or metadata.get("filename")
        source_mime_type = metadata.get("mime_type")
    elif source.path:
        path_obj = validate_file_path(source.path)
        if not path_obj.exists():
            raise UserInputError(
                f"Server-local file does not exist: {source.path}. Client-local "
                "paths are not visible to a remote MCP server."
            )
        if not path_obj.is_file():
            raise UserInputError(f"Server-local path is not a file: {source.path}")
        source_filename = path_obj.name
    else:
        try:
            data = base64.b64decode(source.base64_content or "", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UserInputError(
                "base64_content must be valid standard RFC 4648 base64."
            ) from exc
        filename, mime_type = _resolved_metadata(
            explicit_filename=source.filename,
            explicit_mime_type=source.mime_type,
            source_filename=None,
            source_mime_type=None,
        )
        if max_bytes is not None and len(data) > max_bytes:
            raise UserInputError(
                f"File '{filename}' exceeds the {max_bytes}-byte limit."
            )
        return ResolvedPortableFile(data, filename, mime_type)

    size = path_obj.stat().st_size
    filename, mime_type = _resolved_metadata(
        explicit_filename=source.filename,
        explicit_mime_type=source.mime_type,
        source_filename=source_filename,
        source_mime_type=source_mime_type,
    )
    if max_bytes is not None and size > max_bytes:
        raise UserInputError(f"File '{filename}' exceeds the {max_bytes}-byte limit.")
    return ResolvedPortableFile(path_obj.read_bytes(), filename, mime_type)
