# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import logging
import secrets
import time

from telethon import TelegramClient, events
from telethon.tl.custom import Button

from handlers.handler_utils import require_admin
from payments.payment_system import PaymentSystem
from storage.data_manager import DataManager
from storage.admin_audit import AdminAuditLog


logger = logging.getLogger(__name__)


def _finish_audit(audit_id, event, action, result, target_type="system", target_id=None, **kwargs):
    return AdminAuditLog.record_result(
        audit_id, result, admin_id=event.sender_id, action=action,
        target_type=target_type, target_id=target_id, **kwargs
    )


async def setup_payment_handlers(bot: TelegramClient, payment_system: PaymentSystem):
    """Register read-only payment diagnostics and data maintenance commands."""

    @bot.on(events.NewMessage(pattern='/test_payment'))
    async def test_payment(event):
        """Create an isolated low-value payment link without changing product prices."""
        if not await require_admin(event):
            return
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "payment.test_create", "payment_test")
        unique_id = f"test-{event.sender_id}-{time.time_ns() // 1_000_000}-{secrets.token_hex(4)}"
        result = await payment_system.create_payment_link(
            unique_id=unique_id,
            amount='0.01',
            coin='USDT',
            name='支付链路测试',
            return_url=payment_system.return_url,
            _order_metadata={
                'user_id': int(event.sender_id),
                'type': 'payment_test',
                'test_order': True,
            },
        )
        if result.get('success'):
            audited = _finish_audit(
                audit_id, event, "payment.test_create", "success", "order", result["order_id"]
            )
            await event.respond(
                "🧪 支付测试订单已创建\n\n"
                f"订单号：`{result['order_id']}`\n"
                f"支付链接：{result['pay_url']}\n\n"
                "该订单仅用于验证支付链路，不会发放任何权益。"
                + ("\n⚠️ 订单已创建，但审计记录失败。" if not audited else ""),
                link_preview=False,
            )
        else:
            _finish_audit(
                audit_id, event, "payment.test_create", "failed", "payment_test",
                error=result.get("error"),
            )
            await event.respond(f"❌ 测试支付创建失败：{result.get('error', '未知错误')}")

    @bot.on(events.NewMessage(pattern='/check_order'))
    async def check_order_status(event):
        """Query the provider and process only a cryptographically verified payment."""
        if not await require_admin(event):
            return
        parts = event.text.split(' ', 1)
        target_hint = parts[1].strip() if len(parts) >= 2 else None
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "order.recheck", "order", target_hint)
        if len(parts) < 2:
            _finish_audit(audit_id, event, "order.recheck", "failed", "order", error="missing_order_id")
            await event.respond("❌ 格式错误，请使用：/check_order <订单号>")
            return
        order_id = parts[1].strip()
        before = payment_system.get_order_snapshot(order_id)
        result = await payment_system.check_order_status(order_id)
        audited = _finish_audit(
            audit_id, event, "order.recheck", "success" if result.get("success") else "failed",
            "order", order_id,
            before={"status": (before or {}).get("status"), "processed": (before or {}).get("processed")},
            after={
                "status": (payment_system.get_order_snapshot(order_id) or {}).get("status"),
                "processed": (payment_system.get_order_snapshot(order_id) or {}).get("processed"),
            },
            error=result.get("error"),
        )
        warning = "\n⚠️ 查单已执行，但审计记录失败。" if not audited else ""
        if result.get('success'):
            if result.get('status') == 'paid':
                await event.respond(f"✅ 订单 `{order_id}` 已由支付平台确认并完成处理{warning}")
            else:
                await event.respond(f"⏳ 订单 `{order_id}` 仍在等待支付{warning}")
        else:
            await event.respond(f"❌ 查询订单失败：{result.get('error', '未知错误')}{warning}")

    @bot.on(events.NewMessage(pattern='/reload_data'))
    async def reload_data_command(event):
        if not await require_admin(event):
            return
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "data.reload", "data_file")
        success = DataManager.load_user_data()
        if success:
            payment_system.pending_orders = DataManager.get_payment_orders()
            payment_system.processed_orders = {
                order_id
                for order_id, order in payment_system.pending_orders.items()
                if order.get('processed')
            }
            payment_system._rebuild_active_order_ids()
            audited = _finish_audit(audit_id, event, "data.reload", "success", "data_file")
            await event.respond("✅ 数据重新加载完成" + ("\n⚠️ 审计记录失败。" if not audited else ""))
        else:
            _finish_audit(audit_id, event, "data.reload", "failed", "data_file", error="load_failed")
            await event.respond("❌ 数据重新加载失败；原文件已备份且不会被覆盖")

    @bot.on(events.NewMessage(pattern='/check_unique_id'))
    async def check_unique_id(event):
        if not await require_admin(event):
            return
        parts = event.text.split(' ', 1)
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "order.search_unique_id", "order")
        if len(parts) < 2:
            _finish_audit(audit_id, event, "order.search_unique_id", "failed", "order", error="missing_unique_id")
            await event.respond("❌ 格式错误，请使用：/check_unique_id <unique_id>")
            return
        unique_id = parts[1].strip()
        order_id = await payment_system.find_order_by_unique_id(unique_id)
        if not order_id:
            _finish_audit(audit_id, event, "order.search_unique_id", "success", "order", metadata={"result_count": 0})
            await event.respond(f"❌ 未找到 unique_id 为 `{unique_id}` 的订单")
            return
        order = payment_system.pending_orders.get(order_id, {})
        _finish_audit(audit_id, event, "order.search_unique_id", "success", "order", order_id, metadata={"result_count": 1})
        await event.respond(
            "🔎 找到订单\n\n"
            f"订单号：`{order_id}`\n"
            f"状态：{order.get('status', 'unknown')}\n"
            f"用户 ID：`{order.get('user_id', 'N/A')}`\n"
            f"金额：{order.get('amount', 'N/A')} {order.get('coin', 'N/A')}"
        )

    @bot.on(events.NewMessage(pattern='/list_orders'))
    async def list_orders(event):
        if not await require_admin(event):
            return
        audit_id = AdminAuditLog.record_attempt(event.sender_id, "order.list", "order")
        total = payment_system.list_admin_orders("all", page_size=1)["total"]
        _finish_audit(audit_id, event, "order.list", "success", "order", metadata={"result_count": total})
        await event.respond(
            f"📋 支付订单\n\n共 {total} 条，请进入分页订单中心查看。",
            buttons=[[Button.inline("打开订单中心", b"admin_orders_all_0")]],
        )
