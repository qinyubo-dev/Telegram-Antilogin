# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import logging
from datetime import datetime
from telethon import TelegramClient, events, types
from telethon.tl.custom import Button
from storage.data_manager import DataManager
from storage.user_profile_cache import UserProfileCache
from accounts.account_manager import AccountManager
from handlers.account_handlers import cancel_pending_login_flow, setup_account_handlers
from payments.payment_handlers import setup_payment_handlers
from handlers.vip_handlers import setup_vip_handlers
from handlers.admin_handlers import handle_admin_message, setup_admin_handlers
from handlers.hosting_handlers import setup_hosting_handlers
from handlers.antilogin_handlers import setup_antilogin_handlers
from handlers.transfer_handlers import TRANSFER_CUSTOM_EMOJI_ID, setup_transfer_handlers
from handlers.login_unlock_handlers import (
    LOGIN_UNLOCK_CUSTOM_EMOJI_ID,
    cancel_login_unlock_flow,
    setup_login_unlock_handlers,
)
from handlers.handler_utils import (
    back_button,
    clear_state,
    delete_remembered_main_menu,
    delete_remembered_flow_messages,
    delete_remembered_start_command,
    get_state,
    remember_main_menu_message,
    remember_start_command_message,
    safe_answer_callback,
    safe_edit,
)
from payments.payment_system import PaymentSystem
from localization import t

# 设置日志
logger = logging.getLogger(__name__)
SUPPORT_USER_ID = 8038400287
SUPPORT_USERNAME = "QinSupport"
SUPPORT_CUSTOM_EMOJI_ID = 5891243564309942507
LANGUAGE_CUSTOM_EMOJI_ID = 5879585266426973039


def _language(user_id: int) -> str:
    return DataManager.get_user_language(user_id)


def hosting_quota_status_text(user_id: int, language: str = None) -> str:
    language = language or _language(user_id)
    status = AccountManager.get_quota_status(user_id)
    quota = status['quota']
    total_text = t(language, "main.quota_unlimited") if quota is None else t(
        language, "main.quota_count", count=quota
    )
    return f"{status['used']} / {total_text}"


def main_menu_text(
    name: str, user_id: int, quota_status_text: str, online_accounts: int,
    language: str = "zh",
) -> str:
    return t(
        language, "main.text", name=name, user_id=user_id,
        quota=quota_status_text, online=online_accounts,
    )


def support_profile_markup(input_user, include_back: bool = True, language: str = "zh"):
    profile_button = types.InputKeyboardButtonUserProfile(
        text=t(language, "support.contact"),
        user_id=input_user,
        style=types.KeyboardButtonStyle(icon=SUPPORT_CUSTOM_EMOJI_ID),
    )
    rows = [types.KeyboardButtonRow(buttons=[profile_button])]
    if include_back:
        rows.append(types.KeyboardButtonRow(buttons=[back_button(b"back_to_main", language=language)]))
    return types.ReplyInlineMarkup(
        rows=rows
    )


def language_buttons():
    return [[
        Button.inline("简体中文", b"language_set_zh", icon=LANGUAGE_CUSTOM_EMOJI_ID),
        Button.inline("English", b"language_set_en", icon=LANGUAGE_CUSTOM_EMOJI_ID),
    ]]


def main_menu_buttons(user_id: int):
    language = _language(user_id)
    buttons = [
        [Button.inline(t(language, "main.add_account"), b"add_account", icon=5775937998948404844),
         Button.inline(t(language, "main.account_list"), b"list_accounts", icon=5960551395730919906)],
        [Button.inline(t(language, "main.hosting_tools"), b"hosting_menu", icon=6008118472066732010),
         Button.inline(t(language, "main.antilogin"), b"antilogin_settings", icon=5877260593903177342)],
        [Button.inline(t(language, "main.transfer_account"), b"account_transfer_accounts", icon=TRANSFER_CUSTOM_EMOJI_ID),
         Button.inline(t(language, "main.vip_center"), b"vip_center", icon=6028530359975548369)],
        [Button.inline(
            t(language, "main.login_unlock"), b"login_unlock_menu",
            icon=LOGIN_UNLOCK_CUSTOM_EMOJI_ID,
        ),
         Button.inline(
             t(language, "main.more"), b"more_menu", icon=5884123981706956210
         )],
    ]
    if DataManager.is_admin(user_id):
        buttons.append([Button.inline(
            t(language, "main.admin"), b"admin_panel", icon=5807868868886009920
        )])
    return buttons


def more_menu_buttons(user_id: int):
    language = _language(user_id)
    return [
        [
            Button.inline(
                t(language, "main.reload"), b"reload_user_accounts",
                icon=5877410604225924969,
            ),
            Button.inline(
                t(language, "main.support"), b"customer_support",
                icon=SUPPORT_CUSTOM_EMOJI_ID,
            ),
        ],
        [
            Button.inline(
                t(language, "main.help"), b"show_help",
                icon=5873121512445187130,
            ),
            Button.inline(
                t(language, "language.menu"), b"language_menu",
                icon=LANGUAGE_CUSTOM_EMOJI_ID,
            ),
        ],
        [back_button(b"back_to_main", language=language)],
    ]


async def render_home(bot, event, *, edit: bool):
    user_id = event.sender_id
    language = _language(user_id)
    if not AccountManager.check_access(user_id):
        kwargs = {
            "buttons": [
                [Button.inline(t(language, "access.buy"), b"buy_vip", icon=5956148757899776734)],
                [Button.inline(t(language, "main.support"), b"customer_support", icon=SUPPORT_CUSTOM_EMOJI_ID)],
                [Button.inline(
                    t(language, "language.menu"), b"language_menu",
                    icon=LANGUAGE_CUSTOM_EMOJI_ID,
                )],
            ],
            "parse_mode": "md",
        }
        text = t(language, "access.text", user_id=user_id)
        return (
            await safe_edit(event, text, ignore_invalid=True, **kwargs)
            if edit
            else await event.respond(text, **kwargs)
        )

    accounts = AccountManager.get_user_accounts(user_id) or {}
    online_accounts = sum(1 for account in accounts.values() if account.get("anti_login"))
    user = await event.get_sender()
    name = user.first_name or t(language, "common.user")
    text = main_menu_text(
        name, user_id, hosting_quota_status_text(user_id, language),
        online_accounts, language,
    )
    kwargs = {"buttons": main_menu_buttons(user_id), "parse_mode": "md"}
    return (
        await safe_edit(event, text, ignore_invalid=True, **kwargs)
        if edit
        else await event.respond(text, **kwargs)
    )


async def resolve_support_user(bot: TelegramClient):
    try:
        return await bot.get_input_entity(SUPPORT_USER_ID)
    except ValueError:
        entity = await bot.get_entity(SUPPORT_USERNAME)
        if entity.id != SUPPORT_USER_ID:
            raise ValueError("客服用户名对应的用户 ID 不匹配")
        return await bot.get_input_entity(entity)


async def cancel_user_flow_for_language(user_id: int) -> bool:
    """Cancel the active user flow before showing or changing language."""
    result = await cancel_all_user_flows(user_id, reason="language")
    return result.ok


async def cancel_all_user_flows(user_id: int, reason: str, preserve_message=None):
    """Cancel specialized resources before clearing the shared interaction state."""
    await cancel_login_unlock_flow(user_id)

    result = await cancel_pending_login_flow(
        user_id, reason=reason, preserve_message=preserve_message
    )
    if result.ok:
        await delete_remembered_flow_messages(
            user_id, preserve_message=preserve_message
        )
    return result


async def setup_bot_handlers(bot: TelegramClient, payment_system: PaymentSystem):
    """设置机器人事件处理器"""
    await setup_account_handlers(bot)
    await setup_vip_handlers(bot, payment_system)
    await setup_payment_handlers(bot, payment_system)
    await setup_admin_handlers(bot, payment_system)
    await setup_hosting_handlers(bot)
    await setup_antilogin_handlers(bot)
    await setup_transfer_handlers(bot)
    await setup_login_unlock_handlers(bot)
    async def cache_event_sender(event):
        try:
            sender = await event.get_sender()
            UserProfileCache.set_entity(sender)
        except Exception:
            logger.debug("用户资料缓存更新失败: user_id=%s", event.sender_id)

    @bot.on(events.NewMessage)
    async def cache_message_sender(event):
        await cache_event_sender(event)

    @bot.on(events.CallbackQuery)
    async def cache_callback_sender(event):
        await cache_event_sender(event)

    @bot.on(events.CallbackQuery(pattern=rb"^pagination_noop$"))
    async def pagination_noop(event):
        await event.answer()
    
    @bot.on(events.NewMessage(pattern='/start'))
    async def start(event):
        user_id = event.sender_id

        AccountManager.cleanup_stale_pending_sessions()
        cleanup_result = await cancel_all_user_flows(user_id, reason="start")
        language = _language(user_id)
        if not cleanup_result.ok:
            message = (
                t(language, "start.qr_cleanup_failed")
                if cleanup_result.reason == "qr_message_delete_failed"
                else t(language, "start.session_releasing")
            )
            await event.respond(message)
            return

        # A new menu flow supersedes the previous command and menu messages.
        await delete_remembered_main_menu(user_id)
        await delete_remembered_start_command(user_id)

        user = await event.get_sender() if hasattr(event, "get_sender") else None
        if not DataManager.initialize_user_language(
            user_id, getattr(user, "lang_code", None)
        ):
            await event.respond(t(language, "language.save_failed"))
            return

        if AccountManager.check_access(user_id):
            remember_start_command_message(user_id, event)
        main_menu_message = await render_home(bot, event, edit=False)
        remember_main_menu_message(user_id, main_menu_message)

    @bot.on(events.NewMessage(pattern=r'^/support(?:@\w+)?$'))
    async def support_command(event):
        language = _language(event.sender_id)
        try:
            support_user = await resolve_support_user(bot)
            buttons = support_profile_markup(
                support_user, include_back=False, language=language
            )
        except Exception:
            logger.exception("无法解析客服用户实体")
            await event.respond(t(language, "support.unavailable"))
            return
        await event.respond(t(language, "support.pointer"), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=b"show_help"))
    async def show_help(event):
        language = _language(event.sender_id)
        await event.answer()
        await safe_edit(
            event,
            t(language, "help.text"),
            buttons=[[back_button(b"back_to_main", language=language)]]
        )

    @bot.on(events.NewMessage(pattern=r'^/help(?:@\w+)?$'))
    async def help_command(event):
        language = _language(event.sender_id)
        await event.respond(t(language, "help.text"))

    @bot.on(events.CallbackQuery(pattern=b"more_menu"))
    async def more_menu(event):
        user_id = event.sender_id
        language = _language(user_id)
        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return
        await event.answer()
        await safe_edit(
            event,
            t(language, "main.more_text"),
            buttons=more_menu_buttons(user_id),
        )

    @bot.on(events.CallbackQuery(pattern=b"customer_support"))
    async def customer_support(event):
        language = _language(event.sender_id)
        await safe_answer_callback(event)
        try:
            support_user = await resolve_support_user(bot)
            buttons = support_profile_markup(support_user, language=language)
        except Exception:
            logger.exception("无法解析客服用户实体")
            await safe_edit(
                event,
                t(language, "support.unavailable"),
                buttons=[[back_button(b"back_to_main", language=language)]],
            )
            return
        await safe_edit(
            event,
            t(language, "support.pointer"),
            buttons=buttons,
        )

    @bot.on(events.NewMessage(pattern=r'^/language(?:@\w+)?$'))
    async def language_command(event):
        user_id = event.sender_id
        if not await cancel_user_flow_for_language(user_id):
            await event.respond(t(_language(user_id), "start.session_releasing"))
            raise events.StopPropagation
        await delete_remembered_main_menu(user_id)
        clear_state(user_id)
        language = _language(user_id)
        await event.respond(
            t(language, "language.choose"), buttons=language_buttons()
        )
        raise events.StopPropagation

    @bot.on(events.CallbackQuery(pattern=b"language_menu"))
    async def language_menu(event):
        user_id = event.sender_id
        await safe_answer_callback(event)
        if not await cancel_user_flow_for_language(user_id):
            await safe_edit(
                event,
                t(_language(user_id), "start.session_releasing"),
                buttons=[[back_button(b"back_to_main", user_id=user_id)]],
            )
            return
        clear_state(user_id)
        language = _language(user_id)
        await safe_edit(event, t(language, "language.choose"), buttons=language_buttons())

    @bot.on(events.CallbackQuery(pattern=rb"^language_set_(zh|en)$"))
    async def language_set(event):
        language = event.data.decode().rsplit("_", 1)[-1]
        previous = _language(event.sender_id)
        if not DataManager.set_user_language(event.sender_id, language):
            await event.answer(t(previous, "language.save_failed"), alert=True)
            return
        clear_state(event.sender_id)
        await event.answer(t(language, "language.saved"))
        await render_home(bot, event, edit=True)

    @bot.on(events.CallbackQuery(pattern=b"back_to_main"))
    async def back_to_main(event):
        user_id = event.sender_id
        state = get_state(user_id)
        is_qr_flow = bool(state.get("qr_login") and state.get("qr_flow_id"))
        if is_qr_flow:
            state["qr_cancel_requested"] = True

        # 返回主菜单视为取消当前交互流程，避免状态残留（例如：添加账户等待输入手机号）
        current_message = None if is_qr_flow else await event.get_message()
        cleanup_result = await cancel_all_user_flows(
            user_id,
            reason="back_to_main",
            preserve_message=None if is_qr_flow else current_message,
        )
        if not cleanup_result.ok:
            language = _language(user_id)
            message = (
                t(language, "back.cleanup_failed")
                if cleanup_result.reason == "qr_message_delete_failed"
                else t(language, "start.session_releasing")
            )
            await event.answer(message, alert=True)
            return
        await event.answer()
        clear_state(user_id)
        await render_home(bot, event, edit=not is_qr_flow)

    @bot.on(events.CallbackQuery(pattern=b"list_accounts"))
    async def list_accounts_callback(event):
        user_id = event.sender_id
        language = _language(user_id)
        
        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return
            
        accounts = AccountManager.get_user_accounts(user_id)
        
        if not accounts:
            await event.answer()
            await safe_edit(event, t(language, "accounts.empty"), buttons=[[back_button(b"back_to_main", language=language)]])
            return
        
        response = t(language, "accounts.title")
        for phone, acc in accounts.items():
            display_phone = acc.get('display_phone', phone)
            protection_icon = AccountManager.get_antilogin_status_icon(acc)
            reload_time = datetime.fromtimestamp(acc.get('last_reload', 0)).strftime('%Y-%m-%d %H:%M')
            response += (
                f"{protection_icon} {display_phone}\n"
                + t(language, "accounts.last_reload", time=reload_time)
            )
        
        await event.answer()
        await safe_edit(event, response, buttons=[[back_button(b"back_to_main", language=language)]])

    @bot.on(events.CallbackQuery(pattern=b"vip_center"))
    async def vip_center(event):
        user_id = event.sender_id
        language = _language(user_id)

        # 状态信息
        if DataManager.is_admin(user_id):
            status_text = t(language, "vip.admin_status")
            benefit_heading = t(language, "vip.benefits_current")
            closing_text = t(language, "vip.thanks")
        elif DataManager.has_active_subscription(user_id):
            subscription = DataManager.get_subscription(user_id) or {}
            plan_id = str(subscription.get('plan_id', '')).lower()
            plan_name = subscription.get('plan_name', 'VIP')
            plan_badge = DataManager.get_subscription_badge(plan_id)
            premium_plan_names = {
                'go': '𝐆𝐎',
                'plus': '𝐏𝐋𝐔𝐒',
                'pro': '𝐏𝐑𝐎',
            }
            premium_plan_name = premium_plan_names.get(plan_id, plan_name)
            status_text = t(language, "vip.active_status", badge=plan_badge,
                            plan=premium_plan_name, date=subscription.get('expires_at', '')[:10])
            benefit_heading = t(language, "vip.benefits_current")
            closing_text = t(language, "vip.thanks")
        else:
            status_text = t(language, "vip.inactive_status")
            benefit_heading = t(language, "vip.benefits_available")
            closing_text = t(language, "vip.invite")

        feature_text = t(language, "vip.features")

        buttons = [
            [Button.inline(t(language, "vip.upgrade"), b"buy_vip")],
            [back_button(b"back_to_main", language=language)]
        ]
        subscription = DataManager.get_subscription(user_id)
        if (
            subscription
            and subscription.get('quota') is not None
            and len(AccountManager.hosted_account_phones(user_id)) > int(subscription['quota'])
        ):
            buttons.insert(1, [Button.inline(t(language, "vip.select_accounts"), b"subscription_accounts")])

        await event.answer()
        await safe_edit(event,
            t(language, "vip.center_text", status=status_text, user_id=user_id,
              heading=benefit_heading, features=feature_text, closing=closing_text),
            buttons=buttons
        )

    async def render_subscription_accounts(event):
        user_id = event.sender_id
        language = _language(user_id)
        subscription = DataManager.get_subscription(user_id)
        if not subscription or subscription.get('quota') is None:
            await event.answer(t(language, "subscription.no_selection"), alert=True)
            return
        phones = sorted(AccountManager.hosted_account_phones(user_id))
        selected = set(subscription.get('selected_accounts') or [])
        quota = int(subscription['quota'])
        buttons = [
            [Button.inline(
                f"{'✅' if digits in selected else '⬜'} +{digits}",
                data=f"sub_select_{digits}".encode(),
            )]
            for digits in phones
        ]
        buttons.extend([
            [Button.inline(t(language, "subscription.save"), b"subscription_selection_done")],
            [back_button(b"vip_center", language=language)],
        ])
        await event.answer()
        await safe_edit(
            event,
            t(language, "subscription.selection", selected=len(selected), quota=quota),
            buttons=buttons,
        )

    @bot.on(events.CallbackQuery(pattern=b"subscription_accounts"))
    async def subscription_accounts(event):
        await render_subscription_accounts(event)

    @bot.on(events.CallbackQuery(pattern=rb"sub_select_\d+"))
    async def subscription_toggle_account(event):
        user_id = event.sender_id
        language = _language(user_id)
        digits = event.data.decode().removeprefix('sub_select_')
        subscription = DataManager.get_subscription(user_id)
        if not subscription or subscription.get('quota') is None:
            await event.answer(t(language, "subscription.changed"), alert=True)
            return
        selected = list(subscription.get('selected_accounts') or [])
        if digits in selected:
            selected.remove(digits)
        else:
            if len(selected) >= int(subscription['quota']):
                await event.answer(t(language, "subscription.limit"), alert=True)
                return
            selected.append(digits)
        if not DataManager.set_selected_accounts(user_id, selected, finalize=False):
            await event.answer(t(language, "subscription.save_failed"), alert=True)
            return
        await render_subscription_accounts(event)

    @bot.on(events.CallbackQuery(pattern=b"subscription_selection_done"))
    async def subscription_selection_done(event):
        user_id = event.sender_id
        language = _language(user_id)
        subscription = DataManager.get_subscription(user_id)
        if not subscription or subscription.get('quota') is None:
            await event.answer(t(language, "subscription.changed"), alert=True)
            return
        selected = list(subscription.get('selected_accounts') or [])
        if not DataManager.set_selected_accounts(user_id, selected, finalize=True):
            await event.answer(t(language, "subscription.save_failed"), alert=True)
            return
        await AccountManager.suspend_user_accounts(user_id, keep_selected=True)
        resumed = await AccountManager.resume_selected_accounts(user_id)
        await event.answer(t(language, "subscription.applied", count=resumed), alert=True)
        await vip_center(event)

    # 处理消息输入
    @bot.on(events.NewMessage)
    async def handle_messages(event):
        user_id = event.sender_id
        
        # 忽略命令消息
        if event.text and event.text.startswith('/'):
            return

        if await handle_admin_message(event, bot, payment_system):
            return
        
    @bot.on(events.CallbackQuery(pattern=b"reload_user_accounts"))
    async def manual_reload_callback(event):
        """手动重新加载用户名下的账户"""
        user_id = event.sender_id
        language = _language(user_id)
        
        if not AccountManager.check_access(user_id):
            await event.answer(t(language, "common.no_access"), alert=True)
            return

        # 重新加载指定用户账户
        await event.answer(t(language, "reload.start"))

        try:
            # 重载当前用户的账户
            stats = await AccountManager.reload_user_accounts_detail(
                user_id, source="manual_reload"
            )
            success = stats.get("success", 0)
            failed = stats.get("failed", 0)
            alive_count = stats.get("alive_count", success)
            frozen_count = stats.get("frozen_count", 0)
            dead_count = stats.get("dead_count", failed)
            
            # 显示重新加载结果
            message = t(language, "reload.done", alive=alive_count,
                        frozen=frozen_count, dead=dead_count)

            await safe_edit(event, message, buttons=[[back_button(b"back_to_main", language=language)]])
        except Exception as e:
            await safe_edit(event, t(language, "reload.failed", error=str(e)),
                             buttons=[[back_button(b"back_to_main", language=language)]])
