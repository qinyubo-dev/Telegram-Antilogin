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


class SubscriptionPeriodPricingTests(unittest.TestCase):
    def setUp(self):
        self.original = copy.deepcopy(data_manager.user_data)
        data_manager.user_data.clear()
        data_manager.user_data.update(DataManager._default_data())
        data_manager.subscription_expiry_index.clear()

    def tearDown(self):
        data_manager.user_data.clear()
        data_manager.user_data.update(self.original)
        DataManager.rebuild_subscription_index()

    def test_default_period_discounts(self):
        periods = DataManager.get_subscription_periods()
        self.assertEqual(periods[30]['discount_percent'], '0')
        self.assertEqual(periods[90]['discount_percent'], '8')
        self.assertEqual(periods[180]['discount_percent'], '18')
        self.assertEqual(periods[365]['discount_percent'], '25')

    def test_default_rounded_prices_for_all_plans(self):
        expected = {
            'go': {30: '0.6', 90: '1.8', 180: '3.6', 365: '7.2'},
            'plus': {30: '1', 90: '2.5', 180: '4.5', 365: '9'},
            'pro': {30: '3', 90: '8', 180: '14.5', 365: '27'},
        }
        for plan_id, periods in expected.items():
            for days, price in periods.items():
                with self.subTest(plan_id=plan_id, days=days):
                    quote = DataManager.quote_subscription(plan_id, period_days=days)
                    self.assertEqual(quote['price'], price)
                    self.assertEqual(quote['period_days'], days)

    def test_custom_plus_long_period_rounds_but_30_day_price_stays_exact(self):
        self.assertEqual(DataManager.quote_subscription('plus', 16, 30)['price'], '1.6')
        quote = DataManager.quote_subscription('plus', 16, 90)
        self.assertEqual(quote['list_price'], '4.8')
        self.assertEqual(quote['price'], '4')
        self.assertEqual(quote['effective_monthly_price'], '1.33333333')

    def test_go_never_receives_period_discount(self):
        quote = DataManager.quote_subscription('go', period_days=90)
        self.assertEqual(quote['configured_discount_percent'], '0')
        self.assertEqual(quote['actual_discount_percent'], '0')
        self.assertEqual(quote['discount_amount'], '0')
        self.assertEqual(quote['list_price'], quote['price'])

        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(DataManager.set_subscription_periods({
                30: 0, 90: 50, 180: 50, 365: 50,
            }))
        annual = DataManager.quote_subscription('go', period_days=365)
        self.assertEqual(annual['price'], '7.2')
        self.assertEqual(annual['pricing_days'], 360)
        self.assertEqual(annual['actual_discount_percent'], '0')

    def test_go_long_period_order_text_does_not_claim_a_discount(self):
        quote = DataManager.quote_subscription('go', period_days=365)
        text = _upgrade_order_text({
            'billing_mode': 'full_period', 'period_days': 365,
            'list_price': quote['list_price'], 'amount': quote['price'],
            'actual_discount_percent': quote['actual_discount_percent'],
        })
        self.assertNotIn('周期礼遇', text)
        self.assertNotIn('实际节省', text)

    def test_period_configuration_validation_and_update(self):
        candidate = {30: 0, 90: 10, 180: 20, 365: 30}
        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(DataManager.set_subscription_periods(candidate))
        self.assertEqual(DataManager.get_subscription_periods()[365]['discount_percent'], '30')
        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertFalse(DataManager.set_subscription_periods({30: 1, 90: 8, 180: 18, 365: 25}))
            self.assertFalse(DataManager.set_subscription_periods({30: 0, 90: 100, 180: 18, 365: 25}))

    def test_long_period_fulfillment_extends_and_stores_effective_price(self):
        order = {
            'type': 'subscription_purchase', 'user_id': 31,
            'plan_id': 'plus', 'quota': 10, 'period_days': 180,
            'amount': '4.5', 'coin': 'USDT', 'status': 'paid', 'processed': False,
            'billing_mode': 'full_period', 'catalog_price': '1',
            'effective_monthly_price': '0.75',
        }
        orders = {'long-period': order}
        before = datetime.now()
        with patch.object(DataManager, 'save_payment_orders', return_value=True), patch.object(
            DataManager, 'save_user_data', return_value=True
        ):
            self.assertTrue(DataManager.fulfill_subscription_payment('long-period', orders))
        subscription = data_manager.user_data[31]['subscription']
        expiry = datetime.fromisoformat(subscription['expires_at'])
        self.assertAlmostEqual((expiry - before).total_seconds(), 180 * 86400, delta=2)
        segment = subscription['billing_segments'][0]
        self.assertEqual(segment['monthly_price'], '0.75')
        self.assertEqual(segment['order_id'], 'long-period')

    def test_long_period_order_text_shows_real_saving(self):
        text = _upgrade_order_text({
            'billing_mode': 'full_period', 'period_days': 365,
            'list_price': '12', 'amount': '9',
            'actual_discount_percent': '25',
        })
        self.assertIn('服务周期  ·  365 天', text)
        self.assertIn('实际节省 25%', text)
        self.assertIn('应付金额  ·  9 USDT', text)


class SubscriptionPeriodPaymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orders_patch = patch.object(DataManager, 'get_payment_orders', return_value={})
        self.orders_patch.start()
        self.payment = PaymentSystem()

    async def asyncTearDown(self):
        self.orders_patch.stop()
        await self.payment.stop_monitoring()

    async def test_long_period_order_snapshots_discount_details(self):
        self.payment.create_payment_link = AsyncMock(return_value={
            'success': True, 'order_id': 'period-order', 'pay_url': 'https://pay',
        })
        with patch.object(DataManager, 'is_admin', return_value=False), patch.object(
            DataManager, 'classify_subscription_change', return_value='new'
        ):
            result = await self.payment.create_subscription_payment(
                41, 'plus', 10, period_days=365
            )
        self.assertTrue(result['success'])
        kwargs = self.payment.create_payment_link.await_args.kwargs
        self.assertEqual(kwargs['amount'], '9')
        self.assertEqual(
            kwargs['name'], '🥈 PLUS · 进阶方案｜365天尊享订阅'
        )
        metadata = kwargs['_order_metadata']
        self.assertEqual(metadata['period_days'], 365)
        self.assertEqual(metadata['list_price'], '12')
        self.assertEqual(metadata['pricing_days'], 360)
        self.assertEqual(metadata['configured_discount_percent'], '25')
        self.assertEqual(metadata['effective_monthly_price'], '0.73972603')

    async def test_upgrade_ignores_requested_long_period(self):
        upgrade = {
            'billing_mode': 'prorated_upgrade', 'amount': '0.25',
            'target_expires_at': (datetime.now() + timedelta(days=15)).isoformat(),
        }
        self.payment.create_payment_link = AsyncMock(return_value={
            'success': True, 'order_id': 'upgrade', 'pay_url': 'https://pay',
        })
        with patch.object(DataManager, 'is_admin', return_value=False), patch.object(
            DataManager, 'classify_subscription_change', return_value='upgrade'
        ), patch.object(DataManager, 'quote_subscription_upgrade', return_value=upgrade):
            result = await self.payment.create_subscription_payment(
                42, 'plus', 10, period_days=365
            )
        self.assertTrue(result['success'])
        kwargs = self.payment.create_payment_link.await_args.kwargs
        metadata = kwargs['_order_metadata']
        self.assertEqual(metadata['period_days'], 30)
        self.assertEqual(kwargs['amount'], '0.25')
        self.assertEqual(kwargs['name'], '🥈 PLUS · 进阶方案｜尊享权益升级')
        self.assertEqual(
            PaymentSystem._subscription_payment_name({
                'plan_id': 'plus', 'plan_name': 'Plus', 'quota': 15,
                'addon': 5, 'period_days': 30,
            }, upgrade=True),
            '🥈 PLUS · 进阶方案 · 专属席位｜尊享权益升级至15席',
        )

    async def test_payment_names_follow_vip_plan_hierarchy(self):
        self.payment.create_payment_link = AsyncMock(return_value={
            'success': True, 'order_id': 'named-order', 'pay_url': 'https://pay',
        })
        cases = (
            ('go', None, 30, '🥉 GO · 轻享方案｜30天尊享订阅'),
            ('plus', 15, 90, '🥈 PLUS · 进阶方案 · 专属席位｜90天尊享订阅｜15席'),
            ('pro', None, 180, '🥇 PRO · 尊享方案｜180天尊享订阅'),
        )
        with patch.object(DataManager, 'is_admin', return_value=False), patch.object(
            DataManager, 'classify_subscription_change', return_value='new'
        ):
            for plan_id, quota, period_days, expected_name in cases:
                with self.subTest(plan_id=plan_id, quota=quota):
                    result = await self.payment.create_subscription_payment(
                        43, plan_id, quota, period_days=period_days
                    )
                    self.assertTrue(result['success'])
                    self.assertEqual(
                        self.payment.create_payment_link.await_args.kwargs['name'],
                        expected_name,
                    )


if __name__ == '__main__':
    unittest.main()
