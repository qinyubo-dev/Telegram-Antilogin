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


def hosting_quota_status_text(user_id: int) -> str:
    status = AccountManager.get_quota_status(user_id)
    quota = status['quota']
    total_text = t("main.quota_unlimited") if quota is None else t("main.quota_count", count=quota
    )
    return f"{status['used']} / {total_text}"


def main_menu_text(
    name: str, user_id: int, quota_status_text: str, online_accounts: int,
) -> str:
    return t("main.text", name=name, user_id=user_id,
        quota=quota_status_text, online=online_accounts,
    )


def support_profile_markup(input_user, include_back: bool = True):
    profile_button = types.InputKeyboardButtonUserProfile(
        text=t("support.contact"),
        user_id=input_user,
        style=types.KeyboardButtonStyle(icon=SUPPORT_CUSTOM_EMOJI_ID),
    )
    rows = [types.KeyboardButtonRow(buttons=[profile_button])]
    if include_back:
        rows.append(types.KeyboardButtonRow(buttons=[back_button(b"back_to_main")]))
    return types.ReplyInlineMarkup(
        rows=rows
    )


def main_menu_buttons(user_id: int):
    buttons = [
        [Button.inline(t("main.add_account"), b"add_account", icon=5775937998948404844),
         Button.inline(t("main.account_list"), b"list_accounts", icon=5960551395730919906)],
        [Button.inline(t("main.hosting_tools"), b"hosting_menu", icon=6008118472066732010),
         Button.inline(t("main.antilogin"), b"antilogin_settings", icon=5877260593903177342)],
        [Button.inline(t("main.transfer_account"), b"account_transfer_accounts", icon=TRANSFER_CUSTOM_EMOJI_ID),
         Button.inline(t("main.vip_center"), b"vip_center", icon=6028530359975548369)],
        [Button.inline(
            t("main.login_unlock"), b"login_unlock_menu",
            icon=LOGIN_UNLOCK_CUSTOM_EMOJI_ID,
        ),
         Button.inline(
             t("main.more"), b"more_menu", icon=5884123981706956210
         )],
    ]
    if DataManager.is_admin(user_id):
        buttons.append([Button.inline(
            t("main.admin"), b"admin_panel", icon=5807868868886009920
        )])
    return buttons


def more_menu_buttons(user_id: int):
    return [
        [
            Button.inline(
                t("main.reload"), b"reload_user_accounts",
                icon=5877410604225924969,
            ),
            Button.inline(
                t("main.support"), b"customer_support",
                icon=SUPPORT_CUSTOM_EMOJI_ID,
            ),
        ],
        [
            Button.inline(
                t("main.help"), b"show_help",
                icon=5873121512445187130,
            ),
        ],
        [back_button(b"back_to_main")],
    ]


async def render_home(bot, event, *, edit: bool):
    user_id = event.sender_id
    if not AccountManager.check_access(user_id):
        kwargs = {
            "buttons": [
                [Button.inline(t("access.buy"), b"buy_vip", icon=5956148757899776734)],
                [Button.inline(t("main.support"), b"customer_support", icon=SUPPORT_CUSTOM_EMOJI_ID)],
            ],
            "parse_mode": "md",
        }
        text = t("access.text", user_id=user_id)
        return (
            await safe_edit(event, text, ignore_invalid=True, **kwargs)
            if edit
            else await event.respond(text, **kwargs)
        )

    accounts = AccountManager.get_user_accounts(user_id) or {}
    online_accounts = sum(1 for account in accounts.values() if account.get("anti_login"))
    user = await event.get_sender()
    name = user.first_name or t("common.user")
    text = main_menu_text(
        name, user_id, hosting_quota_status_text(user_id),
        online_accounts,
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
        if not cleanup_result.ok:
            message = (
                t("start.qr_cleanup_failed")
                if cleanup_result.reason == "qr_message_delete_failed"
                else t("start.session_releasing")
            )
            await event.respond(message)
            return

        # A new menu flow supersedes the previous command and menu messages.
        await delete_remembered_main_menu(user_id)
        await delete_remembered_start_command(user_id)

        if AccountManager.check_access(user_id):
            remember_start_command_message(user_id, event)
        main_menu_message = await render_home(bot, event, edit=False)
        remember_main_menu_message(user_id, main_menu_message)

    @bot.on(events.NewMessage(pattern=r'^/support(?:@\w+)?$'))
    async def support_command(event):
        try:
            support_user = await resolve_support_user(bot)
            buttons = support_profile_markup(
                support_user, include_back=False
            )
        except Exception:
            logger.exception("无法解析客服用户实体")
            await event.respond(t("support.unavailable"))
            return
        await event.respond(t("support.pointer"), buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=b"show_help"))
    async def show_help(event):
        await event.answer()
        await safe_edit(
            event,
            t("help.text"),
            buttons=[[back_button(b"back_to_main")]]
        )

    @bot.on(events.NewMessage(pattern=r'^/help(?:@\w+)?$'))
    async def help_command(event):
        await event.respond(t("help.text"))

    @bot.on(events.CallbackQuery(pattern=b"more_menu"))
    async def more_menu(event):
        user_id = event.sender_id
        if not AccountManager.check_access(user_id):
            await event.answer(t("common.no_access"), alert=True)
            return
        await event.answer()
        await safe_edit(
            event,
            t("main.more_text"),
            buttons=more_menu_buttons(user_id),
        )

    @bot.on(events.CallbackQuery(pattern=b"customer_support"))
    async def customer_support(event):
        await safe_answer_callback(event)
        try:
            support_user = await resolve_support_user(bot)
            buttons = support_profile_markup(support_user)
        except Exception:
            logger.exception("无法解析客服用户实体")
            await safe_edit(
                event,
                t("support.unavailable"),
                buttons=[[back_button(b"back_to_main")]],
            )
            return
        await safe_edit(
            event,
            t("support.pointer"),
            buttons=buttons,
        )

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
            message = (
                t("back.cleanup_failed")
                if cleanup_result.reason == "qr_message_delete_failed"
                else t("start.session_releasing")
            )
            await event.answer(message, alert=True)
            return
        await event.answer()
        clear_state(user_id)
        await render_home(bot, event, edit=not is_qr_flow)

    @bot.on(events.CallbackQuery(pattern=b"list_accounts"))
    async def list_accounts_callback(event):
        user_id = event.sender_id
        
        if not AccountManager.check_access(user_id):
            await event.answer(t("common.no_access"), alert=True)
            return
            
        accounts = AccountManager.get_user_accounts(user_id)
        
        if not accounts:
            await event.answer()
            await safe_edit(event, t("accounts.empty"), buttons=[[back_button(b"back_to_main")]])
            return
        
        response = t("accounts.title")
        for phone, acc in accounts.items():
            display_phone = acc.get('display_phone', phone)
            protection_icon = AccountManager.get_antilogin_status_icon(acc)
            reload_time = datetime.fromtimestamp(acc.get('last_reload', 0)).strftime('%Y-%m-%d %H:%M')
            response += (
                f"{protection_icon} {display_phone}\n"
                + t("accounts.last_reload", time=reload_time)
            )
        
        await event.answer()
        await safe_edit(event, response, buttons=[[back_button(b"back_to_main")]])

    @bot.on(events.CallbackQuery(pattern=b"vip_center"))
    async def vip_center(event):
        user_id = event.sender_id

        # 状态信息
        if DataManager.is_admin(user_id):
            status_text = t("vip.admin_status")
            benefit_heading = t("vip.benefits_current")
            closing_text = t("vip.thanks")
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
            status_text = t("vip.active_status", badge=plan_badge,
                            plan=premium_plan_name, date=subscription.get('expires_at', '')[:10])
            benefit_heading = t("vip.benefits_current")
            closing_text = t("vip.thanks")
        else:
            status_text = t("vip.inactive_status")
            benefit_heading = t("vip.benefits_available")
            closing_text = t("vip.invite")

        feature_text = t("vip.features")

        buttons = [
            [Button.inline(t("vip.upgrade"), b"buy_vip")],
            [back_button(b"back_to_main")]
        ]
        subscription = DataManager.get_subscription(user_id)
        if (
            subscription
            and subscription.get('quota') is not None
            and len(AccountManager.hosted_account_phones(user_id)) > int(subscription['quota'])
        ):
            buttons.insert(1, [Button.inline(t("vip.select_accounts"), b"subscription_accounts")])

        await event.answer()
        await safe_edit(event,
            t("vip.center_text", status=status_text, user_id=user_id,
              heading=benefit_heading, features=feature_text, closing=closing_text),
            buttons=buttons
        )

    async def render_subscription_accounts(event):
        user_id = event.sender_id
        subscription = DataManager.get_subscription(user_id)
        if not subscription or subscription.get('quota') is None:
            await event.answer(t("subscription.no_selection"), alert=True)
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
            [Button.inline(t("subscription.save"), b"subscription_selection_done")],
            [back_button(b"vip_center")],
        ])
        await event.answer()
        await safe_edit(
            event,
            t("subscription.selection", selected=len(selected), quota=quota),
            buttons=buttons,
        )

    @bot.on(events.CallbackQuery(pattern=b"subscription_accounts"))
    async def subscription_accounts(event):
        await render_subscription_accounts(event)

    @bot.on(events.CallbackQuery(pattern=rb"sub_select_\d+"))
    async def subscription_toggle_account(event):
        user_id = event.sender_id
        digits = event.data.decode().removeprefix('sub_select_')
        subscription = DataManager.get_subscription(user_id)
        if not subscription or subscription.get('quota') is None:
            await event.answer(t("subscription.changed"), alert=True)
            return
        selected = list(subscription.get('selected_accounts') or [])
        if digits in selected:
            selected.remove(digits)
        else:
            if len(selected) >= int(subscription['quota']):
                await event.answer(t("subscription.limit"), alert=True)
                return
            selected.append(digits)
        if not DataManager.set_selected_accounts(user_id, selected, finalize=False):
            await event.answer(t("subscription.save_failed"), alert=True)
            return
        await render_subscription_accounts(event)

    @bot.on(events.CallbackQuery(pattern=b"subscription_selection_done"))
    async def subscription_selection_done(event):
        user_id = event.sender_id
        subscription = DataManager.get_subscription(user_id)
        if not subscription or subscription.get('quota') is None:
            await event.answer(t("subscription.changed"), alert=True)
            return
        selected = list(subscription.get('selected_accounts') or [])
        if not selected and AccountManager.hosted_account_phones(user_id):
            await event.answer(t("subscription.empty"), alert=True)
            return
        if not DataManager.set_selected_accounts(user_id, selected, finalize=True):
            await event.answer(t("subscription.save_failed"), alert=True)
            return
        await AccountManager.suspend_user_accounts(user_id, keep_selected=True)
        resumed = await AccountManager.resume_selected_accounts(user_id)
        await event.answer(t("subscription.applied", count=resumed), alert=True)
        await vip_center(event)

    # 处理消息输入
    @bot.on(events.NewMessage)
    async def handle_messages(event):
        
        # 忽略命令消息
        if event.text and event.text.startswith('/'):
            return

        if await handle_admin_message(event, bot, payment_system):
            return
        
    @bot.on(events.CallbackQuery(pattern=b"reload_user_accounts"))
    async def manual_reload_callback(event):
        """手动重新加载用户名下的账户"""
        user_id = event.sender_id
        
        if not AccountManager.check_access(user_id):
            await event.answer(t("common.no_access"), alert=True)
            return

        # 重新加载指定用户账户
        await event.answer(t("reload.start"))

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
            message = t("reload.done", alive=alive_count,
                        frozen=frozen_count, dead=dead_count)

            await safe_edit(event, message, buttons=[[back_button(b"back_to_main")]])
        except Exception as e:
            await safe_edit(event, t("reload.failed", error=str(e)),
                             buttons=[[back_button(b"back_to_main")]])
