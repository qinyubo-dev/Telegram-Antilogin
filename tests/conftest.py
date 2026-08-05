# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="anti-login-tests-"))
os.environ["ANTI_LOGIN_DATA_ROOT"] = str(TEST_DATA_ROOT)

# Application modules import ``config`` at module-import time.  Always provide a
# credential-free module during tests so a developer's local secrets are never
# read and a clean checkout does not need an ignored config.py file.
TEST_CONFIG = types.ModuleType("config")
TEST_CONFIG.__file__ = "<pytest-safe-config>"
TEST_CONFIG.DATA_ROOT = str(TEST_DATA_ROOT)
sys.modules["config"] = TEST_CONFIG

_runtime_snapshot = {}


def _runtime_files() -> list[Path]:
    candidates = [
        PROJECT_ROOT / "user_data.json",
        PROJECT_ROOT / "bot.session",
        PROJECT_ROOT / "bot.session-journal",
    ]
    for directory in ("sessions", "storage", "logs"):
        root = PROJECT_ROOT / directory
        if root.exists():
            candidates.extend(
                path
                for path in root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            )
    return sorted(set(candidates))


def _fingerprint(path: Path) -> tuple[int, int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, digest.hexdigest()


def _snapshot_runtime() -> dict[str, tuple[int, int, str]]:
    return {
        str(path.relative_to(PROJECT_ROOT)): _fingerprint(path)
        for path in _runtime_files()
        if path.exists()
    }


def pytest_sessionstart(session) -> None:
    global _runtime_snapshot
    _runtime_snapshot = _snapshot_runtime()


def pytest_sessionfinish(session, exitstatus) -> None:
    current = _snapshot_runtime()
    if current != _runtime_snapshot:
        before = set(_runtime_snapshot)
        after = set(current)
        changed = sorted(
            path for path in before & after
            if _runtime_snapshot[path] != current[path]
        )
        details = [
            *(f"created: {path}" for path in sorted(after - before)),
            *(f"removed: {path}" for path in sorted(before - after)),
            *(f"changed: {path}" for path in changed),
        ]
        sys.stderr.write(
            "\nTests modified real runtime files:\n"
            + "\n".join(f"- {detail}" for detail in details)
            + "\n"
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def _hosted_sessions_ready():
    from accounts import account_runtime

    account_runtime.mark_ready()
    yield
    account_runtime.mark_not_ready()


class RegisteredHandlerBot:
    """Small Telethon-compatible handler registry used by offline handler tests."""

    def __init__(self):
        self.handlers = []

    def on(self, event_builder):
        def register(callback):
            self.handlers.append((event_builder, callback))
            return callback

        return register

    def find(self, callback_name: str):
        matches = [callback for _, callback in self.handlers if callback.__name__ == callback_name]
        if len(matches) != 1:
            raise LookupError(
                f"expected one handler named {callback_name!r}, found {len(matches)}"
            )
        return matches[0]


class MutableClock:
    def __init__(self, value: float = 1_700_000_000.0):
        self.value = float(value)
        self.sleeps = []

    def time(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.advance(seconds)


@pytest.fixture
def handler_bot():
    return RegisteredHandlerBot()


@pytest.fixture
def event_factory():
    def build(**values):
        defaults = {
            "sender_id": 1001,
            "text": "",
            "data": b"",
            "respond": AsyncMock(),
            "edit": AsyncMock(),
            "answer": AsyncMock(),
            "delete": AsyncMock(),
        }
        defaults.update(values)
        return SimpleNamespace(**defaults)

    return build


@pytest.fixture
def mutable_clock():
    return MutableClock()
