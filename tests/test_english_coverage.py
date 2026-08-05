# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import re
from unittest.mock import patch

from accounts.account_manager import AccountManager
from accounts.models import AccountCleanupResult, AccountTransferResult
from accounts.session_upload import (
    SessionImportResult,
    ZipSessionUploadError,
    render_zip_import_summary,
    render_zip_upload_error,
)
from handlers.hosting_handlers import hosting_cleanup_result_text
from handlers.transfer_handlers import _transfer_result_text
from handlers.vip_handlers import _current_subscription_text
from localization import EN, localized_payment_error
from payments.payment_system import PaymentSystem


def _has_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def test_english_catalog_has_no_unexpected_chinese():
    contaminated = {
        key for key, value in EN.items()
        if _has_chinese(value.replace("秦屿泊", "")) and key != "language.choose"
    }
    assert not contaminated


def test_english_session_and_cleanup_summaries_are_complete():
    summary = render_zip_import_summary([
        SessionImportResult(True, "one.session", phone="+1"),
        SessionImportResult(False, "two.session"),
        SessionImportResult(False, "three.session", reason="quota_full"),
    ], "en")
    assert "Imported: 1" in summary
    assert "Failed: 1" in summary
    assert "limit reached" in summary
    assert not _has_chinese(summary)

    error = render_zip_upload_error(
        ZipSessionUploadError("too_many_sessions", "internal"), "en"
    )
    assert "up to 25" in error
    assert not _has_chinese(error)

    cleanup = hosting_cleanup_result_text(
        "+1",
        "all",
        AccountCleanupResult(
            status="partial",
            chats_deleted=2,
            contacts_deleted=3,
            errors=["network"],
        ),
        "en",
    )
    assert "Chats removed: 2" in cleanup
    assert "Issues:" in cleanup
    assert not _has_chinese(cleanup)


def test_english_account_results_do_not_use_generic_fallbacks():
    with patch(
        "accounts.account_manager.DataManager.get_user_language",
        return_value="en",
    ), patch.object(AccountManager, "check_access", return_value=False):
        assert AccountManager._start_code_fetch_unlocked(1, "+1") == "❌ No access"
        assert asyncio.run(
            AccountManager._change_hosted_2fa_unlocked(1, "+1", "", "")
        ) == "❌ Send: old-password new-password"
        quota = AccountManager.quota_error_message(1)
        assert "Hosted-account limit reached" in quota
        assert not _has_chinese(quota)


def test_english_transfer_vip_and_payment_results_are_clear():
    transfer = _transfer_result_text(
        AccountTransferResult(False, "source_disconnect_failed", "internal"),
        "en",
    )
    assert transfer == "❌ Session is in use. Try again shortly."
    current = _current_subscription_text(
        {"plan_id": "admin", "quota": None}, "en"
    )
    assert "Admin" in current and "Hosted seats" in current
    assert not _has_chinese(current)

    title = PaymentSystem._subscription_payment_name(
        {"plan_id": "plus", "quota": 12, "addon": 2, "period_days": 90},
        language="en",
    )
    assert title == "🥈 PLUS · Custom seats | 90 days | 12 seats"
    assert localized_payment_error("en", "支付API请求过于频繁，请稍后重试") == (
        "Too many payment requests. Try again later."
    )
    assert not _has_chinese(title)
