# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telethon.errors import AccessTokenInvalidError

from accounts import account_runtime
from accounts.account_session_files import safe_remove_session_files, session_related_paths
from accounts.session_upload import (
    SESSION_UPLOAD_MAX_BYTES,
    ZIP_MAX_SESSION_FILES,
    SessionImportResult,
    ZipSessionUploadError,
    extract_zip_session_entry,
    find_zip_session_entries,
    is_upload_size_allowed,
    render_zip_import_summary,
    safe_archive_label,
    upload_size_limit,
)


def write_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)


def test_session_related_paths_and_empty_input():
    assert session_related_paths("") == []
    assert session_related_paths("a.session") == [
        "a.session",
        "a.session-journal",
        "a.session-shm",
        "a.session-wal",
    ]


def test_safe_remove_session_files_removes_every_sidecar(tmp_path):
    session = tmp_path / "account.session"
    paths = [Path(item) for item in session_related_paths(str(session))]
    for path in paths:
        path.write_text("runtime", encoding="utf-8")

    safe_remove_session_files(str(session))

    assert all(not path.exists() for path in paths)


def test_safe_remove_session_files_is_idempotent_and_best_effort(tmp_path):
    session = str(tmp_path / "account.session")
    Path(session).write_text("runtime", encoding="utf-8")
    with patch("accounts.account_session_files.os.remove", side_effect=PermissionError):
        safe_remove_session_files(session)
    safe_remove_session_files("")
    assert Path(session).exists()


def test_find_zip_session_entries_preserves_order_and_ignores_other_files(tmp_path):
    archive = tmp_path / "sessions.zip"
    write_zip(
        archive,
        [("readme.txt", b"x"), ("nested/one.SESSION", b"1"), ("two.session", b"2")],
    )
    assert [item.filename for item in find_zip_session_entries(str(archive))] == [
        "nested/one.SESSION",
        "two.session",
    ]


def test_find_zip_session_entries_rejects_invalid_input(tmp_path):
    for index, contents in enumerate([b"not a zip", None]):
        archive = tmp_path / f"invalid-{index}.zip"
        if contents is not None:
            archive.write_bytes(contents)
        with pytest.raises(ZipSessionUploadError) as captured:
            find_zip_session_entries(str(archive))
        assert captured.value.code == "invalid_zip"


def test_find_zip_session_entries_rejects_empty_and_excessive_archives(tmp_path):
    empty = tmp_path / "empty.zip"
    write_zip(empty, [("readme.txt", b"x")])
    with pytest.raises(ZipSessionUploadError) as captured:
        find_zip_session_entries(str(empty))
    assert captured.value.code == "no_session"

    excessive = tmp_path / "many.zip"
    write_zip(
        excessive,
        [(f"{index}.session", b"x") for index in range(ZIP_MAX_SESSION_FILES + 1)],
    )
    with pytest.raises(ZipSessionUploadError) as captured:
        find_zip_session_entries(str(excessive))
    assert captured.value.code == "too_many_sessions"


def test_extract_zip_session_entry_streams_to_random_temp_file(tmp_path):
    archive = tmp_path / "sessions.zip"
    payload = b"sqlite" * 100
    write_zip(archive, [("../../unsafe.session", payload)])
    entry = find_zip_session_entries(str(archive))[0]

    output = extract_zip_session_entry(str(archive), entry)
    try:
        assert Path(output).suffix == ".session"
        assert Path(output).read_bytes() == payload
        assert "unsafe" not in Path(output).name
    finally:
        Path(output).unlink(missing_ok=True)


def test_extract_zip_session_entry_rejects_encryption_and_declared_size():
    encrypted = SimpleNamespace(flag_bits=1, file_size=1)
    with pytest.raises(ZipSessionUploadError) as captured:
        extract_zip_session_entry("unused.zip", encrypted)
    assert captured.value.code == "encrypted"

    oversized = SimpleNamespace(flag_bits=0, file_size=SESSION_UPLOAD_MAX_BYTES + 1)
    with pytest.raises(ZipSessionUploadError) as captured:
        extract_zip_session_entry("unused.zip", oversized)
    assert captured.value.code == "session_too_large"


def test_extract_zip_session_entry_enforces_stream_limit_and_removes_partial_file(tmp_path):
    class Source:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            self.calls += 1
            return b"x" * 8192 if self.calls <= 6 else b""

    class Archive:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def open(self, entry, mode):
            return Source()

    temp_file = tmp_path / "partial.session"
    with patch("accounts.session_upload.tempfile.mkstemp") as mkstemp, patch(
        "accounts.session_upload.zipfile.ZipFile", return_value=Archive()
    ):
        descriptor = os.open(temp_file, os.O_CREAT | os.O_WRONLY)
        mkstemp.return_value = (descriptor, str(temp_file))
        entry = SimpleNamespace(flag_bits=0, file_size=0)
        with pytest.raises(ZipSessionUploadError) as captured:
            extract_zip_session_entry("archive.zip", entry)
    assert captured.value.code == "session_too_large"
    assert not temp_file.exists()


def test_extract_zip_session_entry_wraps_io_failures_and_cleans_temp(tmp_path):
    temp_file = tmp_path / "failed.session"
    with patch("accounts.session_upload.tempfile.mkstemp") as mkstemp, patch(
        "accounts.session_upload.zipfile.ZipFile", side_effect=OSError("disk")
    ):
        descriptor = os.open(temp_file, os.O_CREAT | os.O_WRONLY)
        mkstemp.return_value = (descriptor, str(temp_file))
        entry = SimpleNamespace(flag_bits=0, file_size=0)
        with pytest.raises(ZipSessionUploadError) as captured:
            extract_zip_session_entry("archive.zip", entry)
    assert captured.value.code == "extract_failed"
    assert not temp_file.exists()


def test_upload_size_limit():
    cases = [
        ("x.session", SESSION_UPLOAD_MAX_BYTES),
        ("X.SESSION", SESSION_UPLOAD_MAX_BYTES),
        ("x.zip", 200 * 1024),
        ("x.txt", None),
        ("", None),
    ]
    for name, expected in cases:
        assert upload_size_limit(name) == expected


def test_is_upload_size_allowed():
    cases = [
        ("x.session", 0, True),
        ("x.session", SESSION_UPLOAD_MAX_BYTES, True),
        ("x.session", SESSION_UPLOAD_MAX_BYTES + 1, False),
        ("x.zip", 200 * 1024, True),
        ("x.zip", -1, False),
        ("x.txt", 1, False),
    ]
    for name, size, allowed in cases:
        assert is_upload_size_allowed(name, size) is allowed


def test_safe_archive_label_removes_lines_and_bounds_length():
    assert "\n" not in safe_archive_label("one\r\ntwo")
    assert safe_archive_label("", 10)
    assert len(safe_archive_label("x" * 100, 20)) == 20


def test_render_zip_import_summary_groups_success_failures_and_quota():
    text = render_zip_import_summary(
        [
            SessionImportResult(True, "one", phone="+100"),
            SessionImportResult(False, "bad", reason="invalid"),
            SessionImportResult(False, "later", reason="quota_full"),
        ]
    )
    assert "+100" in text
    assert "bad" in text
    assert "later" in text
    assert text.index("+100") < text.index("bad") < text.index("later")


def test_runtime_cancel_task_handles_missing_done_and_running_tasks():
    async def exercise():
        runtime = account_runtime.AccountRuntime()
        await runtime.cancel_task(runtime.client_tasks, "missing")

        done = asyncio.create_task(asyncio.sleep(0))
        await done
        runtime.client_tasks["done"] = done
        await runtime.cancel_task(runtime.client_tasks, "done")
        assert runtime.client_tasks == {}

        running = asyncio.create_task(asyncio.sleep(60))
        runtime.client_tasks["running"] = running
        await runtime.cancel_task(runtime.client_tasks, "running")
        assert running.cancelled()

    asyncio.run(exercise())


def test_runtime_fatal_classification_and_raise_preserves_original():
    original = AccessTokenInvalidError(request=None)
    assert account_runtime.is_notify_bot_fatal_error(original)
    assert not account_runtime.is_notify_bot_fatal_error(RuntimeError())
    account_runtime.set_notify_bot(object())
    with pytest.raises(account_runtime.NotifyBotFatalError) as captured:
        account_runtime.raise_notify_bot_fatal(original, "fatal context")
    assert captured.value.original_error is original
    assert account_runtime.get_notify_bot_health().status == "fatal"


def test_runtime_raise_existing_canonical_error_is_idempotent():
    original = RuntimeError("root")
    canonical = account_runtime.NotifyBotFatalError("already", original)
    account_runtime.set_notify_bot(object())
    with pytest.raises(account_runtime.NotifyBotFatalError) as captured:
        account_runtime.raise_notify_bot_fatal(canonical, "ignored")
    assert captured.value is canonical
    assert account_runtime.get_notify_bot_health().error_type == "RuntimeError"


def test_runtime_waiter_returns_published_fatal_health():
    async def exercise():
        account_runtime.set_notify_bot(object())
        waiter = asyncio.create_task(account_runtime.wait_notify_bot_fatal())
        await asyncio.sleep(0)
        expected = account_runtime.mark_notify_bot_fatal(RuntimeError("fatal"))
        assert await waiter == expected

    asyncio.run(exercise())
