# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import copy
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from handlers.vip_handlers import _upgrade_order_text
from payments.payment_system import PaymentSystem
from storage import data_manager
from storage.data_manager import DataManager


class ProratedUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.original = copy.deepcopy(data_manager.user_data)
        data_manager.user_data.clear()
        data_manager.user_data.update(DataManager._default_data())
        data_manager.subscription_expiry_index.clear()
        self.now = datetime.now()

    def tearDown(self):
        data_manager.user_data.clear()
        data_manager.user_data.update(self.original)
        DataManager.rebuild_subscription_index()

    def _set_subscription(self, user_id, days, *, plan_id='go', quota=2,
                          monthly_price='0.5', segments=None):
        expiry = self.now + timedelta(days=days)
        subscription = {
            'plan_id': plan_id,
            'quota': quota,
            'starts_at': self.now.isoformat(),
            'expires_at': expiry.isoformat(),
            'selected_accounts': [],
            'selection_required': False,
        }
        if segments is not None:
            subscription['billing_segments'] = segments
        elif monthly_price is not None:
            subscription['billing_segments'] = [{
                'starts_at': self.now.isoformat(),
                'expires_at': expiry.isoformat(),
                'plan_id': plan_id,
                'quota': quota,
                'monthly_price': monthly_price,
                'price_source': 'order',
            }]
        data_manager.user_data[user_id] = {'subscription': subscription}
        DataManager.rebuild_subscription_index()

    def test_go_to_plus_prorates_common_remaining_durations(self):
        for user_id, days in enumerate((1, 15, 30, 45, 60), start=100):
            with self.subTest(days=days):
                self._set_subscription(user_id, days)
                quote = DataManager.quote_subscription_upgrade(
                    user_id, 'plus', 10, now=self.now
                )
                expected = Decimal('0.5') * Decimal(days) / Decimal(30)
                self.assertEqual(quote['amount'], DataManager._decimal_text(expected))
                self.assertEqual(quote['billable_days'], days)
                self.assertEqual(
                    quote['target_expires_at'],
                    (self.now + timedelta(days=days)).isoformat(),
                )

    def test_partial_day_rounds_up(self):
        self._set_subscription(110, 1)
        expiry = self.now + timedelta(hours=1)
        subscription = data_manager.user_data[110]['subscription']
        subscription['expires_at'] = expiry.isoformat()
        subscription['billing_segments'][0]['expires_at'] = expiry.isoformat()
        DataManager.rebuild_subscription_index()

        quote = DataManager.quote_subscription_upgrade(110, 'plus', 10, now=self.now)
        self.assertEqual(quote['billable_days'], 1)
        self.assertEqual(quote['amount'], '0.01666667')

    def test_multiple_purchase_prices_are_credited_per_segment(self):
        segments = [
            {
                'starts_at': self.now.isoformat(),
                'expires_at': (self.now + timedelta(days=30)).isoformat(),
                'plan_id': 'go', 'quota': 2, 'monthly_price': '0.5',
                'price_source': 'order', 'order_id': 'first',
            },
            {
                'starts_at': (self.now + timedelta(days=30)).isoformat(),
                'expires_at': (self.now + timedelta(days=60)).isoformat(),
                'plan_id': 'go', 'quota': 2, 'monthly_price': '0.6',
                'price_source': 'order', 'order_id': 'second',
            },
        ]
        self._set_subscription(111, 60, segments=segments)

        quote = DataManager.quote_subscription_upgrade(111, 'plus', 10, now=self.now)
        self.assertEqual(quote['source_value'], '1.1')
        self.assertEqual(quote['target_value'], '2')
        self.assertEqual(quote['amount'], '0.9')

    def test_plus_expansion_and_pro_upgrade_use_target_catalog_price(self):
        self._set_subscription(
            112, 30, plan_id='plus', quota=15, monthly_price='1.5'
        )
        expanded = DataManager.quote_subscription_upgrade(112, 'plus', 20, now=self.now)
        pro = DataManager.quote_subscription_upgrade(112, 'pro', None, now=self.now)
        self.assertEqual(expanded['amount'], '0.5')
        self.assertEqual(pro['amount'], '1.5')

    def test_legacy_subscription_uses_catalog_fallback(self):
        self._set_subscription(113, 15, monthly_price=None)
        quote = DataManager.quote_subscription_upgrade(113, 'plus', 10, now=self.now)
        self.assertTrue(quote['uses_catalog_fallback'])
        self.assertEqual(quote['source_value'], '0.3')

    def test_apply_upgrade_keeps_expiry_and_rejects_stale_snapshot(self):
        self._set_subscription(114, 30)
        quote = DataManager.quote_subscription_upgrade(114, 'plus', 10, now=self.now)
        expiry = quote['target_expires_at']

        self.assertTrue(DataManager.apply_prorated_upgrade(114, quote))
        upgraded = data_manager.user_data[114]['subscription']
        self.assertEqual(upgraded['plan_id'], 'plus')
        self.assertEqual(upgraded['expires_at'], expiry)
        self.assertEqual(upgraded['billing_segments'][0]['monthly_price'], '1')
        self.assertFalse(DataManager.apply_prorated_upgrade(114, quote))

    def test_renewals_keep_distinct_transaction_prices(self):
        self.assertTrue(DataManager.apply_subscription(
            115, 'go', 2, validate_catalog=False,
            billing_price='0.5', order_id='first',
        ))
        self.assertTrue(DataManager.apply_subscription(
            115, 'go', 2, validate_catalog=False,
            billing_price='0.6', order_id='second',
        ))
        segments = data_manager.user_data[115]['subscription']['billing_segments']
        self.assertEqual([item['monthly_price'] for item in segments], ['0.5', '0.6'])
        self.assertEqual([item['order_id'] for item in segments], ['first', 'second'])

    def test_fulfillment_uses_snapshot_after_catalog_change(self):
        self._set_subscription(116, 30)
        quote = DataManager.quote_subscription_upgrade(116, 'plus', 10, now=self.now)
        original_expiry = quote['target_expires_at']
        orders = {'upgrade-paid': {
            'type': 'subscription_purchase', 'user_id': 116,
            'plan_id': 'plus', 'quota': 10, 'period_days': 30,
            'amount': quote['amount'], 'coin': 'USDT',
            'status': 'paid', 'processed': False,
            'billing_mode': 'prorated_upgrade', 'upgrade_snapshot': quote,
        }}
        changed = DataManager.default_subscription_catalog()
        changed['plus']['price'] = '2'
        data_manager.user_data['subscription_catalog'] = changed
        with patch.object(DataManager, 'save_payment_orders', return_value=True), patch.object(
            DataManager, 'save_user_data', return_value=True
        ):
            self.assertTrue(DataManager.fulfill_subscription_payment('upgrade-paid', orders))
        subscription = data_manager.user_data[116]['subscription']
        self.assertEqual(subscription['expires_at'], original_expiry)
        self.assertEqual(subscription['billing_segments'][0]['monthly_price'], '1')
        self.assertTrue(orders['upgrade-paid']['processed'])

    def test_paid_stale_upgrade_moves_to_manual_review(self):
        self._set_subscription(117, 30)
        quote = DataManager.quote_subscription_upgrade(117, 'plus', 10, now=self.now)
        orders = {'stale-paid': {
            'type': 'subscription_purchase', 'user_id': 117,
            'plan_id': 'plus', 'quota': 10, 'period_days': 30,
            'amount': quote['amount'], 'coin': 'USDT',
            'status': 'paid', 'processed': False,
            'billing_mode': 'prorated_upgrade', 'upgrade_snapshot': quote,
        }}
        data_manager.user_data[117]['subscription']['expires_at'] = (
            self.now + timedelta(days=31)
        ).isoformat()
        with patch.object(DataManager, 'save_payment_orders', return_value=True):
            self.assertFalse(DataManager.fulfill_subscription_payment('stale-paid', orders))
        self.assertTrue(orders['stale-paid']['needs_manual_review'])
        self.assertFalse(orders['stale-paid']['processed'])

    def test_upgrade_order_text_shows_credit_and_unchanged_expiry(self):
        text = _upgrade_order_text({
            'billing_mode': 'prorated_upgrade',
            'amount': '0.25',
            'upgrade_snapshot': {
                'billable_days': 15,
                'source_value': '0.25',
                'target_value': '0.5',
                'target_expires_at': '2026-08-03T12:00:00',
                'uses_catalog_fallback': True,
            },
        })
        self.assertIn('应付差价  ·  0.25 USDT', text)
        self.assertIn('到期时间保持不变', text)
        self.assertIn('目录价估算', text)


class ProratedUpgradePaymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orders_patch = patch.object(DataManager, 'get_payment_orders', return_value={})
        self.orders_patch.start()
        self.payment = PaymentSystem()

    async def asyncTearDown(self):
        self.orders_patch.stop()
        await self.payment.stop_monitoring()

    async def test_create_upgrade_order_uses_prorated_amount_and_snapshot(self):
        upgrade = {
            'billing_mode': 'prorated_upgrade', 'amount': '0.25',
            'target_expires_at': '2026-08-03T12:00:00',
        }
        self.payment.create_payment_link = AsyncMock(return_value={
            'success': True, 'order_id': 'upgrade', 'pay_url': 'https://pay',
        })
        with patch.object(DataManager, 'is_admin', return_value=False), patch.object(
            DataManager, 'quote_subscription', return_value={
                'plan_id': 'plus', 'plan_name': 'Plus', 'quota': 10,
                'addon': 0, 'price': '1',
            }
        ), patch.object(
            DataManager, 'classify_subscription_change', return_value='upgrade'
        ), patch.object(
            DataManager, 'quote_subscription_upgrade', return_value=upgrade
        ):
            result = await self.payment.create_subscription_payment(7, 'plus', 10)

        self.assertTrue(result['success'])
        kwargs = self.payment.create_payment_link.await_args.kwargs
        self.assertEqual(kwargs['amount'], '0.25')
        self.assertEqual(kwargs['_order_metadata']['billing_mode'], 'prorated_upgrade')
        self.assertEqual(kwargs['_order_metadata']['upgrade_snapshot'], upgrade)

    async def test_duplicate_pending_subscription_order_is_rejected(self):
        self.payment.pending_orders['existing'] = {
            'type': 'subscription_purchase', 'user_id': 7,
            'status': 'pending', 'processed': False,
        }
        result = await self.payment.create_subscription_payment(7, 'plus', 10)
        self.assertFalse(result['success'])
        self.assertIn('未完成的订阅订单', result['error'])


if __name__ == '__main__':
    unittest.main()
