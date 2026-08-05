# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import re
import logging
import time
from telethon import TelegramClient, events
from telethon.tl.custom import Button
from accounts.account_manager import AccountManager
from handlers.handler_utils import (
    back_button,
    clear_state,
    delete_remembered_flow_messages,
    delete_sensitive_message,
    forget_flow_message,
    get_state,
    paginate_items,
    remember_flow_message,
    safe_edit,
    set_state,
)
from storage.data_manager import DataManager
from localization import localized_result, t

logger = logging.getLogger(__name__)


HOSTING_2FA_STATE_TTL_SECONDS = 10 * 60
HOSTING_2FA_MAX_ATTEMPTS = 3


def hosting_account_buttons(phone: str, acc: dict, user_id=None):
    language = DataManager.get_user_language(user_id) if user_id is not None else "zh"
    digits = phone.lstrip("+")
    if AccountManager.is_account_online(acc):
        return [
            [Button.inline(t(language, "hosting.kick"), f"hosting_kick_{digits}".encode(), icon=5877341274863832725)],
            [Button.inline(t(language, "hosting.code"), f"hosting_code_{digits}".encode(), icon=5954224165874569584)],
            [Button.inline(t(language, "hosting.password"), f"hosting_2fa_{digits}".encode(), icon=5879895758202735862)],
            [Button.inline(t(language, "hosting.clean"), f"hosting_clean_menu_{digits}".encode(), icon=6007942490076745785)],
            [back_button(b"hosting_menu", language=language)],
        ]
    return [[back_button(b"hosting_menu", language=language)]]


def hosting_clean_buttons(phone: str, user_id=None):
    language = DataManager.get_user_language(user_id) if user_id is not None else "zh"
    digits = phone.lstrip("+")
    return [
        [
            Button.inline(t(language, "hosting.clean_chats"), f"hosting_clean_pick_chats_{digits}".encode()),
            Button.inline(t(language, "hosting.clean_contacts"), f"hosting_clean_pick_contacts_{digits}".encode()),
        ],
        [Button.inline(t(language, "hosting.clean_all"), f"hosting_clean_pick_all_{digits}".encode())],
        [back_button(f"hosting_sel_{digits}".encode(), language=language)],
    ]


def hosting_cleanup_result_text(
    phone: str, clean_type: str, result, language: str = "zh"
) -> str:
    status_key = {
        "success": "hosting.clean_result.success",
        "partial": "hosting.clean_result.partial",
        "failed": "hosting.clean_result.failed",
    }.get(result.status, "hosting.clean_result.failed")
    errors = [f"• {error}" for error in result.errors[:3]]
    if result.errors:
        if len(result.errors) > 3:
            errors.append(t(
                language,
                "hosting.clean_result.more_errors",
                count=len(result.errors) - 3,
            ))
    return t(
        language,
        "hosting.clean_result.text",
        status=t(language, status_key),
        phone=phone,
        action=t(language, f"hosting.clean_{clean_type}"),
        chats=result.chats_deleted,
        contacts=result.contacts_deleted,
        error_block=(
            t(language, "hosting.clean_result.errors", errors="\n".join(errors))
            if errors else ""
        ),
    )


def hosting_2fa_buttons(phone: str, user_id=None):
    language = DataManager.get_user_language(user_id) if user_id is not None else "zh"
    digits = phone.lstrip("+")
    return [
        [Button.inline(t(language, "hosting.password_reset"), f"hosting_2fa_reset_{digits}".encode())],
        [Button.inline(t(language, "hosting.password_change"), f"hosting_2fa_change_{digits}".encode())],
        [Button.inline(t(language, "hosting.password_set"), f"hosting_2fa_set_{digits}".encode())],
        [back_button(f"hosting_sel_{digits}".encode(), language=language)],
    ]


def clear_hosting_2fa_state(user_id: int):
    clear_state(
        user_id,
        "waiting_hosting_2fa_input",
        "hosting_2fa_action",
        "hosting_2fa_phone",
        "hosting_2fa_attempts",
        "hosting_2fa_created_at",
    )


async def setup_hosting_handlers(bot: TelegramClient):
    @bot.on(events.CallbackQuery(pattern=rb"hosting_menu(?:_\d+)?"))
    async def hosting_menu(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        clear_hosting_2fa_state(user_id)
        data = event.data.decode(errors="ignore")
        page = int(data.rsplit("_", 1)[1]) if data.startswith("hosting_menu_") else 0

        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return

        accounts = AccountManager.get_user_accounts(user_id)
        if not accounts:
            await event.answer()
            await safe_edit(event,
                t(language, "hosting.empty"),
                buttons=[[back_button(b"back_to_main", language=language)]],
            )
            return

        account_items, page, max_page = paginate_items(list(accounts.items()), page)
        buttons = []
        for phone, acc in account_items:
            display_phone = acc.get("display_phone", phone)
            hosting_status = AccountManager.get_compact_hosting_status_text(user_id, phone, acc)
            digits = phone.lstrip("+")
            buttons.append([Button.inline(f"📱 {display_phone} ｜ {hosting_status}", f"hosting_sel_{digits}".encode())])

        if max_page > 0:
            nav = []
            if page > 0:
                nav.append(Button.inline(t(language, "common.previous"), f"hosting_menu_{page - 1}".encode()))
            nav.append(Button.inline(f"{page + 1}/{max_page + 1}", f"hosting_menu_{page}".encode()))
            if page < max_page:
                nav.append(Button.inline(t(language, "common.next"), f"hosting_menu_{page + 1}".encode()))
            buttons.append(nav)

        buttons.append([back_button(b"back_to_main", language=language)])

        await event.answer()
        await safe_edit(event,
            t(language, "hosting.page", count=len(accounts)),
            buttons=buttons
        )

    @bot.on(events.CallbackQuery(pattern=b"hosting_sel_"))
    async def hosting_select_account(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        clear_hosting_2fa_state(user_id)
        data = event.data.decode(errors="ignore")
        m = re.match(r"hosting_sel_(\d+)", data)
        if not m:
            await event.answer()
            return

        phone = "+" + m.group(1)

        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return

        accounts = AccountManager.get_user_accounts(user_id)
        if phone not in accounts:
            await event.answer(t(language, "hosting.account_missing"), alert=True)
            return

        acc = accounts[phone]
        display_phone = acc.get("display_phone", phone)
        hosting_status = AccountManager.get_hosting_status_text(user_id, phone, acc)

        buttons = hosting_account_buttons(phone, acc, user_id)

        await event.answer()
        await safe_edit(event,
            t(language, "hosting.detail", phone=display_phone, status=hosting_status),
            buttons=buttons
        )

    @bot.on(events.CallbackQuery(pattern=b"hosting_kick_"))
    async def hosting_kick(event):
        """踢出其他会话（二次确认入口）"""
        user_id = event.sender_id
        data = event.data.decode(errors="ignore")
        m = re.match(r"hosting_kick_(\d+)", data)
        if not m:
            await event.answer()
            return
        phone = "+" + m.group(1)
        digits = phone.lstrip("+")
        language = DataManager.get_user_language(user_id)

        message = t(language, "hosting.kick_confirm", phone=phone)

        buttons = [
            [Button.inline(t(language, "hosting.confirm_kick"), f"hosting_kick_confirm_{digits}".encode()),
            back_button(f"hosting_sel_{digits}".encode(), language=language)],
        ]

        await safe_edit(event, message, buttons=buttons, parse_mode="md")


    @bot.on(events.CallbackQuery(pattern=b"hosting_kick_confirm_"))
    async def hosting_kick_confirm(event):
        """踢出其他会话（二次确认执行）"""
        user_id = event.sender_id
        data = event.data.decode(errors="ignore")
        m = re.match(r"hosting_kick_confirm_(\d+)", data)
        if not m:
            await event.answer()
            return
        phone = "+" + m.group(1)
        digits = phone.lstrip("+")
        language = DataManager.get_user_language(user_id)

        msg = await AccountManager.kick_other_sessions(user_id, phone)

        buttons = [
            [Button.inline(t(language, "hosting.code"), f"hosting_code_{digits}".encode(), icon=5954224165874569584)],
            [back_button(f"hosting_sel_{digits}".encode(), language=language)],
        ]
        await safe_edit(event, localized_result(language, msg), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=b"hosting_code_"))
    async def hosting_code(event):
        user_id = event.sender_id
        data = event.data.decode(errors="ignore")
        m = re.match(r"hosting_code_(\d+)", data)
        if not m:
            await event.answer()
            return
        phone = "+" + m.group(1)
        language = DataManager.get_user_language(user_id)

        msg = await AccountManager.start_code_fetch(user_id, phone)
        digits = phone.lstrip("+")
        if msg.startswith("❌"):
            buttons = [[back_button(f"hosting_sel_{digits}".encode(), language=language)]]
            text = t(language, "hosting.code_error", phone=phone, message=localized_result(language, msg))
        else:
            buttons = [[Button.inline(t(language, "hosting.code_exit"), f"hosting_code_exit_{digits}".encode())]]
            text = t(language, "hosting.code_active", phone=phone, message=localized_result(language, msg))
        await event.answer()
        await safe_edit(event,
            text,
            buttons=buttons
        )

    @bot.on(events.CallbackQuery(pattern=rb"^hosting_clean_menu_(\d+)$"))
    async def hosting_clean_menu(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        phone = "+" + event.pattern_match.group(1).decode()
        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return
        accounts = AccountManager.get_user_accounts(user_id)
        if phone not in accounts:
            await event.answer(t(language, "hosting.account_missing"), alert=True)
            return
        if not AccountManager.is_account_online(accounts[phone]):
            await event.answer(t(language, "hosting.clean_offline"), alert=True)
            return
        remaining = AccountManager.get_hosting_clean_remaining_seconds(
            user_id, phone, accounts[phone]
        )
        if remaining:
            await event.answer(
                AccountManager.hosting_clean_age_message(remaining, language),
                alert=True,
            )
            return

        display_phone = accounts[phone].get("display_phone", phone)
        await event.answer()
        await safe_edit(
            event,
            t(language, "hosting.clean_menu", phone=display_phone),
            buttons=hosting_clean_buttons(phone, user_id),
            parse_mode=None,
        )

    @bot.on(events.CallbackQuery(pattern=rb"^hosting_clean_pick_(chats|contacts|all)_(\d+)$"))
    async def hosting_clean_pick(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        clean_type = event.pattern_match.group(1).decode()
        digits = event.pattern_match.group(2).decode()
        phone = "+" + digits
        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return
        accounts = AccountManager.get_user_accounts(user_id)
        if phone not in accounts:
            await event.answer(t(language, "hosting.account_missing"), alert=True)
            return
        remaining = AccountManager.get_hosting_clean_remaining_seconds(
            user_id, phone, accounts[phone]
        )
        if remaining:
            await event.answer(
                AccountManager.hosting_clean_age_message(remaining, language),
                alert=True,
            )
            return

        warnings = []
        if clean_type in {"chats", "all"}:
            warnings.append(t(language, "hosting.clean_warn_chats"))
        if clean_type in {"contacts", "all"}:
            warnings.append(t(language, "hosting.clean_warn_contacts"))
        display_phone = accounts[phone].get("display_phone", phone)
        await event.answer()
        await safe_edit(
            event,
            t(language, "hosting.clean_confirm", phone=display_phone,
              action=t(language, f"hosting.clean_{clean_type}"), warnings="\n".join(warnings)),
            buttons=[
                [Button.inline(
                    t(language, "hosting.clean_confirm_button"),
                    f"hosting_clean_confirm_{clean_type}_{digits}".encode(),
                )],
                [back_button(f"hosting_clean_menu_{digits}".encode(), language=language)],
            ],
            parse_mode=None,
        )

    @bot.on(events.CallbackQuery(pattern=rb"^hosting_clean_confirm_(chats|contacts|all)_(\d+)$"))
    async def hosting_clean_confirm(event):
        clean_type = event.pattern_match.group(1).decode()
        digits = event.pattern_match.group(2).decode()
        phone = "+" + digits
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        await event.answer()
        await safe_edit(
            event,
            t(language, "hosting.clean_running"),
            buttons=None,
            parse_mode=None,
        )
        result = await AccountManager.clean_hosted_account(user_id, phone, clean_type)
        await safe_edit(
            event,
            hosting_cleanup_result_text(phone, clean_type, result, language),
            buttons=[[back_button(f"hosting_sel_{digits}".encode(), language=language)]],
            parse_mode=None,
        )

    @bot.on(events.CallbackQuery(pattern=b"hosting_code_exit_"))
    async def hosting_code_exit(event):
        user_id = event.sender_id
        data = event.data.decode(errors="ignore")
        m = re.match(r"hosting_code_exit_(\d+)", data)
        if not m:
            await event.answer()
            return
        phone = "+" + m.group(1)

        msg = await AccountManager.stop_code_fetch(user_id, phone)
        digits = phone.lstrip("+")
        accounts = AccountManager.get_user_accounts(user_id)
        acc = accounts.get(phone, {})
        display_phone = acc.get("display_phone", phone)
        hosting_status = AccountManager.get_hosting_status_text(user_id, phone, acc)

        language = DataManager.get_user_language(user_id)
        buttons = hosting_account_buttons(phone, acc, user_id)
        await event.answer()
        await safe_edit(event,
            t(language, "hosting.detail", phone=display_phone, status=f"{hosting_status}\n\n{msg}"),
            buttons=buttons
        )


    @bot.on(events.CallbackQuery(pattern=rb"hosting_2fa_\d+$"))
    async def hosting_2fa_menu(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        current_message = (
            await event.get_message() if hasattr(event, "get_message") else None
        )
        await delete_remembered_flow_messages(
            user_id, preserve_message=current_message
        )
        clear_hosting_2fa_state(user_id)
        data = event.data.decode(errors="ignore")
        m = re.fullmatch(r"hosting_2fa_(\d+)", data)
        if not m:
            return
        phone = "+" + m.group(1)

        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return

        accounts = AccountManager.get_user_accounts(user_id)
        if phone not in accounts:
            await event.answer(t(language, "hosting.account_missing"), alert=True)
            return

        display_phone = accounts[phone].get("display_phone", phone)
        await event.answer()
        await safe_edit(event,
            t(language, "hosting.password_menu", phone=display_phone),
            buttons=hosting_2fa_buttons(phone, user_id),
            parse_mode="md",
        )

    @bot.on(events.CallbackQuery(pattern=b"hosting_2fa_reset_"))
    async def hosting_reset_password(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        data = event.data.decode(errors="ignore")
        m = re.fullmatch(r"hosting_2fa_reset_(\d+)", data)
        if not m:
            return
        phone = "+" + m.group(1)

        await event.answer()
        msg = await AccountManager.request_2fa_reset(user_id, phone)
        digits = phone.lstrip("+")
        buttons = [
            [back_button(f"hosting_2fa_{digits}".encode(), language=language)]
        ]
        await safe_edit(event,
            t(language, "hosting.password_reset_page", phone=phone, result=localized_result(language, msg)),
            buttons=buttons
        )

    @bot.on(events.CallbackQuery(pattern=b"hosting_2fa_change_"))
    async def hosting_2fa_change(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        data = event.data.decode(errors="ignore")
        m = re.fullmatch(r"hosting_2fa_change_(\d+)", data)
        if not m:
            return
        phone = "+" + m.group(1)
        digits = phone.lstrip("+")

        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return

        set_state(
            user_id,
            waiting_hosting_2fa_input=True,
            hosting_2fa_action="change",
            hosting_2fa_phone=phone,
            hosting_2fa_attempts=0,
            hosting_2fa_created_at=time.time(),
        )
        await event.answer()
        await safe_edit(event,
            t(language, "hosting.password_change_prompt", phone=phone),
            buttons=[[back_button(f"hosting_2fa_{digits}".encode(), language=language)]],
            parse_mode="md",
        )

    @bot.on(events.CallbackQuery(pattern=b"hosting_2fa_set_"))
    async def hosting_2fa_set(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        data = event.data.decode(errors="ignore")
        m = re.fullmatch(r"hosting_2fa_set_(\d+)", data)
        if not m:
            return
        phone = "+" + m.group(1)
        digits = phone.lstrip("+")

        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return

        set_state(
            user_id,
            waiting_hosting_2fa_input=True,
            hosting_2fa_action="set",
            hosting_2fa_phone=phone,
            hosting_2fa_attempts=0,
            hosting_2fa_created_at=time.time(),
        )
        await event.answer()
        await safe_edit(event,
            t(language, "hosting.password_set_prompt", phone=phone),
            buttons=[[back_button(f"hosting_2fa_{digits}".encode(), language=language)]],
            parse_mode="md",
        )

    @bot.on(events.NewMessage)
    async def hosting_2fa_text_input(event):
        user_id = event.sender_id
        language = DataManager.get_user_language(user_id)
        if event.text and event.text.startswith("/"):
            return

        state = get_state(user_id)
        if not state.get("waiting_hosting_2fa_input"):
            return

        text = (event.text or "").strip()
        await delete_sensitive_message(event, "hosted account 2FA input")

        phone = state.get("hosting_2fa_phone")
        action = state.get("hosting_2fa_action")
        digits = (phone or "").lstrip("+")
        attempts = int(state.get("hosting_2fa_attempts", 0))
        created_at = float(state.get("hosting_2fa_created_at", 0) or 0)

        if not phone or action not in {"change", "set"}:
            clear_state(user_id)
            forget_flow_message(user_id)
            await event.respond(t(language, "hosting.password_state_expired"))
            return

        if time.time() - created_at > HOSTING_2FA_STATE_TTL_SECONDS:
            clear_state(user_id)
            forget_flow_message(user_id)
            await event.respond(t(language, "hosting.password_timeout"))
            return

        if action == "change":
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                attempts += 1
                if attempts >= HOSTING_2FA_MAX_ATTEMPTS:
                    clear_state(user_id)
                    forget_flow_message(user_id)
                    await event.respond(t(language, "hosting.password_format_cancelled"))
                else:
                    state["hosting_2fa_attempts"] = attempts
                    set_state(user_id, **state)
                    retry_message = await event.respond(
                        t(language, "hosting.password_format"), parse_mode="md"
                    )
                    remember_flow_message(user_id, retry_message)
                return

            old_password, new_password = parts[0], parts[1]
            if new_password.strip().lower() in {"none", "null", "clear", "remove", "清除"}:
                result = await AccountManager.clear_hosted_2fa(user_id, phone, old_password)
            else:
                result = await AccountManager.change_hosted_2fa(user_id, phone, old_password, new_password)
        else:
            if not text:
                attempts += 1
                if attempts >= HOSTING_2FA_MAX_ATTEMPTS:
                    clear_state(user_id)
                    forget_flow_message(user_id)
                    await event.respond(t(language, "hosting.password_empty_cancelled"))
                else:
                    state["hosting_2fa_attempts"] = attempts
                    set_state(user_id, **state)
                    retry_message = await event.respond(
                        t(language, "hosting.password_empty")
                    )
                    remember_flow_message(user_id, retry_message)
                return

            result = await AccountManager.set_hosted_2fa(user_id, phone, text)

        if action == "change" and "旧二级密码错误" in result:
            attempts += 1
            if attempts >= HOSTING_2FA_MAX_ATTEMPTS:
                clear_state(user_id)
                forget_flow_message(user_id)
                await event.respond(t(language, "hosting.password_old_cancelled"))
            else:
                state["hosting_2fa_attempts"] = attempts
                set_state(user_id, **state)
                retry_message = await event.respond(
                    t(language, "hosting.password_retry", result=localized_result(language, result)),
                    parse_mode="md",
                )
                remember_flow_message(user_id, retry_message)
            return

        clear_state(user_id)
        forget_flow_message(user_id)
        await event.respond(
            t(language, "hosting.password_result", phone=phone, result=localized_result(language, result)),
            buttons=[
                [back_button(f"hosting_2fa_{digits}".encode(), language=language)],
            ],
            parse_mode="md",
        )
