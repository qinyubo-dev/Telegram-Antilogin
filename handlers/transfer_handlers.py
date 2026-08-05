# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import base64
import hashlib
import hmac
import math
import re
import time

from telethon import TelegramClient, events, types
from telethon.tl.custom import Button

import settings as config
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
from storage.data_manager import DataManager
from localization import localized_result, t


TRANSFER_CUSTOM_EMOJI_ID = 5807499888245612254
INLINE_TRANSFER_MESSAGE_CUSTOM_EMOJI_ID = 5875465628285931233
INLINE_TRANSFER_TTL_SECONDS = 24 * 60 * 60
INLINE_TRANSFER_CALLBACK_PREFIX = "itr"
INLINE_TRANSFER_THUMB_URL = "https://i.postimg.cc/RVSJZwpR/download.jpg"
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


def _remaining_text(seconds: int, language="zh") -> str:
    total_minutes = math.ceil(max(0, int(seconds)) / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return t(language, "transfer.remaining_hours_minutes", hours=hours, minutes=minutes)
    if hours:
        return t(language, "transfer.remaining_hours", hours=hours)
    return t(language, "transfer.remaining_minutes", minutes=minutes)


def _base36_encode(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = int(value)
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = alphabet[remainder] + encoded
    return encoded


def _base36_decode(value: str) -> int:
    return int(value, 36)


def _inline_transfer_signature(payload: str) -> str:
    digest = hmac.new(
        config.BOT_TOKEN.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).digest()[:8]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_inline_transfer_callback(
    from_user_id: int, phone: str, expires_at: int
) -> bytes:
    digits = AccountManager._digits_only(phone)
    payload = ":".join(
        (
            INLINE_TRANSFER_CALLBACK_PREFIX,
            _base36_encode(from_user_id),
            _base36_encode(expires_at),
            digits,
        )
    )
    callback = f"{payload}:{_inline_transfer_signature(payload)}".encode("ascii")
    if len(callback) > 64:
        raise ValueError("inline transfer callback exceeds Telegram's 64-byte limit")
    return callback


def parse_inline_transfer_callback(data: bytes, now: int | None = None):
    try:
        decoded = data.decode("ascii")
        prefix, sender_text, expiry_text, digits, signature = decoded.split(":")
        if prefix != INLINE_TRANSFER_CALLBACK_PREFIX or not re.fullmatch(r"\d{6,15}", digits):
            return None, "invalid"
        payload = ":".join((prefix, sender_text, expiry_text, digits))
        if not hmac.compare_digest(signature, _inline_transfer_signature(payload)):
            return None, "invalid"
        from_user_id = _base36_decode(sender_text)
        expires_at = _base36_decode(expiry_text)
        if from_user_id <= 0 or expires_at <= int(time.time() if now is None else now):
            return None, "expired"
        return (from_user_id, f"+{digits}", expires_at), "ok"
    except (UnicodeDecodeError, ValueError, TypeError):
        return None, "invalid"


def _inline_phone_input(text: str) -> str:
    text = (text or "").strip()
    if not re.fullmatch(r"\+?[\d\s()\-]{6,24}", text):
        return ""
    normalized = AccountManager.normalize_phone(text)
    digits = AccountManager._digits_only(normalized)
    return f"+{digits}" if 6 <= len(digits) <= 15 else ""


def _inline_transfer_thumb() -> types.InputWebDocument:
    return types.InputWebDocument(
        url=INLINE_TRANSFER_THUMB_URL,
        size=0,
        mime_type="image/jpeg",
        attributes=[types.DocumentAttributeImageSize(w=320, h=320)],
    )


def _inline_source_failure_text(code: str, language="zh") -> str:
    if code == "not_owned":
        return t(language, "transfer.failure_not_owned")
    if code == "source_not_vip":
        return t(language, "transfer.failure_source_access")
    if code == "uploaded_session_not_transferable":
        return t(language, "transfer.failure_locked")
    if code == "too_new":
        return t(language, "transfer.failure_too_new")
    return t(language, "transfer.failure_default")


def _transfer_result_text(result, language="zh") -> str:
    message_key = getattr(result, "message_key", "")
    if message_key:
        return t(
            language,
            message_key,
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
    return t(language, key) if key else localized_result(
        language, getattr(result, "message", "")
    )


def _receiver_display_name(sender, fallback_user_id: int) -> str:
    name_parts = [
        getattr(sender, "first_name", None),
        getattr(sender, "last_name", None),
    ]
    display_name = "".join(part.strip() for part in name_parts if part and part.strip())
    display_name = re.sub(r"\s+", " ", display_name).strip()
    return display_name or str(fallback_user_id)


async def _expire_inline_transfer_card(event, detail: str) -> None:
    language = DataManager.get_user_language(event.sender_id)
    await event.answer(t(language, "transfer.expired", detail=detail), alert=True)
    try:
        await safe_edit(event,
            t(language, "transfer.expired_title", detail=detail),
            buttons=None,
            parse_mode="md",
        )
    except Exception:
        # The alert still tells the user what happened if Telegram no longer
        # permits editing this inline message.
        pass


async def setup_transfer_handlers(bot: TelegramClient):
    async def answer_inline_notice(event, title: str, description: str):
        result = await event.builder.article(
            title=title,
            description=description,
            text=f"{title}\n\n{description}",
            thumb=_inline_transfer_thumb(),
            link_preview=False,
        )
        await event.answer([result], cache_time=0, private=True)

    @bot.on(events.InlineQuery)
    async def inline_transfer_query(event):
        language = DataManager.get_user_language(event.sender_id)
        # Only a normal one-to-one user chat has exactly one possible recipient.
        if not isinstance(event.query.peer_type, types.InlineQueryPeerTypePM):
            await answer_inline_notice(
                event,
                t(language, "transfer.pm_only"),
                t(language, "transfer.pm_only_detail"),
            )
            return

        phone = _inline_phone_input(event.text)
        if not phone:
            if not (event.text or "").strip():
                await answer_inline_notice(
                    event,
                    t(language, "transfer.inline_title"),
                    t(language, "transfer.inline_detail"),
                )
                return
            await answer_inline_notice(
                event,
                t(language, "transfer.enter_phone"),
                t(language, "transfer.enter_phone_detail"),
            )
            return

        validation = AccountManager.validate_account_transfer_offer(
            event.sender_id, phone
        )
        if not validation.ok:
            await answer_inline_notice(
                event,
                t(language, "transfer.offer_failed"),
                _transfer_result_text(validation, language),
            )
            return

        expires_at = int(time.time()) + INLINE_TRANSFER_TTL_SECONDS
        callback = build_inline_transfer_callback(
            event.sender_id, validation.phone, expires_at
        )
        result = await event.builder.article(
            title=t(language, "transfer.offer_title", phone=validation.phone),
            description=t(language, "transfer.offer_description"),
            text=t(language, "transfer.offer_text", phone=validation.phone),
            parse_mode="html",
            buttons=[[
                Button.inline(
                    t(language, "transfer.receive"),
                    callback,
                )
            ]],
            thumb=_inline_transfer_thumb(),
            link_preview=False,
        )
        await event.answer([result], cache_time=0, private=True)

    @bot.on(events.CallbackQuery(pattern=rb"^itr:"))
    async def inline_transfer_receive(event):
        language = DataManager.get_user_language(event.sender_id)
        offer, status = parse_inline_transfer_callback(event.data)
        if not offer:
            message = t(language, "transfer.request_expired") if status == "expired" else t(language, "transfer.request_invalid")
            await event.answer(message, alert=True)
            return

        from_user_id, phone, _ = offer
        to_user_id = event.sender_id

        source_validation = AccountManager.validate_account_transfer_offer(
            from_user_id, phone
        )
        if not source_validation.ok:
            await _expire_inline_transfer_card(
                event, _inline_source_failure_text(source_validation.code, language)
            )
            return

        if to_user_id == from_user_id:
            await event.answer(t(language, "transfer.self"), alert=True)
            return
        if not AccountManager.check_access(to_user_id):
            await event.answer(t(language, "transfer.vip_required"), alert=True)
            return

        validation = AccountManager.validate_account_transfer(
            from_user_id, phone, to_user_id
        )
        if not validation.ok:
            if validation.code in {
                "not_owned",
                "source_not_vip",
                "uploaded_session_not_transferable",
                "too_new",
            }:
                await _expire_inline_transfer_card(
                    event, _inline_source_failure_text(validation.code, language)
                )
                return
            await event.answer(_transfer_result_text(validation, language), alert=True)
            return

        result = await AccountManager.transfer_account(
            from_user_id,
            phone,
            to_user_id,
            notify_target=False,
        )
        if not result.ok:
            await event.answer(_transfer_result_text(result, language), alert=True)
            return

        receiver_name = _receiver_display_name(await event.get_sender(), to_user_id)
        await safe_edit(event,
            t(language, "transfer.received", name=receiver_name, phone=result.phone),
            buttons=None,
            parse_mode="html",
        )

    async def render_transfer_accounts(event, page: int = 0):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        current_message = (
            await event.get_message() if hasattr(event, "get_message") else None
        )
        await delete_remembered_flow_messages(
            user_id, preserve_message=current_message
        )
        clear_account_transfer_state(user_id)

        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return

        accounts = user_accounts.get(user_id, {})
        if not accounts:
            await event.answer()
            await safe_edit(event,
                t(language, "accounts.empty"),
                buttons=[[back_button(b"back_to_main", language=language)]],
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
                    label = f"📱 {display_phone} ｜ ⛔️ {t(language, 'transfer.locked', remaining=_remaining_text(remaining, language))}"
                else:
                    label = f"📱 {display_phone} ｜ ✅"
            digits = re.sub(r"\D", "", phone)
            buttons.append([
                Button.inline(label, f"account_transfer_select_{digits}".encode())
            ])

        if max_page > 0:
            nav = []
            if page > 0:
                nav.append(Button.inline(t(language, "common.previous"), f"account_transfer_accounts_{page - 1}".encode()))
            nav.append(Button.inline(f"{page + 1}/{max_page + 1}", f"account_transfer_accounts_{page}".encode()))
            if page < max_page:
                nav.append(Button.inline(t(language, "common.next"), f"account_transfer_accounts_{page + 1}".encode()))
            buttons.append(nav)
        buttons.append([back_button(b"back_to_main", language=language)])

        await event.answer()
        await safe_edit(event,
            t(language, "transfer.page"),
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
        language = DataManager.get_user_language(user_id)
        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
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
            await event.answer(t(language, "transfer.not_owned"), alert=True)
            return

        if AccountManager.is_uploaded_transfer_locked(user_id, phone):
            await event.answer(t(language, "transfer.upload_locked"), alert=True)
            return

        remaining = AccountManager.get_account_transfer_remaining_seconds(user_id, phone)
        if remaining:
            await event.answer(
                t(language, "transfer.wait", remaining=_remaining_text(remaining, language)),
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
            t(language, "transfer.target_prompt", phone=phone),
            buttons=[[back_button(b"account_transfer_accounts", language=language)]],
            parse_mode="md",
        )

    @bot.on(events.NewMessage)
    async def transfer_target_input(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
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
            await event.respond(t(language, "transfer.timeout"))
            return

        text = (event.text or "").strip()
        if not re.fullmatch(r"\d+", text):
            retry_message = await event.respond(t(language, "transfer.invalid_id"))
            remember_flow_message(user_id, retry_message)
            return
        to_user_id = int(text)
        phone = state.get("account_transfer_phone") or ""
        validation = AccountManager.validate_account_transfer(
            user_id, phone, to_user_id
        )
        if not validation.ok:
            retry_message = await event.respond(
                _transfer_result_text(validation, language)
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
            t(language, "transfer.confirm", phone=validation.phone, target=to_user_id),
            buttons=[[
                Button.inline(t(language, "transfer.confirm_button"), b"account_transfer_confirm"),
                Button.inline(t(language, "transfer.cancel"), b"account_transfer_cancel"),
            ]],
            parse_mode="md",
        )
        remember_flow_message(user_id, confirmation_message)

    @bot.on(events.CallbackQuery(pattern=b"account_transfer_confirm"))
    async def transfer_confirm(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        state = get_state(user_id)
        if not state.get("account_transfer_pending"):
            await event.answer(t(language, "transfer.none_pending"), alert=True)
            return

        created_at = float(state.get("account_transfer_created_at", 0) or 0)
        if not created_at or time.time() - created_at > TRANSFER_STATE_TTL_SECONDS:
            clear_account_transfer_state(user_id)
            forget_flow_message(user_id)
            await event.answer(t(language, "transfer.timeout"), alert=True)
            await safe_edit(event,
                t(language, "transfer.timeout"),
                buttons=[[back_button(b"account_transfer_accounts", language=language)]],
            )
            return

        phone = state.get("account_transfer_phone") or ""
        to_user_id = int(state.get("account_transfer_to_user_id", 0) or 0)
        clear_account_transfer_state(user_id)
        forget_flow_message(user_id)
        await event.answer(t(language, "transfer.in_progress"))

        result = await AccountManager.transfer_account(user_id, phone, to_user_id)
        if result.ok:
            await delete_remembered_start_command(user_id)
        await safe_edit(event,
            _transfer_result_text(result, language),
            buttons=None if result.ok else [[back_button(b"back_to_main", language=language)]],
            parse_mode="md",
        )

    @bot.on(events.CallbackQuery(pattern=b"account_transfer_cancel"))
    async def transfer_cancel(event):
        clear_account_transfer_state(event.sender_id)
        forget_flow_message(event.sender_id)
        await render_transfer_accounts(event, 0)
