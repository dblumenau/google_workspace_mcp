import base64

import pytest

import core.attachment_storage as attachment_storage
import core.file_sources as file_sources
from core.attachment_storage import AttachmentStorage
from core.file_sources import PortableFileSource, resolve_local_file_source
from core.utils import UserInputError


def test_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one"):
        PortableFileSource()
    with pytest.raises(ValueError, match="exactly one"):
        PortableFileSource(url="https://example.test/a", base64_content="YQ==")


def test_resolves_standard_base64_and_metadata():
    resolved = resolve_local_file_source(
        PortableFileSource(
            base64_content=base64.b64encode(b"hello").decode(),
            filename="hello.txt",
        )
    )
    assert resolved.data == b"hello"
    assert resolved.mime_type == "text/plain"


def test_invalid_base64_is_user_input_error():
    with pytest.raises(UserInputError, match="valid standard"):
        resolve_local_file_source(
            PortableFileSource(base64_content="not base64", filename="bad.bin")
        )


def test_workspace_file_id_uses_original_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(attachment_storage, "STORAGE_DIR", tmp_path)
    storage = AttachmentStorage()
    saved = storage.save_attachment(
        base64.urlsafe_b64encode(b"workspace bytes").decode(),
        filename="report.pdf",
        mime_type="application/pdf",
    )
    monkeypatch.setattr(file_sources, "get_attachment_storage", lambda: storage)

    resolved = resolve_local_file_source(
        PortableFileSource(workspace_file_id=saved.file_id)
    )
    assert resolved.data == b"workspace bytes"
    assert resolved.filename == "report.pdf"
    assert resolved.mime_type == "application/pdf"


def test_expired_workspace_file_id_has_retry_guidance(monkeypatch):
    storage = AttachmentStorage()
    monkeypatch.setattr(file_sources, "get_attachment_storage", lambda: storage)
    with pytest.raises(UserInputError, match="Download or stage the file again"):
        resolve_local_file_source(PortableFileSource(workspace_file_id="missing-id"))
