# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from handlers.admin_handlers import _get_user_display_name
from storage.user_profile_cache import UserProfileCache


class FakeEntity:
    def __init__(self, first_name=None, last_name=None, username=None):
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class FakeBot:
    def __init__(self, entity):
        self.entity = entity
        self.calls = 0

    async def get_entity(self, user_id):
        self.calls += 1
        return self.entity


class UserProfileCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_path = os.path.join(self.temp_dir.name, "profiles.json")
        self.path_patch = patch(
            "storage.user_profile_cache.PROFILE_CACHE_FILE", self.cache_path
        )
        self.path_patch.start()
        UserProfileCache._loaded = False
        UserProfileCache._profiles = {}

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_fresh_name_is_persisted_and_reused(self):
        bot = FakeBot(FakeEntity(first_name="Alice", last_name="Chen"))

        self.assertEqual(await _get_user_display_name(bot, 123), "Alice Chen")
        self.assertEqual(await _get_user_display_name(bot, 123), "Alice Chen")
        self.assertEqual(bot.calls, 1)

        with open(self.cache_path, encoding="utf-8") as cache_file:
            self.assertEqual(json.load(cache_file)["123"]["display_name"], "Alice Chen")

    async def test_user_without_name_is_cached_without_unknown_label(self):
        bot = FakeBot(FakeEntity())

        self.assertIsNone(await _get_user_display_name(bot, 456))
        self.assertIsNone(await _get_user_display_name(bot, 456))
        self.assertEqual(bot.calls, 1)

    async def test_entry_older_than_three_days_is_refreshed(self):
        UserProfileCache._loaded = True
        UserProfileCache._profiles = {
            "789": {
                "display_name": "Old Name",
                "updated_at": (datetime.now() - timedelta(days=3, seconds=1)).isoformat(),
            }
        }
        bot = FakeBot(FakeEntity(username="new_name"))

        self.assertEqual(await _get_user_display_name(bot, 789), "new_name")
        self.assertEqual(bot.calls, 1)

    async def test_legacy_entry_is_extended_with_username(self):
        UserProfileCache._loaded = True
        UserProfileCache._profiles = {
            "321": {"display_name": "Legacy", "updated_at": datetime.now().isoformat()}
        }

        changed = UserProfileCache.set_profile(321, "Legacy", "NewName")

        self.assertTrue(changed)
        self.assertEqual(UserProfileCache.get_profile(321)["username"], "newname")
