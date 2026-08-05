# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

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


if __name__ == '__main__':
    unittest.main()
