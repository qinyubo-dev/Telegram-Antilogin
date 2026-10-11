# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import asyncio
import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import bot_main
from accounts.account_manager import AccountManager
from storage import data_manager
from storage.data_manager import DataManager


class SubscriptionCoordinationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = copy.deepcopy(data_manager.user_data)
        data_manager.user_data.clear()
        data_manager.user_data.update(DataManager._default_data())
        data_manager.subscription_expiry_index.clear()

    def tearDown(self):
        data_manager.user_data.clear()
        data_manager.user_data.update(self.original)
        DataManager.rebuild_subscription_index()

    @staticmethod
    def _set_subscription(user_id, selected, selection_required=False):
        expiry = datetime.now() + timedelta(days=30)
        data_manager.user_data[user_id] = {
            'subscription': {
                'plan_id': 'go', 'quota': 2,
                'starts_at': datetime.now().isoformat(),
                'expires_at': expiry.isoformat(),
                'selected_accounts': selected,
                'selection_required': selection_required,
            },
        }
        DataManager.rebuild_subscription_index()

    async def test_oversized_selection_is_suspended_for_reselection(self):
        self._set_subscription(21, ['111', '222', '333'])
        suspend = AsyncMock(return_value=3)
        resume = AsyncMock(return_value=0)
        with patch.object(
            AccountManager, 'hosted_account_phones', return_value={'111', '222', '333'}
        ), patch.object(
            AccountManager, 'suspend_user_accounts', suspend
        ), patch.object(
            AccountManager, 'resume_selected_accounts', resume
        ), patch.object(DataManager, 'save_user_data', return_value=True):
            await bot_main._reconcile_user_subscription(21)

        subscription = data_manager.user_data[21]['subscription']
        self.assertEqual(subscription['selected_accounts'], [])
        self.assertTrue(subscription['selection_required'])
        suspend.assert_awaited_once_with(21)
        resume.assert_not_awaited()

    async def test_accounts_within_new_quota_are_auto_selected_and_resumed(self):
        self._set_subscription(22, [], selection_required=True)
        suspend = AsyncMock(return_value=0)
        resume = AsyncMock(return_value=2)
        with patch.object(
            AccountManager, 'hosted_account_phones', return_value={'111', '222'}
        ), patch.object(
            AccountManager, 'suspend_user_accounts', suspend
        ), patch.object(
            AccountManager, 'resume_selected_accounts', resume
        ), patch.object(DataManager, 'save_user_data', return_value=True):
            await bot_main._reconcile_user_subscription(22)

        subscription = data_manager.user_data[22]['subscription']
        self.assertEqual(subscription['selected_accounts'], ['111', '222'])
        self.assertFalse(subscription['selection_required'])
        suspend.assert_awaited_once_with(22, keep_selected=True)
        resume.assert_awaited_once_with(22)

    async def test_empty_finite_selection_is_repaired_before_suspending(self):
        self._set_subscription(23, [], selection_required=False)
        suspend = AsyncMock(return_value=0)
        resume = AsyncMock(return_value=2)
        with patch.object(
            AccountManager, 'hosted_account_phones', return_value={'111', '222'}
        ), patch.object(
            AccountManager, 'suspend_user_accounts', suspend
        ), patch.object(
            AccountManager, 'resume_selected_accounts', resume
        ), patch.object(DataManager, 'save_user_data', return_value=True):
            await bot_main._reconcile_user_subscription(23)

        subscription = data_manager.user_data[23]['subscription']
        self.assertEqual(subscription['selected_accounts'], ['111', '222'])
        self.assertFalse(subscription['selection_required'])
        suspend.assert_awaited_once_with(23, keep_selected=True)
        resume.assert_awaited_once_with(23)

    async def _reconcile_over_quota(self, user_id, since):
        self._set_subscription(user_id, [], selection_required=True)
        if since is not None:
            data_manager.user_data[user_id]['subscription'][
                'selection_required_since'] = since.isoformat()
        suspend = AsyncMock(return_value=0)
        resume = AsyncMock(return_value=2)
        notify = AsyncMock()
        with patch.object(
            AccountManager, 'hosted_account_phones', return_value={'111', '222', '333'}
        ), patch.object(
            AccountManager, 'suspend_user_accounts', suspend
        ), patch.object(
            AccountManager, 'resume_selected_accounts', resume
        ), patch.object(
            bot_main, '_notify_auto_selection', notify
        ), patch.object(DataManager, 'save_user_data', return_value=True):
            await bot_main._reconcile_user_subscription(user_id)
            await asyncio.gather(*list(bot_main._background_notifications))
        return suspend, resume, notify

    async def test_selection_ignored_for_24_hours_enables_random_accounts(self):
        suspend, resume, notify = await self._reconcile_over_quota(
            24, datetime.now() - timedelta(hours=25)
        )
        subscription = data_manager.user_data[24]['subscription']
        self.assertEqual(len(subscription['selected_accounts']), 2)
        self.assertTrue(set(subscription['selected_accounts']) <= {'111', '222', '333'})
        self.assertFalse(subscription['selection_required'])
        self.assertNotIn('selection_required_since', subscription)
        suspend.assert_awaited_once_with(24, keep_selected=True)
        resume.assert_awaited_once_with(24)
        notify.assert_awaited_once()

    async def test_selection_pending_under_24_hours_stays_suspended(self):
        suspend, resume, notify = await self._reconcile_over_quota(
            25, datetime.now() - timedelta(hours=1)
        )
        self.assertTrue(data_manager.user_data[25]['subscription']['selection_required'])
        suspend.assert_awaited_once_with(25)
        resume.assert_not_awaited()
        notify.assert_not_awaited()

    async def test_pending_selection_records_start_time(self):
        await self._reconcile_over_quota(26, None)
        subscription = data_manager.user_data[26]['subscription']
        self.assertIn('selection_required_since', subscription)
        self.assertTrue(subscription['selection_required'])

    async def test_one_user_failure_does_not_block_others(self):
        calls = []

        async def reconcile(user_id):
            calls.append(user_id)
            if user_id == 1:
                raise RuntimeError('boom')
            raise asyncio.CancelledError

        with patch.object(DataManager, 'activate_due_subscriptions'), patch.object(
            DataManager, 'get_subscription_user_ids', return_value=[1, 2]
        ), patch.object(bot_main, '_reconcile_user_subscription', reconcile):
            with self.assertRaises(asyncio.CancelledError):
                await bot_main._monitor_subscriptions()
        self.assertEqual(calls, [1, 2])


if __name__ == '__main__':
    unittest.main()
