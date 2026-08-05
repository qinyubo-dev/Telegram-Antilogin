# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import unittest

from storage import data_manager
from storage.data_manager import DataManager


class VipDetailsTests(unittest.TestCase):
    def setUp(self):
        self.original_user_data = data_manager.user_data
        data_manager.user_data = {}

    def tearDown(self):
        data_manager.user_data = self.original_user_data

    def test_get_subscription_uses_current_structure(self):
        data_manager.user_data[123] = {
            "subscription": {
                "plan_id": "pro",
                "quota": None,
                "expires_at": "2999-08-15T23:59:58.123456",
            },
        }

        subscription = DataManager.get_subscription(123)

        self.assertEqual(subscription["expires_at"], "2999-08-15T23:59:58.123456")

    def test_get_subscription_handles_missing_or_invalid_data(self):
        data_manager.user_data[123] = {
            "subscription": {"plan_id": "pro", "expires_at": "invalid"}
        }

        self.assertIsNone(DataManager.get_subscription(123))
        self.assertIsNone(DataManager.get_subscription(456))
