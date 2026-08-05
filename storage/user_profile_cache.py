# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import Optional

import settings


logger = logging.getLogger(__name__)

PROFILE_CACHE_FILE = settings.USER_PROFILE_CACHE_FILE
PROFILE_CACHE_TTL = timedelta(days=3)


class UserProfileCache:
    """Small persistent cache for Telegram user names used by admin search."""

    _loaded = False
    _profiles = {}

    @classmethod
    def _load(cls) -> None:
        if cls._loaded:
            return

        cls._loaded = True
        try:
            with open(PROFILE_CACHE_FILE, "r", encoding="utf-8") as cache_file:
                data = json.load(cache_file)
            if isinstance(data, dict):
                cls._profiles = data
        except FileNotFoundError:
            cls._profiles = {}
        except Exception:
            cls._profiles = {}
            logger.exception("Failed to load Telegram user profile cache")

    @classmethod
    def get(cls, user_id: int):
        """Return ``(is_fresh, display_name)``; a fresh name may be absent."""
        cls._load()
        entry = cls._profiles.get(str(user_id))
        if not isinstance(entry, dict):
            return False, None

        display_name = entry.get("display_name")
        updated_at = entry.get("updated_at")
        if not updated_at:
            return False, None

        try:
            if datetime.now() - datetime.fromisoformat(updated_at) >= PROFILE_CACHE_TTL:
                return False, None
        except (TypeError, ValueError):
            return False, None
        return True, str(display_name) if display_name else None

    @classmethod
    def get_profile(cls, user_id: int):
        """Return a copy of a cached profile, including stale entries."""
        cls._load()
        entry = cls._profiles.get(str(user_id))
        return dict(entry) if isinstance(entry, dict) else None

    @classmethod
    def set_profile(
        cls, user_id: int, display_name: Optional[str], username: Optional[str] = None
    ) -> bool:
        """Persist a profile only when it changed or its cache entry is stale."""
        display_name = display_name.strip() if display_name else None
        username = username.strip().lstrip("@").lower() if username else None
        cls._load()
        key = str(int(user_id))
        previous = cls._profiles.get(key) if isinstance(cls._profiles.get(key), dict) else {}
        fresh = False
        try:
            fresh = datetime.now() - datetime.fromisoformat(
                str(previous.get("updated_at", ""))
            ) < PROFILE_CACHE_TTL
        except (TypeError, ValueError):
            pass
        if (
            fresh
            and previous.get("display_name") == display_name
            and previous.get("username") == username
        ):
            return False
        cls._profiles[key] = {
            "display_name": display_name,
            "username": username,
            "updated_at": datetime.now().isoformat(),
        }
        cls._save()
        return True

    @classmethod
    def set_entity(cls, entity) -> bool:
        user_id = getattr(entity, "id", None)
        if not user_id:
            return False
        display_name = " ".join(
            part.strip()
            for part in (
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            )
            if part and part.strip()
        ) or None
        return cls.set_profile(user_id, display_name, getattr(entity, "username", None))

    @classmethod
    def iter_profiles(cls):
        cls._load()
        for user_id, entry in list(cls._profiles.items()):
            if not isinstance(entry, dict):
                continue
            try:
                parsed_id = int(user_id)
            except (TypeError, ValueError):
                continue
            yield parsed_id, dict(entry)

    @classmethod
    def _save(cls) -> None:
        cache_dir = os.path.dirname(os.path.abspath(PROFILE_CACHE_FILE))
        os.makedirs(cache_dir, exist_ok=True)
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix="user_profile_cache.", suffix=".tmp", dir=cache_dir, text=True
            )
            with os.fdopen(fd, "w", encoding="utf-8") as cache_file:
                json.dump(cls._profiles, cache_file, ensure_ascii=False, indent=2)
                cache_file.flush()
                os.fsync(cache_file.fileno())
            os.replace(temp_path, PROFILE_CACHE_FILE)
        except Exception:
            logger.exception("Failed to save Telegram user profile cache")
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
