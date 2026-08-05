# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import aiohttp
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from payments.payment_system import PaymentSystem
from storage import data_manager
from storage.data_manager import DataManager


TOKEN = "TESTtoken123456789abcdefghijABCD"


class OkayPaySignatureTests(unittest.TestCase):
    def test_official_request_vector(self):
        payload = {
            "id": 10001,
            "amount": "100.5",
            "coin": "USDT",
            "unique_id": "ORDER-20260628-001",
            "timestamp": 1782680000,
            "nonce": "a1b2c3d4e5",
        }
        self.assertEqual(
            PaymentSystem.build_base(payload),
            "amount=100.5&coin=USDT&id=10001&nonce=a1b2c3d4e5&timestamp=1782680000&unique_id=ORDER-20260628-001",
        )
        self.assertEqual(
            PaymentSystem.calculate_signature(payload, TOKEN),
            "7444ADFD8E4F4DA09D752DDF9345E0EE56DC25090FCFAF675DD042830E5E3F79",
        )

    def test_official_callback_vector(self):
        payload = {
            "status": "success",
            "code": 200,
            "data": {
                "order_id": "abc123def456",
                "unique_id": "ORDER-20260628-001",
                "pay_user_id": 123456789,
                "amount": "100.5",
                "coin": "USDT",
                "status": 1,
                "type": "deposit",
            },
            "id": 10001,
        }
        self.assertEqual(
            PaymentSystem.calculate_signature(payload, TOKEN),
            "64B09C8847849FA6921D8FFBDF8E406D4A8EA623E53970712350F61783403F7D",
        )

    def test_official_boundary_vector(self):
        payload = {
            "id": 7,
            "a": "0",
            "b": 0,
            "c": "",
            "d": None,
            "e": False,
            "f": "hello",
            "nest": {"x": "1", "y": "2"},
        }
        self.assertEqual(
            PaymentSystem.build_base(payload),
            "a=0&b=0&e=false&f=hello&id=7&nest.x=1&nest.y=2",
        )
        self.assertEqual(
            PaymentSystem.calculate_signature(payload, TOKEN),
            "8BC0AF979075038025DDD51B6F4A2E6CF3FF9B5B5371EB2268D303F89883E92A",
        )

    def test_verify_rejects_missing_and_tampered_signatures(self):
        payload = {"status": "success", "code": 200, "id": 1}
        self.assertFalse(PaymentSystem.verify_signature(payload, TOKEN))
        payload["sign"] = PaymentSystem.calculate_signature(payload, TOKEN)
        self.assertTrue(PaymentSystem.verify_signature(payload, TOKEN))
        payload["code"] = 201
        self.assertFalse(PaymentSystem.verify_signature(payload, TOKEN))

    def test_amount_normalization_preserves_whole_number_zeroes(self):
        self.assertEqual(PaymentSystem._normalize_amount("100.00"), "100")
        self.assertEqual(PaymentSystem._normalize_amount("0.0100"), "0.01")


class PaymentSystemAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orders_patch = patch.object(DataManager, "get_payment_orders", return_value={})
        self.orders_patch.start()
        self.payment = PaymentSystem()
        self.payment.merchant_id = "10001"
        self.payment.token = TOKEN

    async def asyncTearDown(self):
        await self.payment.stop_monitoring()
        self.orders_patch.stop()

    async def test_sign_adds_fresh_replay_protection_fields(self):
        first = self.payment.sign({"amount": "1"})
        second = self.payment.sign({"amount": "1"})
        self.assertIn("timestamp", first)
        self.assertEqual(len(first["nonce"]), 32)
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertTrue(self.payment.verify(first))

    async def test_api_response_requires_http_200_and_valid_signature(self):
        payload = {
            "status": "success",
            "code": 200,
            "data": {"order_id": "order-1"},
            "id": 10001,
        }
        payload["sign"] = PaymentSystem.calculate_signature(payload, TOKEN)
        valid = self.payment._parse_api_response("payLink", json.dumps(payload), 200)
        self.assertTrue(valid["success"])

        payload["data"]["order_id"] = "tampered"
        invalid = self.payment._parse_api_response("payLink", json.dumps(payload), 200)
        self.assertFalse(invalid["success"])
        self.assertIn("验签失败", invalid["error"])
        self.assertFalse(
            self.payment._parse_api_response("payLink", json.dumps(payload), 500)["success"]
        )

    async def test_api_warning_returns_new_protocol_msg(self):
        result = self.payment._parse_api_response(
            "payLink", json.dumps({"status": "warning", "msg": "请求重复 (nonce 已使用)"})
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "请求重复 (nonce 已使用)")

    async def test_retryable_api_failures_are_classified_without_fallback(self):
        cases = (
            (500, "upstream failed", "http_5xx"),
            (502, "bad gateway", "http_5xx"),
            (429, "slow down", "rate_limited"),
            (200, "not-json", "invalid_response"),
        )
        for status, body, expected_kind in cases:
            with self.subTest(status=status):
                result = self.payment._parse_api_response(
                    "checkTransferByTxid", body, status, "text/plain"
                )
                self.assertFalse(result["success"])
                self.assertTrue(result["retryable"])
                self.assertEqual(result["error_kind"], expected_kind)
                self.assertFalse(result.get("fallback_allowed", False))

    async def test_timeout_and_network_failures_are_retryable(self):
        class FailingSession:
            closed = False

            def __init__(self, error):
                self.error = error

            def post(self, *_args, **_kwargs):
                raise self.error

            async def close(self):
                self.closed = True

        for error, expected_kind in (
            (asyncio.TimeoutError(), "timeout"),
            (aiohttp.ClientConnectionError("down"), "network_error"),
        ):
            with self.subTest(expected_kind=expected_kind):
                self.payment._session = FailingSession(error)
                result = await self.payment._signed_request(
                    "checkTransferByTxid", {"txid": "order-1"}
                )
                self.assertFalse(result["success"])
                self.assertTrue(result["retryable"])
                self.assertEqual(result["error_kind"], expected_kind)
                self.assertFalse(result.get("fallback_allowed", False))

    async def test_query_fallback_only_runs_for_http_200_business_error(self):
        self.payment.pending_orders["order-1"] = {
            "unique_id": "unique-1",
            "status": "pending",
            "processed": False,
        }
        self.payment.query_order_by_id = AsyncMock(return_value={
            "success": False,
            "error": "not found",
            "error_kind": "business_error",
            "fallback_allowed": True,
        })
        self.payment.query_order_by_unique_id = AsyncMock(return_value={
            "success": True,
            "status": "pending",
        })
        result = await self.payment.check_order_status("order-1")
        self.assertEqual(result, {"success": True, "status": "pending"})
        self.payment.query_order_by_unique_id.assert_awaited_once_with("unique-1")

        self.payment.query_order_by_id.return_value = {
            "success": False,
            "error": "bad gateway",
            "error_kind": "http_5xx",
            "retryable": True,
        }
        self.payment.query_order_by_unique_id.reset_mock()
        result = await self.payment.check_order_status("order-1")
        self.assertEqual(result["error_kind"], "http_5xx")
        self.payment.query_order_by_unique_id.assert_not_awaited()

    async def test_order_retry_backoff_increases_and_success_resets_it(self):
        now = 1000.0
        failure = {"success": False, "retryable": True, "error_kind": "http_5xx"}
        for expected in (5, 10, 20, 40, 60, 60):
            self.payment._update_order_retry("order-1", failure, now)
            state = self.payment._order_retry_state["order-1"]
            self.assertEqual(state["next_check_at"], now + expected)
        self.payment._update_order_retry("order-1", {"success": True}, now)
        self.assertNotIn("order-1", self.payment._order_retry_state)

    async def test_provider_circuit_opens_half_opens_and_recovers(self):
        failure = {"success": False, "retryable": True, "error_kind": "http_5xx"}
        for _ in range(self.payment.provider_failure_threshold):
            await self.payment._record_provider_result(failure)
        allowed, _ = await self.payment._reserve_provider_request()
        self.assertFalse(allowed)

        self.payment._provider_open_until = time.monotonic() - 1
        first, second = await asyncio.gather(
            self.payment._reserve_provider_request(),
            self.payment._reserve_provider_request(),
        )
        self.assertEqual([first[0], second[0]].count(True), 1)
        probe_token = first[1] if first[0] else second[1]
        await self.payment._record_provider_result({"success": True}, probe_token)
        allowed, token = await self.payment._reserve_provider_request()
        self.assertTrue(allowed)
        self.assertIsNone(token)

    async def test_cancelled_half_open_probe_releases_its_token(self):
        started = asyncio.Event()

        class BlockingResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def text(self):
                started.set()
                await asyncio.Event().wait()

        class BlockingSession:
            closed = False

            def post(self, *_args, **_kwargs):
                return BlockingResponse()

            async def close(self):
                self.closed = True

        self.payment._session = BlockingSession()
        self.payment._provider_open_until = time.monotonic() - 1
        request = asyncio.create_task(
            self.payment._signed_request("checkDeposit", {"unique_id": "probe"})
        )
        await started.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request

        self.assertIsNone(self.payment._provider_half_open_token)
        allowed, probe_token = await self.payment._reserve_provider_request()
        self.assertTrue(allowed)
        self.assertIsNotNone(probe_token)
        await self.payment._release_provider_probe(probe_token)

    async def test_late_normal_result_cannot_close_open_circuit(self):
        self.payment._provider_open_until = time.monotonic() + 30
        await self.payment._record_provider_result({"success": True}, None)
        self.assertGreater(self.payment._provider_open_until, time.monotonic())

    async def test_all_provider_requests_share_global_concurrency_limit(self):
        active = 0
        peak = 0
        payload = {"status": "success", "code": 200, "data": {}, "id": 10001}
        payload["sign"] = PaymentSystem.calculate_signature(payload, TOKEN)

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def text(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return json.dumps(payload)

        class FakeSession:
            closed = False

            def post(self, *_args, **_kwargs):
                return FakeResponse()

            async def close(self):
                self.closed = True

        self.payment._session = FakeSession()
        results = await asyncio.gather(*(
            self.payment._signed_request("checkDeposit", {"unique_id": str(index)})
            for index in range(5)
        ))
        self.assertTrue(all(result["success"] for result in results))
        self.assertLessEqual(peak, self.payment.request_concurrency)

    async def test_create_link_sends_required_business_fields(self):
        self.payment._signed_request = AsyncMock(return_value={
            "success": True,
            "data": {"order_id": "order-1", "pay_url": "https://t.me/okpay", "status": 0},
        })
        with patch.object(self.payment, "_save_orders", return_value=True):
            result = await self.payment.create_payment_link(
                "unique-1", "100.00", "usdt", "VIP 30天", "https://t.me/AntiQin_bot"
            )
        self.assertTrue(result["success"])
        endpoint, request = self.payment._signed_request.await_args.args
        self.assertEqual(endpoint, "payLink")
        self.assertEqual(request, {
            "unique_id": "unique-1",
            "name": "VIP 30天",
            "amount": "100",
            "return_url": "https://t.me/AntiQin_bot",
            "coin": "USDT",
        })

    async def test_paid_order_requires_all_remote_fields_to_match(self):
        self.payment.pending_orders["order-1"] = {
            "unique_id": "unique-1",
            "amount": "10",
            "coin": "USDT",
            "status": "pending",
            "processed": False,
            "type": "subscription_purchase",
            "user_id": 7,
            "plan_id": "pro",
            "quota": None,
            "period_days": 30,
        }
        self.payment.query_order_by_id = AsyncMock(return_value={
            "success": True,
            "status": "paid",
            "order_id": "order-1",
            "unique_id": "unique-1",
            "amount": "9",
            "coin": "USDT",
            "pay_user_id": 8,
        })
        self.payment._process_paid_order_unlocked = AsyncMock(return_value=True)
        result = await self.payment.check_order_status("order-1")
        self.assertFalse(result["success"])
        self.payment._process_paid_order_unlocked.assert_not_awaited()

    async def test_monitor_checks_recent_orders_and_stops_old_orders(self):
        now = time.time()
        self.payment.pending_orders.update({
            "recent": {"created_time": now, "status": "pending", "processed": False},
            "old": {"created_time": now - 301, "status": "pending", "processed": False},
            "cancelled": {"created_time": now, "status": "cancelled", "processed": False},
        })
        self.payment._rebuild_active_order_ids()
        self.payment.check_order_status = AsyncMock(return_value={"success": True, "status": "pending"})
        with patch.object(self.payment, "_save_orders", return_value=True):
            await self.payment._monitor_pending_orders_once()
        self.payment.check_order_status.assert_awaited_once_with("recent")
        self.assertTrue(self.payment.pending_orders["old"]["auto_check_stopped"])

    async def test_cancel_pending_order_stops_future_checks(self):
        self.payment.pending_orders["order-1"] = {
            "user_id": 7,
            "status": "pending",
            "processed": False,
            "created_time": time.time(),
        }
        self.payment._check_order_status_unlocked = AsyncMock(
            return_value={"success": True, "status": "pending"}
        )
        with patch.object(self.payment, "_save_orders", return_value=True):
            result = await self.payment.cancel_order("order-1", 7)
        self.assertTrue(result["success"])
        order = self.payment.pending_orders["order-1"]
        self.assertEqual(order["status"], "cancelled")
        self.assertTrue(order["auto_check_stopped"])
        self.assertNotIn("order-1", self.payment._order_locks)

    async def test_monitor_ignores_order_cancelled_during_status_check(self):
        self.payment.pending_orders["order-race"] = {
            "user_id": 7,
            "status": "pending",
            "processed": False,
            "created_time": time.time(),
        }

        async def cancel_during_check(order_id):
            self.payment.pending_orders[order_id]["status"] = "cancelled"
            return {
                "success": False,
                "status": "cancelled",
                "error": "订单已取消",
            }

        self.payment.check_order_status = AsyncMock(side_effect=cancel_during_check)
        with patch.object(self.payment, "_update_order_retry") as update_retry:
            await self.payment._monitor_pending_orders_once()
        update_retry.assert_not_called()

    async def test_cancel_rejects_order_owned_by_another_user(self):
        self.payment.pending_orders["order-1"] = {
            "user_id": 7, "status": "pending", "processed": False,
        }
        result = await self.payment.cancel_order("order-1", 8)
        self.assertFalse(result["success"])
        self.assertEqual(self.payment.pending_orders["order-1"]["status"], "pending")

    async def test_unpaid_order_expires_after_two_hours_and_deletes_message(self):
        now = time.time()
        self.payment.order_expiry_window = 7200
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['expired-order'] = {
            'type': 'subscription_purchase',
            'user_id': 7,
            'status': 'pending',
            'processed': False,
            'created_time': now - 7201,
            'order_message_chat_id': 7,
            'order_message_id': 99,
        }
        self.payment._check_order_status_unlocked = AsyncMock(
            return_value={'success': True, 'status': 'pending'}
        )
        with patch.object(self.payment, '_save_orders', return_value=True):
            expired = await self.payment._expire_order_if_due('expired-order', now)

        self.assertTrue(expired)
        order = self.payment.pending_orders['expired-order']
        self.assertEqual(order['status'], 'expired')
        self.assertTrue(order['auto_check_stopped'])
        self.assertFalse(self.payment._has_open_subscription_order(7))
        self.payment.bot.delete_messages.assert_awaited_once_with(7, [99])
        self.payment.bot.send_message.assert_not_awaited()

    async def test_due_order_is_not_expired_when_final_check_finds_payment(self):
        now = time.time()
        self.payment.order_expiry_window = 7200
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['paid-at-expiry'] = {
            'type': 'subscription_purchase',
            'user_id': 7,
            'status': 'pending',
            'processed': False,
            'created_time': now - 7201,
            'order_message_chat_id': 7,
            'order_message_id': 100,
        }
        self.payment._check_order_status_unlocked = AsyncMock(
            return_value={'success': True, 'status': 'paid'}
        )
        with patch.object(self.payment, '_save_orders', return_value=True):
            expired = await self.payment._expire_order_if_due('paid-at-expiry', now)

        self.assertFalse(expired)
        self.assertEqual(self.payment.pending_orders['paid-at-expiry']['status'], 'pending')
        self.payment.bot.delete_messages.assert_not_awaited()

    async def test_due_order_is_not_expired_when_provider_query_fails(self):
        now = time.time()
        self.payment.order_expiry_window = 7200
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['query-failed'] = {
            'type': 'subscription_purchase',
            'user_id': 7,
            'status': 'pending',
            'processed': False,
            'created_time': now - 7201,
            'order_message_chat_id': 7,
            'order_message_id': 101,
        }
        self.payment._check_order_status_unlocked = AsyncMock(
            return_value={'success': False, 'error': '支付网络请求失败，请稍后重试'}
        )
        with patch.object(self.payment, '_save_orders', return_value=True):
            expired = await self.payment._expire_order_if_due('query-failed', now)

        self.assertFalse(expired)
        self.assertEqual(self.payment.pending_orders['query-failed']['status'], 'pending')
        self.assertTrue(self.payment._has_open_subscription_order(7))
        self.payment.bot.delete_messages.assert_not_awaited()

    async def test_paid_fulfillment_failure_is_never_overwritten_as_expired(self):
        now = time.time()
        self.payment.order_expiry_window = 7200
        self.payment.bot = AsyncMock()
        order = {
            'type': 'subscription_purchase',
            'user_id': 7,
            'status': 'pending',
            'processed': False,
            'created_time': now - 7201,
            'order_message_chat_id': 7,
            'order_message_id': 102,
        }
        self.payment.pending_orders['paid-failed'] = order

        async def paid_but_not_fulfilled(_order_id):
            order.update({
                'status': 'paid',
                'needs_manual_review': True,
                'manual_review_reason': 'subscription_state_changed',
            })
            return {'success': False, 'error': '支付已确认，但订阅发放失败'}

        self.payment._check_order_status_unlocked = AsyncMock(
            side_effect=paid_but_not_fulfilled
        )
        with patch.object(self.payment, '_save_orders', return_value=True):
            expired = await self.payment._expire_order_if_due('paid-failed', now)

        self.assertFalse(expired)
        self.assertEqual(order['status'], 'paid')
        self.assertTrue(order['needs_manual_review'])
        self.assertNotIn('expired_time', order)
        self.payment.bot.delete_messages.assert_not_awaited()

    async def test_concurrent_processing_fulfills_order_once(self):
        original_data = data_manager.user_data
        original_loaded = data_manager.data_load_succeeded
        try:
            data_manager.user_data = {}
            data_manager.data_load_succeeded = True
            self.payment.pending_orders["order-1"] = {
                "status": "paid",
                "processed": False,
                "type": "subscription_purchase",
                "user_id": 7,
                "plan_id": "pro",
                "quota": None,
                "period_days": 30,
                "amount": "10",
                "coin": "USDT",
            }
            def fulfill(order_id, orders):
                orders[order_id].update(
                    processed=True, status="paid", fulfilled_time=time.time()
                )
                return True

            with patch.object(
                DataManager, "fulfill_subscription_payment", side_effect=fulfill
            ), patch.object(self.payment, "_notify_fulfilled_order", new=AsyncMock()), patch.object(
                self.payment, "_notify_admin_new_subscription", new=AsyncMock()
            ):
                results = await asyncio.gather(
                    self.payment.process_paid_order("order-1"),
                    self.payment.process_paid_order("order-1"),
                )
            self.assertEqual(results.count(True), 1)
            self.assertTrue(self.payment.pending_orders["order-1"]["processed"])
        finally:
            data_manager.user_data = original_data
            data_manager.data_load_succeeded = original_loaded
            DataManager.rebuild_subscription_index()

    async def test_downgrade_notification_prompts_for_account_selection(self):
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['order-downgrade'] = {
            'type': 'subscription_purchase',
            'user_id': 7,
            'plan_id': 'go',
            'quota': 2,
            'amount': '0.5',
            'coin': 'USDT',
            'success_notified': False,
        }
        with patch.object(DataManager, 'get_subscription', return_value={
            'active': True,
            'selection_required': True,
        }), patch.object(self.payment, '_save_orders', return_value=True):
            await self.payment._notify_fulfilled_order('order-downgrade')

        message = self.payment.bot.send_message.await_args.args[1]
        self.assertIn('VIP 中心选择要恢复的账户', message)


    async def test_new_subscription_notifies_each_admin_once(self):
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['new-order'] = {
            'type': 'subscription_purchase',
            'change_type': 'new',
            'user_id': 7,
            'plan_id': 'plus',
            'quota': 15,
            'period_days': 90,
            'amount': '2.5',
            'coin': 'USDT',
        }
        with patch('payments.payment_system.config.ADMIN_IDS', [101, 202]), patch.object(
            self.payment, '_save_orders', return_value=True
        ):
            await self.payment._notify_admin_new_subscription('new-order')
            await self.payment._notify_admin_new_subscription('new-order')

        self.assertEqual(self.payment.bot.send_message.await_count, 2)
        self.assertEqual(
            self.payment.pending_orders['new-order']['admin_new_subscription_notified_to'],
            [101, 202],
        )
        message = self.payment.bot.send_message.await_args.args[1]
        self.assertIn('新订阅开通', message)
        self.assertIn('订阅方案：🥈 PLUS · 进阶方案 · 专属席位', message)
        self.assertIn('专属席位：15 席', message)
        self.assertNotIn('托管配额', message)

    async def test_standard_plan_admin_notification_hides_quota(self):
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['pro-order'] = {
            'type': 'subscription_purchase',
            'change_type': 'new',
            'user_id': 8,
            'plan_id': 'pro',
            'quota': None,
            'period_days': 180,
            'amount': '14.5',
            'coin': 'USDT',
        }
        with patch('payments.payment_system.config.ADMIN_IDS', [101]), patch.object(
            self.payment, '_save_orders', return_value=True
        ):
            await self.payment._notify_admin_new_subscription('pro-order')

        message = self.payment.bot.send_message.await_args.args[1]
        self.assertIn('订阅方案：🥇 PRO · 尊享方案', message)
        self.assertNotIn('托管配额', message)
        self.assertNotIn('专属席位：', message)

    async def test_renewal_does_not_send_new_subscription_notification(self):
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['renewal-order'] = {
            'type': 'subscription_purchase',
            'change_type': 'renewal',
        }
        with patch('payments.payment_system.config.ADMIN_IDS', [101]):
            await self.payment._notify_admin_new_subscription('renewal-order')
        self.payment.bot.send_message.assert_not_awaited()

    async def test_fulfillment_failure_notifies_admins_without_granting(self):
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['failed-order'] = {
            'type': 'subscription_purchase',
            'status': 'paid',
            'processed': False,
            'user_id': 7,
            'amount': '1',
            'coin': 'USDT',
            'manual_review_reason': 'subscription_state_changed',
        }
        with patch('payments.payment_system.config.ADMIN_IDS', [101]), patch.object(
            DataManager, 'fulfill_subscription_payment', return_value=False
        ), patch.object(self.payment, '_save_orders', return_value=True):
            result = await self.payment.process_paid_order('failed-order')

        self.assertFalse(result)
        self.assertFalse(self.payment.pending_orders['failed-order']['processed'])
        self.payment.bot.send_message.assert_awaited_once()
        message = self.payment.bot.send_message.await_args.args[1]
        self.assertIn('支付异常订单', message)
        self.assertIn('源订阅状态已发生变化', message)

    async def test_manual_payment_override_methods_are_removed(self):
        self.assertFalse(hasattr(self.payment, 'mark_order_as_paid'))
        self.assertFalse(hasattr(self.payment, 'manual_process_payment'))

    async def test_monitor_notifies_persisted_manual_review_order(self):
        self.payment.bot = AsyncMock()
        self.payment.pending_orders['review-order'] = {
            'type': 'subscription_purchase',
            'status': 'paid',
            'processed': False,
            'created_time': time.time() - 999,
            'needs_manual_review': True,
            'manual_review_reason': 'subscription_state_changed',
            'user_id': 7,
            'amount': '1',
            'coin': 'USDT',
        }
        self.payment._rebuild_active_order_ids()
        with patch('payments.payment_system.config.ADMIN_IDS', [101]), patch.object(
            self.payment, '_save_orders', return_value=True
        ):
            await self.payment._monitor_pending_orders_once()

        self.payment.bot.send_message.assert_awaited_once()
        self.assertIn(
            101,
            self.payment.pending_orders['review-order'][
                'admin_exception_subscription_state_changed_notified_to'
            ],
        )


class AtomicFulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.original_data = data_manager.user_data
        self.original_loaded = data_manager.data_load_succeeded
        data_manager.user_data = {}
        data_manager.data_load_succeeded = True

    def tearDown(self):
        data_manager.user_data = self.original_data
        data_manager.data_load_succeeded = self.original_loaded
        DataManager.rebuild_subscription_index()

    def test_legacy_vip_fulfillment_api_is_removed(self):
        self.assertFalse(hasattr(DataManager, "fulfill_vip_payment"))


if __name__ == "__main__":
    unittest.main()
