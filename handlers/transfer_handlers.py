# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import math
import re
import time

from telethon import TelegramClient, events
from telethon.tl.custom import Button

from accounts.account_manager import AccountManager
from accounts.account_runtime import user_accounts
from handlers.handler_utils import (
    back_button,
    clear_state,
    delete_remembered_flow_messages,
    delete_remembered_start_command,
    forget_flow_message,
    get_state,
    paginate_items,
    remember_flow_message,
    safe_edit,
    set_state,
)
from localization import localized_result, t


TRANSFER_CUSTOM_EMOJI_ID = 5807499888245612254
TRANSFER_STATE_TTL_SECONDS = 10 * 60
TRANSFER_STATE_KEYS = (
    "waiting_account_transfer_target",
    "account_transfer_pending",
    "account_transfer_phone",
    "account_transfer_to_user_id",
    "account_transfer_created_at",
)


def clear_account_transfer_state(user_id: int) -> None:
    clear_state(user_id, *TRANSFER_STATE_KEYS)


def _remaining_text(seconds: int) -> str:
    total_minutes = math.ceil(max(0, int(seconds)) / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return t("transfer.remaining_hours_minutes", hours=hours, minutes=minutes)
    if hours:
        return t("transfer.remaining_hours", hours=hours)
    return t("transfer.remaining_minutes", minutes=minutes)


def _transfer_result_text(result) -> str:
    message_key = getattr(result, "message_key", "")
    if message_key:
        return t(message_key,
            **getattr(result, "message_values", {}),
        )
    keys = {
        "source_not_vip": "transfer.failure_source_access",
        "invalid_phone": "transfer.error.invalid_phone",
        "not_owned": "transfer.failure_not_owned",
        "uploaded_session_not_transferable": "transfer.failure_locked",
        "too_new": "transfer.failure_too_new",
        "same_user": "transfer.self",
        "target_not_vip": "transfer.vip_required",
        "target_quota_full": "transfer.error.target_quota",
        "target_duplicate": "transfer.error.target_duplicate",
        "target_session_exists": "transfer.error.target_session_exists",
        "source_session_missing": "transfer.error.source_session_missing",
        "journal_failed": "transfer.error.journal_failed",
        "metadata_failed": "transfer.error.metadata_failed",
        "rollback_failed": "transfer.error.rollback_failed",
        "source_disconnect_failed": "transfer.error.source_busy",
        "move_failed": "transfer.error.move_failed",
        "target_load_failed": "transfer.error.target_load_failed",
    }
    key = keys.get(getattr(result, "code", ""))
    return t(key) if key else localized_result(getattr(result, "message", "")
    )


async def setup_transfer_handlers(bot: TelegramClient):
    async def render_transfer_accounts(event, page: int = 0):
        user_id = event.sender_id
        current_message = (
            await event.get_message() if hasattr(event, "get_message") else None
        )
        await delete_remembered_flow_messages(
            user_id, preserve_message=current_message
        )
        clear_account_transfer_state(user_id)

        if not AccountManager.check_access(user_id):
            await event.answer(t("common.no_access"), alert=True)
            return

        accounts = user_accounts.get(user_id, {})
        if not accounts:
            await event.answer()
            await safe_edit(event,
                t("accounts.empty"),
                buttons=[[back_button(b"back_to_main")]],
            )
            return

        items, page, max_page = paginate_items(list(accounts.items()), page)
        buttons = []
        for phone, acc_info in items:
            display_phone = acc_info.get("display_phone", phone)
            if AccountManager.is_uploaded_transfer_locked(user_id, phone):
                label = f"📱 {display_phone} ｜ 🔒"
            else:
                remaining = AccountManager.get_account_transfer_remaining_seconds(
                    user_id, phone
                )
                if remaining:
                    label = f"📱 {display_phone} ｜ ⛔️ {t('transfer.locked', remaining=_remaining_text(remaining))}"
                else:
                    label = f"📱 {display_phone} ｜ ✅"
            digits = re.sub(r"\D", "", phone)
            buttons.append([
                Button.inline(label, f"account_transfer_select_{digits}".encode())
            ])

        if max_page > 0:
            nav = []
            if page > 0:
                nav.append(Button.inline(t("common.previous"), f"account_transfer_accounts_{page - 1}".encode()))
            nav.append(Button.inline(f"{page + 1}/{max_page + 1}", f"account_transfer_accounts_{page}".encode()))
            if page < max_page:
                nav.append(Button.inline(t("common.next"), f"account_transfer_accounts_{page + 1}".encode()))
            buttons.append(nav)
        buttons.append([back_button(b"back_to_main")])

        await event.answer()
        await safe_edit(event,
            t("transfer.page"),
            buttons=buttons,
        )

    @bot.on(events.CallbackQuery(pattern=rb"account_transfer_accounts(?:_\d+)?$"))
    async def transfer_accounts(event):
        data = event.data.decode(errors="ignore")
        match = re.fullmatch(r"account_transfer_accounts_(\d+)", data)
        page = int(match.group(1)) if match else 0
        await render_transfer_accounts(event, page)

    @bot.on(events.CallbackQuery(pattern=rb"account_transfer_select_\d+$"))
    async def transfer_select(event):
        user_id = event.sender_id
        if not AccountManager.check_access(user_id):
            await event.answer(t("common.no_access"), alert=True)
            return

        match = re.fullmatch(
            r"account_transfer_select_(\d+)",
            event.data.decode(errors="ignore"),
        )
        if not match:
            await event.answer()
            return
        phone = "+" + match.group(1)
        if phone not in user_accounts.get(user_id, {}):
            await event.answer(t("transfer.not_owned"), alert=True)
            return

        if AccountManager.is_uploaded_transfer_locked(user_id, phone):
            await event.answer(t("transfer.upload_locked"), alert=True)
            return

        remaining = AccountManager.get_account_transfer_remaining_seconds(user_id, phone)
        if remaining:
            await event.answer(
                t("transfer.wait", remaining=_remaining_text(remaining)),
                alert=True,
            )
            return

        set_state(
            user_id,
            waiting_account_transfer_target=True,
            account_transfer_phone=phone,
            account_transfer_created_at=time.time(),
        )
        await event.answer()
        await safe_edit(event,
            t("transfer.target_prompt", phone=phone),
            buttons=[[back_button(b"account_transfer_accounts")]],
            parse_mode="md",
        )

    @bot.on(events.NewMessage)
    async def transfer_target_input(event):
        user_id = event.sender_id
        state = get_state(user_id)
        if not state.get("waiting_account_transfer_target"):
            return
        if event.text and event.text.startswith("/"):
            return

        remember_flow_message(user_id, event)

        created_at = float(state.get("account_transfer_created_at", 0) or 0)
        if not created_at or time.time() - created_at > TRANSFER_STATE_TTL_SECONDS:
            clear_account_transfer_state(user_id)
            forget_flow_message(user_id)
            await event.respond(t("transfer.timeout"))
            return

        text = (event.text or "").strip()
        if not re.fullmatch(r"\d+", text):
            retry_message = await event.respond(t("transfer.invalid_id"))
            remember_flow_message(user_id, retry_message)
            return
        to_user_id = int(text)
        phone = state.get("account_transfer_phone") or ""
        validation = AccountManager.validate_account_transfer(
            user_id, phone, to_user_id
        )
        if not validation.ok:
            retry_message = await event.respond(
                _transfer_result_text(validation)
            )
            remember_flow_message(user_id, retry_message)
            return

        set_state(
            user_id,
            account_transfer_pending=True,
            account_transfer_phone=validation.phone,
            account_transfer_to_user_id=to_user_id,
            account_transfer_created_at=time.time(),
        )
        confirmation_message = await event.respond(
            t("transfer.confirm", phone=validation.phone, target=to_user_id),
            buttons=[[
                Button.inline(t("transfer.confirm_button"), b"account_transfer_confirm"),
                Button.inline(t("transfer.cancel"), b"account_transfer_cancel"),
            ]],
            parse_mode="md",
        )
        remember_flow_message(user_id, confirmation_message)

    @bot.on(events.CallbackQuery(pattern=b"account_transfer_confirm"))
    async def transfer_confirm(event):
        user_id = event.sender_id
        state = get_state(user_id)
        if not state.get("account_transfer_pending"):
            await event.answer(t("transfer.none_pending"), alert=True)
            return

        created_at = float(state.get("account_transfer_created_at", 0) or 0)
        if not created_at or time.time() - created_at > TRANSFER_STATE_TTL_SECONDS:
            clear_account_transfer_state(user_id)
            forget_flow_message(user_id)
            await event.answer(t("transfer.timeout"), alert=True)
            await safe_edit(event,
                t("transfer.timeout"),
                buttons=[[back_button(b"account_transfer_accounts")]],
            )
            return

        phone = state.get("account_transfer_phone") or ""
        to_user_id = int(state.get("account_transfer_to_user_id", 0) or 0)
        clear_account_transfer_state(user_id)
        forget_flow_message(user_id)
        await event.answer(t("transfer.in_progress"))

        result = await AccountManager.transfer_account(user_id, phone, to_user_id)
        if result.ok:
            await delete_remembered_start_command(user_id)
        await safe_edit(event,
            _transfer_result_text(result),
            buttons=None if result.ok else [[back_button(b"back_to_main")]],
            parse_mode="md",
        )

    @bot.on(events.CallbackQuery(pattern=b"account_transfer_cancel"))
    async def transfer_cancel(event):
        clear_account_transfer_state(event.sender_id)
        forget_flow_message(event.sender_id)
        await render_transfer_accounts(event, 0)
