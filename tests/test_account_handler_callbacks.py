# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from accounts.models import SessionCleanupResult
from accounts.session_upload import ZipSessionUploadError
from handlers import account_handlers
from handlers.handler_utils import clear_state, get_state, set_state


def run(awaitable):
    return asyncio.run(awaitable)


def register(handler_bot):
    run(account_handlers.setup_account_handlers(handler_bot))
    return handler_bot


@pytest.fixture(autouse=True)
def _state():
    clear_state(1001)
    yield
    clear_state(1001)


@pytest.mark.parametrize("case", ["denied", "busy", "success"])
def test_add_account_paths(handler_bot, event_factory, case):
    bot = register(handler_bot)
    event = event_factory(data=b"add_account")
    cleanup = SessionCleanupResult(ok=case != "busy", action="cleanup", reason="busy" if case == "busy" else "")
    with patch(
        "handlers.account_handlers.require_access", new=AsyncMock(return_value=case != "denied")
    ), patch(
        "handlers.account_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=cleanup)
    ), patch("handlers.account_handlers.AccountManager.cleanup_stale_pending_sessions"), patch(
        "handlers.account_handlers.safe_edit", new=AsyncMock()
    ) as edit:
        run(bot.find("add_account_callback")(event))
    if case == "success":
        edit.assert_awaited_once()
    else:
        edit.assert_not_awaited()


def test_back_to_methods_regular_and_qr(handler_bot, event_factory):
    bot = register(handler_bot)
    ok = SessionCleanupResult(ok=True, action="cleanup", reason="")
    regular = event_factory(data=b"back_to_add_methods", get_message=AsyncMock(return_value="m"))
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=ok)
    ), patch("handlers.account_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("back_to_add_methods_callback")(regular))
    edit.assert_awaited_once()

    set_state(1001, qr_login=True, qr_flow_id="f")
    qr = event_factory(data=b"back_to_add_methods", get_message=AsyncMock())
    bot.send_message = AsyncMock()
    with patch(
        "handlers.account_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=ok)
    ):
        run(bot.find("back_to_add_methods_callback")(qr))
    qr.get_message.assert_not_awaited()
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize("reason", ["qr_message_delete_failed", "busy"])
def test_back_to_methods_cleanup_failure(handler_bot, event_factory, reason):
    bot = register(handler_bot)
    event = event_factory(data=b"back_to_add_methods", get_message=AsyncMock(return_value=None))
    result = SessionCleanupResult(ok=False, action="cleanup", reason=reason)
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=result)
    ):
        run(bot.find("back_to_add_methods_callback")(event))
    assert event.answer.await_args.kwargs == {"alert": True}


@pytest.mark.parametrize("case", ["denied", "busy", "safe_edit", "message"])
def test_add_phone_paths(handler_bot, event_factory, case):
    bot = register(handler_bot)
    fallback = SimpleNamespace(id=2)
    event = event_factory(
        data=b"add_account_phone", get_message=AsyncMock(return_value=fallback)
    )
    cleanup = SessionCleanupResult(ok=case != "busy", action="cleanup", reason="busy" if case == "busy" else "")
    rendered = SimpleNamespace(id=1) if case == "safe_edit" else None
    with patch(
        "handlers.account_handlers.require_access", new=AsyncMock(return_value=case != "denied")
    ), patch(
        "handlers.account_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=cleanup)
    ), patch("handlers.account_handlers.AccountManager.cleanup_stale_pending_sessions"), patch(
        "handlers.account_handlers.safe_edit", new=AsyncMock(return_value=rendered)
    ):
        run(bot.find("add_account_phone_callback")(event))
    if case in {"safe_edit", "message"}:
        assert get_state(1001)["phone_prompt_message"] is (rendered or fallback)


def test_qr_creation_success(handler_bot, event_factory):
    bot = register(handler_bot)
    bot.send_message = AsyncMock()
    status = SimpleNamespace(id=1)
    qr_login = SimpleNamespace(url="tg://login")
    client = SimpleNamespace(
        connect=AsyncMock(),
        qr_login=AsyncMock(return_value=qr_login),
    )
    captured = []

    def consume(coro):
        captured.append(coro)
        coro.close()
        return "task"

    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SessionCleanupResult(ok=True, action="cleanup", reason="")),
    ), patch("handlers.account_handlers.AccountManager.cleanup_stale_pending_sessions"), patch(
        "handlers.account_handlers.AccountManager.create_qr_client", new=AsyncMock(return_value=client)
    ), patch(
        "handlers.account_handlers.AccountManager._client_session_path", return_value="pending.session"
    ), patch("handlers.account_handlers.safe_edit", new=AsyncMock(return_value=status)), patch(
        "handlers.account_handlers.build_qr_image", return_value=b"image"
    ), patch(
        "handlers.account_handlers.edit_status_or_send", new=AsyncMock(return_value=status)
    ), patch("handlers.account_handlers.asyncio.create_task", side_effect=consume):
        run(bot.find("add_account_qr_callback")(event_factory(data=b"add_account_qr")))
    state = get_state(1001)
    assert state["qr_phase"] == "waiting"
    assert state["qr_wait_task"] == "task"
    assert captured


@pytest.mark.parametrize("replacement", [SimpleNamespace(id=3), None])
def test_qr_creation_failure(handler_bot, event_factory, replacement):
    bot = register(handler_bot)
    bot.send_message = AsyncMock()
    status = SimpleNamespace(id=1)
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SessionCleanupResult(ok=True, action="cleanup", reason="")),
    ), patch("handlers.account_handlers.AccountManager.cleanup_stale_pending_sessions"), patch(
        "handlers.account_handlers.AccountManager.create_qr_client",
        new=AsyncMock(side_effect=RuntimeError("qr failed")),
    ), patch("handlers.account_handlers.safe_edit", new=AsyncMock(return_value=status)), patch(
        "handlers.account_handlers.AccountManager.cleanup_pending_login_state", new=AsyncMock()
    ), patch(
        "handlers.account_handlers.delete_qr_and_send_new", new=AsyncMock(return_value=replacement)
    ):
        run(bot.find("add_account_qr_callback")(event_factory(data=b"add_account_qr")))
    if replacement is None:
        assert get_state(1001)["qr_message_delete_failed"] is True


@pytest.mark.parametrize("case", ["denied", "busy", "success"])
def test_upload_session_prompt_paths(handler_bot, event_factory, case):
    bot = register(handler_bot)
    event = event_factory(data=b"upload_session")
    cleanup = SessionCleanupResult(ok=case != "busy", action="cleanup", reason="busy" if case == "busy" else "")
    with patch(
        "handlers.account_handlers.require_access", new=AsyncMock(return_value=case != "denied")
    ), patch(
        "handlers.account_handlers.cancel_pending_login_flow", new=AsyncMock(return_value=cleanup)
    ), patch("handlers.account_handlers.safe_edit", new=AsyncMock()) as edit:
        run(bot.find("upload_session_callback")(event))
    if case == "success":
        edit.assert_awaited_once()
    else:
        edit.assert_not_awaited()


def test_delete_qr_and_rollback_helpers():
    bot = SimpleNamespace(send_message=AsyncMock(return_value="new"))
    with patch("handlers.account_handlers.delete_qr_message_strict", new=AsyncMock(return_value=False)):
        assert run(account_handlers.delete_qr_and_send_new(bot, 1, "old", "text")) is None
    with patch("handlers.account_handlers.delete_qr_message_strict", new=AsyncMock(return_value=True)):
        assert run(account_handlers.delete_qr_and_send_new(bot, 1, "old", "text")) == "new"

    for result in ("🗑 removed", "failed"):
        with patch(
            "handlers.account_handlers.AccountManager.delete_account",
            new=AsyncMock(return_value=result),
        ):
            run(account_handlers._rollback_promoted_qr_account(1, "+1"))
    with patch(
        "handlers.account_handlers.AccountManager.delete_account",
        new=AsyncMock(side_effect=RuntimeError),
    ):
        run(account_handlers._rollback_promoted_qr_account(1, "+1"))


def upload_event(event_factory, name="sample.session", size=10):
    return event_factory(
        text="",
        file=SimpleNamespace(name=name, size=size),
        download_media=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("install_result", "accounts"),
    [
        ((None, "+1", True, ""), {"+1": {"display_phone": "+1", "anti_login": True}}),
        ((None, "+1", True, ""), {}),
        ((None, "", False, "invalid"), {}),
        ((None, "", False, "existing_session_busy"), {}),
        ((None, "", False, "replace_failed_restored"), {}),
        ((None, "", False, "replace_failed"), {}),
        ((None, "", False, "quota_full"), {}),
        ((None, "", False, "other"), {}),
    ],
)
def test_single_session_upload_results(handler_bot, event_factory, install_result, accounts):
    bot = register(handler_bot)
    event = upload_event(event_factory)
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SessionCleanupResult(True, "cleanup", "")),
    ), patch(
        "handlers.account_handlers.AccountManager.install_uploaded_session",
        new=AsyncMock(return_value=install_result),
    ), patch(
        "handlers.account_handlers.AccountManager.get_user_accounts", return_value=accounts
    ), patch(
        "handlers.account_handlers.AccountManager.quota_error_message", return_value="quota"
    ):
        run(bot.find("handle_account_messages")(event))
    assert event.respond.await_count >= 2
    event.delete.assert_awaited()


def test_upload_guards_size_cleanup_and_exception(handler_bot, event_factory):
    bot = register(handler_bot)
    denied = upload_event(event_factory)
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=False)):
        run(bot.find("handle_account_messages")(denied))
    denied.download_media.assert_not_awaited()
    denied.delete.assert_awaited_once()

    too_large = upload_event(event_factory, size=100_000)
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)):
        run(bot.find("handle_account_messages")(too_large))
    too_large.download_media.assert_not_awaited()
    too_large.delete.assert_awaited_once()

    busy = upload_event(event_factory)
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SessionCleanupResult(False, "cleanup", "busy")),
    ):
        run(bot.find("handle_account_messages")(busy))
    busy.download_media.assert_not_awaited()
    busy.delete.assert_awaited_once()

    failed = upload_event(event_factory)
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SessionCleanupResult(True, "cleanup", "")),
    ), patch(
        "handlers.account_handlers.AccountManager.install_uploaded_session",
        new=AsyncMock(side_effect=RuntimeError("inspect failed")),
    ):
        run(bot.find("handle_account_messages")(failed))
    assert "inspect failed" in failed.respond.await_args.args[0]


@pytest.mark.parametrize("discovery", [ZipSessionUploadError("broken", "broken"), []])
def test_zip_discovery_results(handler_bot, event_factory, discovery):
    bot = register(handler_bot)
    event = upload_event(event_factory, "bundle.zip")
    effect = discovery if isinstance(discovery, Exception) else None
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SessionCleanupResult(True, "cleanup", "")),
    ), patch(
        "handlers.account_handlers.find_zip_session_entries",
        side_effect=effect,
        return_value=discovery if not effect else None,
    ):
        run(bot.find("handle_account_messages")(event))
    assert event.respond.await_count >= 2


def test_zip_entry_success_and_failures(handler_bot, event_factory):
    bot = register(handler_bot)
    entries = [
        SimpleNamespace(filename="ok.session"),
        SimpleNamespace(filename="bad.session"),
        SimpleNamespace(filename="boom.session"),
    ]
    event = upload_event(event_factory, "bundle.zip")

    def extract(_archive, entry):
        if entry.filename == "bad.session":
            raise ZipSessionUploadError("bad", "bad")
        return f"missing-{entry.filename}"

    install = AsyncMock(side_effect=[(None, "+1", True, ""), RuntimeError("boom")])
    with patch("handlers.account_handlers.require_access", new=AsyncMock(return_value=True)), patch(
        "handlers.account_handlers.cancel_pending_login_flow",
        new=AsyncMock(return_value=SessionCleanupResult(True, "cleanup", "")),
    ), patch("handlers.account_handlers.find_zip_session_entries", return_value=entries), patch(
        "handlers.account_handlers.extract_zip_session_entry", side_effect=extract
    ), patch(
        "handlers.account_handlers.AccountManager.install_uploaded_session", new=install
    ):
        run(bot.find("handle_account_messages")(event))
    assert event.respond.await_count >= 2
