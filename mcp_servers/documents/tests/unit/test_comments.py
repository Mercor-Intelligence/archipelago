"""Tests for comments tool."""

from pathlib import Path

import pytest
from tools.comments import comments


class _FakeElement:
    def __init__(self) -> None:
        self.removed = False

    def getparent(self):
        return self

    def remove(self, element) -> None:
        self.removed = True


class _FakeRun:
    def __init__(self) -> None:
        self.text = "run text"


class _FakeParagraph:
    def __init__(self, runs=None) -> None:
        self._runs = runs or []

    @property
    def runs(self):
        return self._runs

    def add_run(self, text=""):
        run = _FakeRun()
        run.text = text
        self._runs.append(run)
        return run


class _FakeCell:
    def __init__(self) -> None:
        self.paragraphs = [_FakeParagraph()]


class _FakeComment:
    def __init__(self, comment_id: int, author: str = "Author", text: str = "Text"):
        self.comment_id = comment_id
        self.author = author
        self.text = text
        self.timestamp = None
        self._element = _FakeElement()


class _FakeDocument:
    def __init__(self, comments_list=None) -> None:
        self.comments = comments_list or []
        self.saved_path = None

    def add_comment(self, runs, text: str, author: str):
        new_id = len(self.comments) + 1
        comment = _FakeComment(new_id, author=author, text=text)
        self.comments.append(comment)
        return comment

    def save(self, path: str) -> None:
        self.saved_path = path


class _FakeDocumentNoComments:
    """Document without comments attribute (simulates old python-docx)."""

    def __init__(self) -> None:
        self.saved_path = None

    def save(self, path: str) -> None:
        self.saved_path = path


def _touch_doc(mock_docs_root: str, filename: str) -> str:
    full_path = Path(mock_docs_root) / filename
    full_path.write_bytes(b"")
    return f"/{filename}"


@pytest.mark.asyncio
async def test_comments_read_success(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_read.docx")
    fake_doc = _FakeDocument(comments_list=[_FakeComment(3, "Alice", "Hello")])

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)

    result = await comments(file_path, action="read")
    assert "Status: success" in result
    assert "Comment Count: 1" in result


@pytest.mark.asyncio
async def test_comments_read_empty(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_empty.docx")
    fake_doc = _FakeDocument(comments_list=[])

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)

    result = await comments(file_path, action="read")
    assert "Status: success" in result
    assert "Comment Count: 0" in result


@pytest.mark.asyncio
async def test_comments_add_success(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_add.docx")
    fake_doc = _FakeDocument()

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)
    monkeypatch.setattr("tools.comments.parse_identifier", lambda _: {"id": "x"})
    monkeypatch.setattr(
        "tools.comments.resolve_target",
        lambda _doc, _parsed: (None, _FakeRun(), "run"),
    )

    result = await comments(
        file_path,
        action="add",
        identifier="body.p.0",
        text="Test comment",
        author="Bob",
    )
    assert "Status: success" in result
    assert "Comment ID: 1" in result


@pytest.mark.asyncio
async def test_comments_add_to_paragraph(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_add_para.docx")
    fake_doc = _FakeDocument()
    fake_para = _FakeParagraph(runs=[_FakeRun()])

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)
    monkeypatch.setattr("tools.comments.parse_identifier", lambda _: {"id": "x"})
    monkeypatch.setattr(
        "tools.comments.resolve_target",
        lambda _doc, _parsed: (None, fake_para, "paragraph"),
    )

    result = await comments(
        file_path, action="add", identifier="body.p.0", text="Comment", author="Test"
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_comments_add_to_paragraph_no_runs(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_add_para_no_runs.docx")
    fake_doc = _FakeDocument()
    fake_para = _FakeParagraph(runs=[])

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)
    monkeypatch.setattr("tools.comments.parse_identifier", lambda _: {"id": "x"})
    monkeypatch.setattr(
        "tools.comments.resolve_target",
        lambda _doc, _parsed: (None, fake_para, "paragraph"),
    )

    result = await comments(
        file_path, action="add", identifier="body.p.0", text="Comment", author="Test"
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_comments_add_to_cell(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_add_cell.docx")
    fake_doc = _FakeDocument()
    fake_cell = _FakeCell()

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)
    monkeypatch.setattr("tools.comments.parse_identifier", lambda _: {"id": "x"})
    monkeypatch.setattr(
        "tools.comments.resolve_target",
        lambda _doc, _parsed: (None, fake_cell, "cell"),
    )

    result = await comments(
        file_path,
        action="add",
        identifier="body.tbl.0.r.0.c.0",
        text="Comment",
        author="Test",
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_comments_add_unsupported_type(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_add_unsupported.docx")
    fake_doc = _FakeDocument()

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)
    monkeypatch.setattr("tools.comments.parse_identifier", lambda _: {"id": "x"})
    monkeypatch.setattr(
        "tools.comments.resolve_target",
        lambda _doc, _parsed: (None, object(), "table"),
    )

    result = await comments(
        file_path, action="add", identifier="body.tbl.0", text="Comment", author="Test"
    )
    assert "Cannot add comment to target type" in result


@pytest.mark.asyncio
async def test_comments_delete_success(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_delete.docx")
    target_comment = _FakeComment(7, "Eve", "Remove me")
    fake_doc = _FakeDocument(comments_list=[target_comment])

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)

    result = await comments(file_path, action="delete", comment_id=7)
    assert "Status: success" in result
    assert "Comment ID: 7" in result


@pytest.mark.asyncio
async def test_comments_delete_not_found(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_delete_notfound.docx")
    fake_doc = _FakeDocument(comments_list=[_FakeComment(1, "A", "B")])

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)

    result = await comments(file_path, action="delete", comment_id=999)
    assert "not found" in result


@pytest.mark.asyncio
async def test_comments_validation_empty_path(mock_docs_root):
    result = await comments("", action="read")
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_comments_validation_no_slash(mock_docs_root):
    result = await comments("test.docx", action="read")
    assert "must start with /" in result


@pytest.mark.asyncio
async def test_comments_validation_wrong_extension(mock_docs_root):
    result = await comments("/test.txt", action="read")
    assert "must end with .docx" in result


@pytest.mark.asyncio
async def test_comments_validation_invalid_action(mock_docs_root):
    _touch_doc(mock_docs_root, "comments_action.docx")
    result = await comments("/comments_action.docx", action="unknown")
    assert "Invalid action" in result


@pytest.mark.asyncio
async def test_comments_validation_add_no_identifier(mock_docs_root):
    _touch_doc(mock_docs_root, "comments_no_id.docx")
    result = await comments("/comments_no_id.docx", action="add", text="test")
    assert "Identifier is required" in result


@pytest.mark.asyncio
async def test_comments_validation_add_empty_identifier(mock_docs_root):
    _touch_doc(mock_docs_root, "comments_empty_id.docx")
    result = await comments(
        "/comments_empty_id.docx", action="add", identifier="", text="test"
    )
    assert "Identifier is required" in result


@pytest.mark.asyncio
async def test_comments_validation_add_no_text(mock_docs_root):
    _touch_doc(mock_docs_root, "comments_no_text.docx")
    result = await comments(
        "/comments_no_text.docx", action="add", identifier="body.p.0"
    )
    assert "Text is required" in result


@pytest.mark.asyncio
async def test_comments_validation_add_empty_text(mock_docs_root):
    _touch_doc(mock_docs_root, "comments_empty_text.docx")
    result = await comments(
        "/comments_empty_text.docx", action="add", identifier="body.p.0", text=""
    )
    assert "Text is required" in result


@pytest.mark.asyncio
async def test_comments_validation_delete_no_id(mock_docs_root):
    _touch_doc(mock_docs_root, "comments_delete_no_id.docx")
    result = await comments("/comments_delete_no_id.docx", action="delete")
    assert "comment_id is required" in result


@pytest.mark.asyncio
async def test_comments_file_not_found(mock_docs_root):
    result = await comments("/nonexistent.docx", action="read")
    assert "File not found" in result


@pytest.mark.asyncio
async def test_comments_not_a_file(mock_docs_root):
    dir_path = Path(mock_docs_root) / "not_a_file.docx"
    dir_path.mkdir()
    result = await comments("/not_a_file.docx", action="read")
    assert "Not a file" in result


@pytest.mark.asyncio
async def test_comments_not_supported(mock_docs_root, monkeypatch):
    file_path = _touch_doc(mock_docs_root, "comments_not_supported.docx")
    fake_doc = _FakeDocumentNoComments()

    monkeypatch.setattr("tools.comments.Document", lambda _: fake_doc)

    result = await comments(file_path, action="read")
    assert "Comments not supported" in result
