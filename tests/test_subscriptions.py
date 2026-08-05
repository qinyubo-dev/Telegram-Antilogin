# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from accounts.account_manager import AccountManager
from storage import data_manager
from storage.data_manager import DataManager


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.original = copy.deepcopy(data_manager.user_data)
        data_manager.user_data.clear()
        data_manager.user_data.update(DataManager._default_data())
        data_manager.subscription_expiry_index.clear()

    def tearDown(self):
        data_manager.user_data.clear()
        data_manager.user_data.update(self.original)
        DataManager.rebuild_subscription_index()

    def test_default_quotes_and_plus_expansion(self):
        self.assertEqual(DataManager.quote_subscription('go')['price'], '0.6')
        self.assertEqual(DataManager.quote_subscription('plus', 15)['price'], '1.5')
        self.assertEqual(DataManager.quote_subscription('plus', 16)['price'], '1.6')
        self.assertEqual(DataManager.quote_subscription('plus', 20)['price'], '2')
        self.assertIsNone(DataManager.quote_subscription('pro')['quota'])
        with self.assertRaises(ValueError):
            DataManager.quote_subscription('plus', 14)

    def test_subscription_badges_progress_without_using_admin_crown(self):
        self.assertEqual(DataManager.get_subscription_badge('go'), '🥉')
        self.assertEqual(DataManager.get_subscription_badge('plus'), '🥈')
        self.assertEqual(DataManager.get_subscription_badge('pro'), '🥇')
        self.assertEqual(DataManager.get_subscription_badge('admin'), '👑')
        self.assertEqual(DataManager.get_subscription_badge('unknown'), '✦')

    def test_get_subscription_is_pure_read(self):
        expiry = datetime.now() - timedelta(seconds=1)
        scheduled_expiry = datetime.now() + timedelta(days=12)
        data_manager.user_data[7] = {
            'subscription': {
                'plan_id': 'plus',
                'quota': 10,
                'starts_at': (expiry - timedelta(days=30)).isoformat(),
                'expires_at': expiry.isoformat(),
                'scheduled': {
                    'plan_id': 'pro',
                    'quota': None,
                    'starts_at': expiry.isoformat(),
                    'expires_at': scheduled_expiry.isoformat(),
                },
            },
        }
        before = copy.deepcopy(data_manager.user_data[7])
        subscription = DataManager.get_subscription(7, include_inactive=True)
        self.assertEqual(subscription['plan_id'], 'plus')
        self.assertEqual(data_manager.user_data[7], before)

    def test_due_subscription_activation_is_explicit_and_persisted(self):
        effective = datetime.now() - timedelta(seconds=1)
        data_manager.user_data[7] = {
            'subscription': {
                'plan_id': 'plus',
                'quota': 10,
                'starts_at': (effective - timedelta(days=30)).isoformat(),
                'expires_at': effective.isoformat(),
                'scheduled': {
                    'plan_id': 'pro',
                    'quota': None,
                    'starts_at': effective.isoformat(),
                    'expires_at': (effective + timedelta(days=30)).isoformat(),
                },
            },
        }
        with patch.object(DataManager, 'save_user_data', return_value=True) as save:
            self.assertEqual(DataManager.activate_due_subscriptions(), [7])
        save.assert_called_once()
        self.assertEqual(data_manager.user_data[7]['subscription']['plan_id'], 'pro')

    def test_due_subscription_activation_rolls_back_when_save_fails(self):
        effective = datetime.now() - timedelta(seconds=1)
        data_manager.user_data[7] = {
            'subscription': {
                'plan_id': 'plus',
                'quota': 10,
                'starts_at': (effective - timedelta(days=30)).isoformat(),
                'expires_at': effective.isoformat(),
                'scheduled': {
                    'plan_id': 'go',
                    'quota': 2,
                    'starts_at': effective.isoformat(),
                    'expires_at': (effective + timedelta(days=30)).isoformat(),
                },
            },
        }
        before = copy.deepcopy(data_manager.user_data[7])
        with patch.object(DataManager, 'save_user_data', return_value=False):
            self.assertEqual(DataManager.activate_due_subscriptions(), [])
        self.assertEqual(data_manager.user_data[7], before)

    def test_renew_upgrade_and_scheduled_downgrade(self):
        self.assertTrue(DataManager.apply_subscription(8, 'go', 2))
        first_expiry = datetime.fromisoformat(data_manager.user_data[8]['subscription']['expires_at'])
        self.assertTrue(DataManager.apply_subscription(8, 'plus', 15))
        upgraded = data_manager.user_data[8]['subscription']
        self.assertEqual(upgraded['plan_id'], 'plus')
        self.assertAlmostEqual(
            (datetime.fromisoformat(upgraded['expires_at']) - first_expiry).total_seconds(),
            timedelta(days=30).total_seconds(), delta=1,
        )
        self.assertTrue(DataManager.apply_subscription(8, 'go', 2))
        scheduled = data_manager.user_data[8]['subscription']['scheduled']
        self.assertEqual(scheduled['plan_id'], 'go')
        self.assertEqual(scheduled['starts_at'], upgraded['expires_at'])

    def test_conflicting_scheduled_change_is_rejected(self):
        DataManager.apply_subscription(9, 'pro', None)
        DataManager.apply_subscription(9, 'go', 2)
        self.assertEqual(DataManager.classify_subscription_change(9, 'plus', 10), 'conflict')

    def test_expired_downgrade_clears_selection_and_requires_confirmation(self):
        expired = datetime.now() - timedelta(minutes=1)
        data_manager.user_data[12] = {
            'subscription': {
                'plan_id': 'plus', 'quota': 15,
                'starts_at': (expired - timedelta(days=30)).isoformat(),
                'expires_at': expired.isoformat(),
                'selected_accounts': [str(number) for number in range(100, 115)],
                'selection_required': False,
            },
        }

        self.assertTrue(DataManager.apply_subscription(
            12, 'go', 2, validate_catalog=False
        ))
        subscription = data_manager.user_data[12]['subscription']
        self.assertEqual(subscription['selected_accounts'], [])
        self.assertTrue(subscription['selection_required'])

    def test_expired_pro_to_finite_plan_requires_confirmation(self):
        expired = datetime.now() - timedelta(minutes=1)
        data_manager.user_data[13] = {
            'subscription': {
                'plan_id': 'pro', 'quota': None,
                'starts_at': (expired - timedelta(days=30)).isoformat(),
                'expires_at': expired.isoformat(),
                'selected_accounts': ['111', '222'],
            },
        }

        self.assertTrue(DataManager.apply_subscription(
            13, 'go', 2, validate_catalog=False
        ))
        subscription = data_manager.user_data[13]['subscription']
        self.assertEqual(subscription['selected_accounts'], [])
        self.assertTrue(subscription['selection_required'])

    def test_expired_same_plan_preserves_selection(self):
        expired = datetime.now() - timedelta(minutes=1)
        data_manager.user_data[14] = {
            'subscription': {
                'plan_id': 'go', 'quota': 2,
                'starts_at': (expired - timedelta(days=30)).isoformat(),
                'expires_at': expired.isoformat(),
                'selected_accounts': ['111'],
                'selection_required': False,
            },
        }

        self.assertTrue(DataManager.apply_subscription(
            14, 'go', 2, validate_catalog=False
        ))
        subscription = data_manager.user_data[14]['subscription']
        self.assertEqual(subscription['selected_accounts'], ['111'])
        self.assertFalse(subscription['selection_required'])

    def test_scheduled_downgrade_clears_selection_when_activated(self):
        DataManager.apply_subscription(15, 'plus', 15)
        data_manager.user_data[15]['subscription']['selected_accounts'] = ['111', '222']
        DataManager.apply_subscription(15, 'go', 2)
        data_manager.user_data[15]['subscription']['scheduled']['starts_at'] = (
            datetime.now() - timedelta(seconds=1)
        ).isoformat()

        self.assertTrue(DataManager._activate_scheduled_subscription(15))
        subscription = data_manager.user_data[15]['subscription']
        self.assertEqual(subscription['selected_accounts'], [])
        self.assertTrue(subscription['selection_required'])

    def test_selection_is_staged_until_finalized(self):
        DataManager.apply_subscription(16, 'go', 2)
        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(DataManager.set_selected_accounts(
                16, ['+111'], finalize=False
            ))
            self.assertTrue(data_manager.user_data[16]['subscription']['selection_required'])
            self.assertEqual(
                data_manager.user_data[16]['subscription']['selected_accounts'], ['111']
            )
            self.assertTrue(DataManager.set_selected_accounts(16, ['111'], finalize=True))
        self.assertFalse(data_manager.user_data[16]['subscription']['selection_required'])

    def test_quota_counts_retained_session_files(self):
        DataManager.apply_subscription(10, 'go', 2)
        with tempfile.TemporaryDirectory() as directory, patch(
            'accounts.account_manager.SESSIONS_DIR', directory
        ):
            open(os.path.join(directory, '10_111.session'), 'wb').close()
            open(os.path.join(directory, '10_222.session'), 'wb').close()
            self.assertTrue(AccountManager.get_quota_status(10)['full'])
            self.assertFalse(AccountManager.can_add_hosted_account(10, '+333'))
            self.assertTrue(AccountManager.can_add_hosted_account(10, '+111'))

    def test_paid_order_uses_catalog_snapshot_after_catalog_change(self):
        order = {
            'type': 'subscription_purchase', 'user_id': 11,
            'plan_id': 'plus', 'quota': 15, 'period_days': 30,
            'amount': '1.5', 'coin': 'USDT', 'status': 'paid', 'processed': False,
        }
        orders = {'snapshot-order': order}
        changed = DataManager.default_subscription_catalog()
        changed['plus']['quota'] = 20
        data_manager.user_data['subscription_catalog'] = changed
        with patch.object(DataManager, 'save_payment_orders', return_value=True), patch.object(
            DataManager, 'save_user_data', return_value=True
        ):
            self.assertTrue(DataManager.fulfill_subscription_payment('snapshot-order', orders))
        self.assertEqual(data_manager.user_data[11]['subscription']['quota'], 15)

class AccountSelectionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = copy.deepcopy(data_manager.user_data)
        data_manager.user_data.clear()
        data_manager.user_data.update(DataManager._default_data())
        data_manager.subscription_expiry_index.clear()

    def tearDown(self):
        data_manager.user_data.clear()
        data_manager.user_data.update(self.original)
        DataManager.rebuild_subscription_index()

    async def test_pending_selection_cannot_resume_draft_accounts(self):
        DataManager.apply_subscription(17, 'go', 2)
        subscription = data_manager.user_data[17]['subscription']
        subscription.update({
            'selected_accounts': ['111'],
            'selection_required': True,
        })
        with patch.object(
            AccountManager, 'hosted_account_phones', return_value={'111'}
        ), patch.object(
            AccountManager, 'create_client_from_session', new=AsyncMock()
        ) as create_client:
            self.assertEqual(await AccountManager.resume_selected_accounts(17), 0)
        create_client.assert_not_awaited()

    def test_new_hosted_account_is_added_to_finite_subscription_selection(self):
        DataManager.apply_subscription(20, 'go', 2)
        with patch.object(
            AccountManager, 'hosted_account_phones', return_value={'111'}
        ), patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(AccountManager.ensure_account_selected(20, '+111'))

        subscription = data_manager.user_data[20]['subscription']
        self.assertEqual(subscription['selected_accounts'], ['111'])
        self.assertFalse(subscription['selection_required'])

    async def test_confirmed_empty_selection_resumes_nothing(self):
        DataManager.apply_subscription(18, 'go', 2)
        subscription = data_manager.user_data[18]['subscription']
        subscription.update({
            'selected_accounts': [],
            'selection_required': False,
        })
        with patch.object(
            AccountManager, 'hosted_account_phones', return_value={'111', '222'}
        ), patch.object(
            AccountManager, 'create_client_from_session', new=AsyncMock()
        ) as create_client:
            self.assertEqual(await AccountManager.resume_selected_accounts(18), 0)
        create_client.assert_not_awaited()

    async def test_resume_never_exceeds_quota_with_corrupt_selection(self):
        DataManager.apply_subscription(19, 'go', 2)
        subscription = data_manager.user_data[19]['subscription']
        subscription.update({
            'selected_accounts': ['111', '222', '333'],
            'selection_required': False,
        })
        create_client = AsyncMock(return_value=(None, '+111', True, None))
        with patch.object(
            AccountManager, 'hosted_account_phones', return_value={'111', '222', '333'}
        ), patch('accounts.account_manager.os.path.exists', return_value=True), patch.object(
            AccountManager, 'create_client_from_session', create_client
        ):
            self.assertEqual(await AccountManager.resume_selected_accounts(19), 2)
        self.assertEqual(create_client.await_count, 2)


class SubscriptionAdminMutationTests(unittest.TestCase):
    def setUp(self):
        self.original = copy.deepcopy(data_manager.user_data)
        data_manager.user_data.clear()
        data_manager.user_data.update(DataManager._default_data())
        data_manager.subscription_expiry_index.clear()

    def tearDown(self):
        data_manager.user_data.clear()
        data_manager.user_data.update(self.original)
        DataManager.rebuild_subscription_index()

    def test_admin_grant_uses_exact_custom_days(self):
        before = datetime.now()
        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(DataManager.grant_subscription(70, 'go', 17))
        expiry = datetime.fromisoformat(
            data_manager.user_data[70]['subscription']['expires_at']
        )
        self.assertAlmostEqual(
            (expiry - before).total_seconds(), timedelta(days=17).total_seconds(), delta=2
        )

    def test_plus_grant_accepts_default_and_custom_quota(self):
        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(DataManager.grant_subscription(71, 'plus', 1))
            self.assertTrue(DataManager.grant_subscription(72, 'plus', 11, 15))
        default_quota = DataManager.get_subscription_catalog()['plus']['quota']
        self.assertEqual(data_manager.user_data[71]['subscription']['quota'], default_quota)
        self.assertEqual(data_manager.user_data[72]['subscription']['quota'], 15)

    def test_grant_rolls_back_when_save_fails(self):
        with patch.object(DataManager, 'save_user_data', return_value=False):
            self.assertFalse(DataManager.grant_subscription(73, 'pro', 9))
        self.assertNotIn(73, data_manager.user_data)
        self.assertFalse(DataManager.has_active_subscription(73))

    def test_delete_subscription_retains_non_subscription_data(self):
        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(DataManager.grant_subscription(74, 'pro', 8))
        data_manager.user_data[74]['hosted_accounts'] = {'111': {'name': 'retained'}}

        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(DataManager.delete_subscription(74))

        info = data_manager.user_data[74]
        self.assertEqual(info['hosted_accounts'], {'111': {'name': 'retained'}})
        for key in ('subscription', 'is_vip', 'vip_expiry', 'vip_added', 'vip_days'):
            self.assertNotIn(key, info)
        self.assertFalse(DataManager.has_active_subscription(74))

    def test_delete_rolls_back_when_save_fails(self):
        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertTrue(DataManager.grant_subscription(75, 'go', 6))
        previous = copy.deepcopy(data_manager.user_data[75])

        with patch.object(DataManager, 'save_user_data', return_value=False):
            self.assertFalse(DataManager.delete_subscription(75))

        self.assertEqual(data_manager.user_data[75], previous)
        self.assertTrue(DataManager.has_active_subscription(75))

    def test_invalid_days_and_admin_targets_are_rejected(self):
        with patch.object(DataManager, 'save_user_data', return_value=True):
            self.assertFalse(DataManager.grant_subscription(76, 'go', 0))
        with patch.object(DataManager, 'is_admin', return_value=True):
            self.assertFalse(DataManager.grant_subscription(76, 'go', 1))
            self.assertFalse(DataManager.delete_subscription(76))


if __name__ == '__main__':
    unittest.main()
