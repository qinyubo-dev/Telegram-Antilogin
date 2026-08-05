# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from telethon import TelegramClient, events
from telethon.tl.custom import Button

from handlers.handler_utils import back_button, back_to_main_buttons, clear_state, get_state, safe_edit, safe_edit_message, set_state
from payments.payment_system import PaymentSystem
from storage.data_manager import DataManager
from localization import t
import settings as config


logger = logging.getLogger(__name__)


def _quota_text(quota, language="zh"):
    return t(language, "plans.quota_unlimited") if quota is None else t(language, "plans.quota", quota=quota)


def _percent_text(value) -> str:
    try:
        text = format(Decimal(str(value)).quantize(Decimal('0.1')), 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def _upgrade_order_text(order: dict, language="zh") -> str:
    snapshot = order.get('upgrade_snapshot') or {}
    if order.get('billing_mode') != 'prorated_upgrade':
        period_days = int(order.get('period_days', 30))
        if language == "zh":
            discount_line = ""
            if period_days > 30 and Decimal(str(order.get('actual_discount_percent', '0'))) > 0:
                discount_line = (
                    f"│ 标准原价  ·  {order.get('list_price', order['amount'])} USDT\n"
                    f"│ 周期礼遇  ·  实际节省 {_percent_text(order.get('actual_discount_percent', '0'))}%\n"
                )
            return (
                "╭─ ✦ 尊享账单\n"
                f"│ 服务周期  ·  {period_days} 天\n"
                f"{discount_line}"
                f"│ 应付金额  ·  {order['amount']} USDT\n"
                "╰─ 权益将在支付确认后自动生效"
            )
        return t(language, "plans.billing_standard", days=period_days, amount=order['amount'])
    expiry_text = snapshot.get('target_expires_at', '未知')
    try:
        expiry_text = datetime.fromisoformat(expiry_text).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        pass
    if language == "zh":
        fallback = "\n│ 计价说明  ·  历史权益按当前目录价估算" if snapshot.get('uses_catalog_fallback') else ""
        return (
            "╭─ ✦ 权益折抵\n"
            f"│ 剩余周期  ·  {snapshot.get('billable_days', 0)} 个计费日\n"
            f"│ 当前权益  ·  − {snapshot.get('source_value', '0')} USDT\n"
            f"│ 目标权益  ·  {snapshot.get('target_value', '0')} USDT"
            f"{fallback}\n"
            "├─ ✦ 本次结算\n"
            f"│ 应付差价  ·  {order['amount']} USDT\n"
            f"│ 权益到期  ·  {expiry_text}\n"
            "╰─ 到期时间保持不变"
        )
    return t(language, "plans.billing_upgrade", days=snapshot.get('billable_days', 0),
             source=snapshot.get('source_value', '0'), target=snapshot.get('target_value', '0'),
             amount=order['amount'], expiry=expiry_text)


def _catalog_text(language="zh") -> str:
    catalog = DataManager.get_subscription_catalog()
    go, plus, pro = catalog['go'], catalog['plus'], catalog['pro']
    return t(language, "plans.catalog", go_price=go['price'], go_quota=go['quota'],
             plus_price=plus['price'], plus_quota=plus['quota'], plus_min=plus['min_addon'],
             plus_unit=plus['addon_unit_price'], pro_price=pro['price'],
             go_alerts=config.LOGIN_UNLOCK_MONITOR_LIMITS['go'],
             plus_alerts=config.LOGIN_UNLOCK_MONITOR_LIMITS['plus'])


def _current_subscription_text(subscription: dict, language="zh") -> str:
    plan_id = str(subscription.get('plan_id', '')).lower()
    badge = DataManager.get_subscription_badge(plan_id)
    plan_names = {
        'admin': '𝐀𝐝𝐦𝐢𝐧',
        'go': '𝐆𝐎',
        'plus': '𝐏𝐋𝐔𝐒',
        'pro': '𝐏𝐑𝐎',
    }
    plan_name = plan_names.get(plan_id, subscription.get('plan_name', 'VIP'))
    quota_text = _quota_text(subscription.get('quota'), language)

    if plan_id == 'admin':
        return t(
            language,
            "plans.admin_current",
            badge=badge,
            plan=plan_name,
            quota=quota_text,
        )

    expiry = subscription.get('expires_at', '')
    expiry_text = str(expiry)[:10] or '未知'
    try:
        days_left = max((datetime.fromisoformat(expiry) - datetime.now()).days, 0)
        period_text = t(language, "plans.period_active", date=expiry_text, days=days_left)
    except (TypeError, ValueError):
        period_text = t(language, "plans.period_expiry", date=expiry_text)

    return t(language, "plans.current", badge=badge, plan=plan_name,
             period=period_text, quota=quota_text)


def _success_message(payment_system: PaymentSystem, user_id: int, order_id: str) -> str:
    order = payment_system.pending_orders.get(order_id, {})
    language = DataManager.get_user_language(user_id)
    title = t(language, "plans.upgraded") if order.get('billing_mode') == 'prorated_upgrade' else t(language, "plans.activated")
    subscription = DataManager.get_subscription(user_id, include_inactive=True) or {}
    details = t(language, "plans.details",
                plan=str(subscription.get('plan_id', 'VIP')).upper(),
                expiry=str(subscription.get('expires_at', '-'))[:19])
    return t(language, "plans.success", title=title, details=details,
             amount=order.get('amount', '?'), coin=order.get('coin', 'USDT'), order_id=order_id)


async def setup_vip_handlers(bot: TelegramClient, payment_system: PaymentSystem):
    async def render_purchase(event, notice: str = ""):
        language = DataManager.get_user_language(event.sender_id)
        current = DataManager.get_subscription(event.sender_id, include_inactive=True)
        current_text = _current_subscription_text(current, language) if current and current.get('active') else ""
        buttons = [
            [Button.inline(t(language, "plans.go"), b"buy_sub_go"), Button.inline(t(language, "plans.plus"), b"buy_sub_plus")],
            [Button.inline(t(language, "plans.plus_custom"), b"buy_sub_plus_custom"), Button.inline(t(language, "plans.pro"), b"buy_sub_pro")],
            [back_button(b"vip_center", language=language)],
        ]
        page_text = _catalog_text(language) + current_text
        if notice:
            page_text = f"{notice}\n\n{page_text}"
        await event.answer()
        try:
            await safe_edit(event, page_text, buttons=buttons)
        except Exception:
            await event.respond(page_text, buttons=buttons)

    async def create_order(event, plan_id: str, quota=None, period_days: int = 30):
        language = DataManager.get_user_language(event.sender_id)
        try:
            await event.answer()
        except Exception:
            pass
        result = await payment_system.create_subscription_payment(
            event.sender_id, plan_id, quota, period_days=period_days
        )
        if not result.get('success'):
            message = t(language, "plans.create_failed", error=result.get('error', t(language, "plans.unknown_error")))
            try:
                await safe_edit(event, message, buttons=back_to_main_buttons(language=language))
            except Exception:
                await event.respond(message, buttons=back_to_main_buttons(language=language))
            return
        order_id, pay_url = result['order_id'], result['pay_url']
        order = payment_system.pending_orders[order_id]
        change_keys = {'new':'plans.change_new','renewal':'plans.change_renewal','upgrade':'plans.change_upgrade',
                       'downgrade':'plans.change_downgrade','scheduled_renewal':'plans.change_scheduled'}
        plan_keys = {'go':'plans.go','plus':'plans.plus','pro':'plans.pro'}
        message = t(language, "plans.order",
                    change=t(language, change_keys.get(order.get('change_type'), 'plans.change_default')),
                    plan=t(language, plan_keys.get(plan_id, 'plans.change_default')),
                    quota=_quota_text(order.get('quota'), language),
                    billing=_upgrade_order_text(order, language), order_id=order_id)
        buttons = [
                [Button.url(t(language, "plans.pay"), pay_url)],
                [Button.inline(
                    t(language, "plans.confirm_upgrade")
                    if order.get('billing_mode') == 'prorated_upgrade'
                    else t(language, "plans.confirm"),
                    f"manual_confirm_{order_id}",
                )],
                [back_button(f"cancel_order_{order_id}".encode(), language=language)],
        ]
        try:
            rendered_message = await safe_edit_message(
                event, message, buttons=buttons, link_preview=False
            )
        except Exception:
            rendered_message = await event.respond(message, buttons=buttons, link_preview=False)
        message_id = getattr(rendered_message, 'id', None) or getattr(event, 'message_id', None)
        chat_id = getattr(event, 'chat_id', None) or event.sender_id
        if message_id is not None:
            payment_system.bind_order_message(order_id, chat_id, message_id)

    async def show_period_selection(event, plan_id: str, quota=None):
        language = DataManager.get_user_language(event.sender_id)
        base_quote = DataManager.quote_subscription(plan_id, quota, 30)
        change = DataManager.classify_subscription_change(
            event.sender_id, base_quote['plan_id'], base_quote['quota']
        )
        if change == 'upgrade':
            await create_order(event, plan_id, quota, 30)
            return
        set_state(
            event.sender_id,
            subscription_period_selection=True,
            subscription_plan_id=plan_id,
            subscription_quota=quota,
        )
        periods = DataManager.get_subscription_periods()
        quotes = {
            days: DataManager.quote_subscription(plan_id, quota, days)
            for days in periods
        }
        lines = []
        for days in (30, 90, 180, 365):
            quote = quotes[days]
            lines.append(t(language, "plans.period_line", days=days, price=quote['price']))
        message = t(language, "plans.period_page", lines="\n".join(lines))
        buttons = [
            [Button.inline(t(language, "plans.period_button", days=30), b"subscription_period_30"),
             Button.inline(t(language, "plans.period_button", days=90), b"subscription_period_90")],
            [Button.inline(t(language, "plans.period_button", days=180), b"subscription_period_180"),
             Button.inline(t(language, "plans.period_button", days=365), b"subscription_period_365")],
            [back_button(b"buy_vip", language=language)],
        ]
        try:
            await event.answer()
        except Exception:
            pass
        try:
            await safe_edit(event, message, buttons=buttons)
        except Exception:
            await event.respond(message, buttons=buttons)

    @bot.on(events.CallbackQuery(pattern=b"buy_vip"))
    async def buy_vip(event):
        clear_state(event.sender_id)
        await render_purchase(event)

    @bot.on(events.CallbackQuery(pattern=rb"^buy_sub_(go|plus|pro)$"))
    async def select_plan(event):
        plan_id = event.data.decode().removeprefix('buy_sub_')
        await show_period_selection(event, plan_id)

    @bot.on(events.CallbackQuery(pattern=rb"^subscription_period_(30|90|180|365)$"))
    async def select_subscription_period(event):
        state = get_state(event.sender_id)
        if not state.get('subscription_period_selection'):
            await event.answer(t(DataManager.get_user_language(event.sender_id), "plans.selection_expired"), alert=True)
            return
        period_days = int(event.data.decode().removeprefix('subscription_period_'))
        plan_id = state.get('subscription_plan_id')
        quota = state.get('subscription_quota')
        clear_state(event.sender_id)
        await create_order(event, plan_id, quota, period_days)

    @bot.on(events.CallbackQuery(pattern=b"buy_sub_plus_custom"))
    async def plus_custom(event):
        plus = DataManager.get_subscription_catalog()['plus']
        minimum = int(plus['quota']) + int(plus['min_addon'])
        set_state(event.sender_id, subscription_plus_quota=True)
        await event.answer()
        await safe_edit(
            event,
            t(DataManager.get_user_language(event.sender_id), "plans.plus_prompt",
              base=plus['quota'], minimum=minimum, unit=plus['addon_unit_price']),
            buttons=[[back_button(b"buy_vip", user_id=event.sender_id)]],
        )

    @bot.on(events.NewMessage)
    async def plus_quota_input(event):
        if event.text and event.text.startswith("/"):
            return
        state = get_state(event.sender_id)
        if not state.get('subscription_plus_quota'):
            return
        clear_state(event.sender_id)
        try:
            quota = int((event.text or '').strip())
            quote = DataManager.quote_subscription('plus', quota)
        except (TypeError, ValueError) as error:
            await event.respond(t(DataManager.get_user_language(event.sender_id), "plans.plus_invalid", error=error))
            return
        await show_period_selection(event, 'plus', quota)

    async def check_payment(event, confirmed=False):
        prefix = 'manual_confirm_' if confirmed else 'check_payment_'
        order_id = event.data.decode().removeprefix(prefix)
        order = payment_system.pending_orders.get(order_id)
        if not order or order.get('user_id') != event.sender_id:
            await event.answer(t(DataManager.get_user_language(event.sender_id), "plans.order_wrong_user"), alert=True)
            return
        await event.answer(t(DataManager.get_user_language(event.sender_id), "plans.checking"))
        result = await payment_system.check_order_status(order_id)
        if result.get('success') and result.get('status') == 'paid':
            await safe_edit(
                event,
                _success_message(payment_system, event.sender_id, order_id),
                buttons=back_to_main_buttons(user_id=event.sender_id),
            )
        elif result.get('success'):
            await event.answer(t(DataManager.get_user_language(event.sender_id), "plans.unpaid"), alert=True)
        else:
            language = DataManager.get_user_language(event.sender_id)
            await event.answer(t(language, "plans.check_failed", error=result.get('error', t(language, "plans.unknown_error"))), alert=True)

    @bot.on(events.CallbackQuery(pattern=rb"^cancel_order_.+"))
    async def cancel_payment_order(event):
        order_id = event.data.decode().removeprefix('cancel_order_')
        result = await payment_system.cancel_order(order_id, event.sender_id)
        if result.get('success'):
            await render_purchase(event, t(DataManager.get_user_language(event.sender_id), "plans.cancelled"))
            return
        if result.get('status') == 'paid':
            await event.answer(t(DataManager.get_user_language(event.sender_id), "plans.already_paid"), alert=True)
            await safe_edit(
                event,
                _success_message(payment_system, event.sender_id, order_id),
                buttons=back_to_main_buttons(user_id=event.sender_id),
            )
            return
        await event.answer(result.get('error', t(DataManager.get_user_language(event.sender_id), "plans.cancel_failed")), alert=True)

    @bot.on(events.CallbackQuery(pattern=rb"check_payment_.+"))
    async def check_payment_status(event):
        await check_payment(event)

    @bot.on(events.CallbackQuery(pattern=rb"manual_confirm_.+"))
    async def manual_confirm_payment(event):
        await check_payment(event, confirmed=True)
