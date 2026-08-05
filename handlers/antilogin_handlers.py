# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import logging
from telethon import TelegramClient, events
from telethon.tl.custom import Button
from accounts.account_manager import AccountManager
from handlers.handler_utils import (
    back_button,
    delete_remembered_start_command,
    paginate_items,
    safe_edit,
)
from storage.data_manager import DataManager
from localization import t

logger = logging.getLogger(__name__)


def _language(user_id):
    return DataManager.get_user_language(user_id)


async def setup_antilogin_handlers(bot: TelegramClient):
    @bot.on(events.CallbackQuery(pattern=rb"antilogin_settings(?:_\d+)?"))
    async def manage_antilogin_callback(event):
        """反登录设置：先选择账户，再进行开启/暂停/删除操作"""
        user_id = event.sender_id
        data = event.data.decode(errors="ignore")
        page = int(data.rsplit("_", 1)[1]) if data.startswith("antilogin_settings_") else 0

        if not AccountManager.check_access(user_id):
            await event.answer(t(_language(user_id), "common.no_access"), alert=True)
            return

        accounts = AccountManager.get_user_accounts(user_id)

        if not accounts:
            await event.answer()
            await safe_edit(event, t(_language(user_id), "protection.empty"), buttons=[[back_button(b"back_to_main", user_id=user_id)]])
            return

        account_items, page, max_page = paginate_items(list(accounts.items()), page)
        buttons = []
        for phone, acc_info in account_items:
            display_phone = acc_info.get('display_phone', phone)
            status_text = AccountManager.get_antilogin_status_text(acc_info, user_id)
            # 列表里只做“选择”，不直接切换
            buttons.append([Button.inline(f"📱 {display_phone} ｜ {status_text}", data=f"antilogin_sel_{phone}".encode())])

        if max_page > 0:
            nav = []
            if page > 0:
                nav.append(Button.inline(t(_language(user_id), "common.previous"), f"antilogin_settings_{page - 1}".encode()))
            nav.append(Button.inline(f"{page + 1}/{max_page + 1}", f"antilogin_settings_{page}".encode()))
            if page < max_page:
                nav.append(Button.inline(t(_language(user_id), "common.next"), f"antilogin_settings_{page + 1}".encode()))
            buttons.append(nav)

        buttons.append([back_button(b"back_to_main", user_id=user_id)])

        await event.answer()
        await safe_edit(event, t(_language(user_id), "protection.list", count=len(accounts)), buttons=buttons)

    async def _render_antilogin_account_detail(event, user_id: int, phone: str):
        """渲染单个账户的反登录操作面板"""
        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        acc = accounts.get(phone)
        if not acc:
            await event.answer(t(_language(user_id), "protection.no_account"), alert=True)
            return

        display_phone = acc.get("display_phone", phone)
        language = _language(user_id)
        status_text = AccountManager.get_antilogin_status_text(acc, user_id)
        detail_text = t(
            language,
            "protection.detail",
            phone=display_phone,
            status=status_text,
        )

        if AccountManager.is_account_online(acc):
            buttons = [
                [Button.inline(t(language, "protection.enable"), data=f"antilogin_on_{phone}".encode()),
                 Button.inline(t(language, "protection.pause"), data=f"antilogin_pause_{phone}".encode())],
                [Button.inline(t(language, "protection.delete"), data=f"antilogin_del_{phone}".encode())],
                [back_button(b"antilogin_settings", language=language),
                 Button.inline(t(language, "protection.home"), b"back_to_main")]
            ]
        else:
            buttons = [
                [Button.inline(t(language, "protection.delete"), data=f"antilogin_del_{phone}".encode())],
                [back_button(b"antilogin_settings", language=language),
                 Button.inline(t(language, "protection.home"), b"back_to_main")]
            ]

        await safe_edit(event,
            detail_text,
            buttons=buttons,
            parse_mode="md"
        )

    @bot.on(events.CallbackQuery(pattern=rb"antilogin_sel_\+\d+"))
    async def antilogin_select_account(event):
        user_id = event.sender_id
        phone = event.data.decode().replace("antilogin_sel_", "")

        if not AccountManager.check_access(user_id):
            await event.answer(t(_language(user_id), "common.no_access"), alert=True)
            return

        await event.answer()
        await _render_antilogin_account_detail(event, user_id, phone)

    @bot.on(events.CallbackQuery(pattern=rb"antilogin_on_\+\d+"))
    async def antilogin_enable(event):
        user_id = event.sender_id
        phone = event.data.decode().replace("antilogin_on_", "")

        if not AccountManager.check_access(user_id):
            await event.answer(t(_language(user_id), "common.no_access"), alert=True)
            return

        msg = await AccountManager.resume_anti_login(user_id, phone)
        await event.answer(msg)
        await _render_antilogin_account_detail(event, user_id, phone)

    @bot.on(events.CallbackQuery(pattern=rb"antilogin_pause_\+\d+"))
    async def antilogin_pause(event):
        user_id = event.sender_id
        phone = event.data.decode().replace("antilogin_pause_", "")

        if not AccountManager.check_access(user_id):
            await event.answer(t(_language(user_id), "common.no_access"), alert=True)
            return

        msg = await AccountManager.pause_anti_login(user_id, phone, minutes=30)
        await event.answer(msg)
        await _render_antilogin_account_detail(event, user_id, phone)

    @bot.on(events.CallbackQuery(pattern=rb"^nda:[ar]:\d+:-?\d+$"))
    async def new_device_authorization_action(event):
        """Allow or revoke exactly one authorization from a new-device prompt."""
        user_id = event.sender_id
        try:
            _, action, digits, auth_hash = event.data.decode("ascii").split(":", 3)
            phone = f"+{digits}"
        except (ValueError, UnicodeDecodeError):
            await event.answer(t(_language(user_id), "protection.invalid_device"), alert=True)
            return

        if phone not in AccountManager.get_user_accounts(user_id):
            await event.answer(t(_language(user_id), "protection.device_missing"), alert=True)
            return

        original_message = await event.get_message()
        original_text = getattr(original_message, "raw_text", None) or getattr(
            original_message, "text", ""
        )

        try:
            result = await AccountManager.resolve_new_authorization(
                user_id, phone, auth_hash, allow=action == "a"
            )
        except Exception as error:
            logger.exception(
                "处理新设备授权失败: user_id=%s phone=%s hash=%s",
                user_id,
                phone,
                auth_hash,
            )
            await event.answer(t(_language(user_id), "protection.processing_failed", error=str(error)[:80]), alert=True)
            return

        if not result.get("resolved"):
            await event.answer(result.get("message") or t(_language(user_id), "protection.processing_failed", error=""), alert=True)
            return

        language = _language(user_id)
        result_message = result.get("message") or t(language, "protection.processed")
        for prompt_suffix in (t("zh", "device.choose"), t("en", "device.choose")):
            if original_text.endswith(prompt_suffix):
                original_text = original_text[: -len(prompt_suffix)].rstrip()
                break
        await event.answer(result_message)
        await safe_edit(
            event,
            f"{original_text}\n\n{result_message}" if original_text else result_message,
            buttons=None,
        )



    async def _render_antilogin_delete_confirm(event, user_id: int, phone: str):
        """渲染删除二次确认界面（高端风格）"""
        accounts = AccountManager.get_user_accounts(user_id)
        phone = AccountManager.normalize_phone(phone)
        acc = accounts.get(phone)
        if not acc:
            await event.answer(t(_language(user_id), "protection.no_account"), alert=True)
            return

        display_phone = acc.get("display_phone", phone)

        language = _language(user_id)
        message = t(language, "protection.delete_confirm", phone=display_phone)

        buttons = [
            [Button.inline(t(language, "protection.confirm_delete"), data=f"antilogin_del_confirm_{phone}".encode()),
            back_button(f"antilogin_del_cancel_{phone}".encode(), language=language)],
        ]

        await safe_edit(event, message, buttons=buttons, parse_mode="md")


    @bot.on(events.CallbackQuery(pattern=rb"antilogin_del_\+\d+"))
    async def antilogin_delete(event):
        """删除二次确认入口"""
        user_id = event.sender_id
        phone = event.data.decode().replace("antilogin_del_", "")

        if not AccountManager.check_access(user_id):
            await event.answer(t(_language(user_id), "common.no_access"), alert=True)
            return

        await event.answer()
        await _render_antilogin_delete_confirm(event, user_id, phone)


    @bot.on(events.CallbackQuery(pattern=rb"antilogin_del_cancel_\+\d+"))
    async def antilogin_delete_cancel(event):
        """取消删除，返回详情页"""
        user_id = event.sender_id
        phone = event.data.decode().replace("antilogin_del_cancel_", "")

        if not AccountManager.check_access(user_id):
            await event.answer(t(_language(user_id), "common.no_access"), alert=True)
            return

        await event.answer(t(_language(user_id), "protection.cancelled"))
        await _render_antilogin_account_detail(event, user_id, phone)


    @bot.on(events.CallbackQuery(pattern=rb"antilogin_del_confirm_\+\d+"))
    async def antilogin_delete_confirm(event):
        """确认删除：远程登出当前设备 + 删除本地 session"""
        user_id = event.sender_id
        phone = event.data.decode().replace("antilogin_del_confirm_", "")

        if not AccountManager.check_access(user_id):
            await event.answer(t(_language(user_id), "common.no_access"), alert=True)
            return

        await event.answer(t(_language(user_id), "protection.processing"), alert=False)

        try:
            msg = await AccountManager.delete_account(user_id, phone)
            if msg.startswith("🗑"):
                await delete_remembered_start_command(user_id)
            await event.delete()
            # 尝试额外通知
            try:
                await bot.send_message(user_id, f"{msg}")
            except Exception:
                pass
        except Exception as e:
            logger.exception("删除/退出失败")
            await safe_edit(event,
                t(_language(user_id), "protection.delete_failed", error=str(e)),
                buttons=[
                    [back_button(f"antilogin_sel_{phone}".encode())],
                ],
            )
