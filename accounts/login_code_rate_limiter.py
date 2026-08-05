# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

import settings as config
from localization import t
from storage.data_manager import DataManager


@dataclass(frozen=True)
class LoginCodeRateLimitResult:
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0
    min_interval_seconds: int = config.LOGIN_CODE_REQUEST_MIN_INTERVAL_SECONDS
    window_seconds: int = config.LOGIN_CODE_REQUEST_WINDOW_SECONDS
    max_requests: int = config.LOGIN_CODE_REQUEST_MAX_PER_WINDOW


class LoginCodeRateLimitMessage(str):
    """Localized rate-limit text that handlers can recognize after a race."""


class LoginCodeRequestRateLimiter:
    """Persisted per-user limiter shared by all login-code request entrypoints."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @staticmethod
    def _recent(timestamps: list[float], now: float) -> list[float]:
        cutoff = now - config.LOGIN_CODE_REQUEST_WINDOW_SECONDS
        return sorted(value for value in timestamps if value > cutoff)

    @staticmethod
    def _evaluate(timestamps: list[float], now: float) -> LoginCodeRateLimitResult:
        interval_retry = 0.0
        if timestamps:
            interval_retry = max(
                0.0,
                timestamps[-1]
                + config.LOGIN_CODE_REQUEST_MIN_INTERVAL_SECONDS
                - now,
            )

        window_retry = 0.0
        if len(timestamps) >= config.LOGIN_CODE_REQUEST_MAX_PER_WINDOW:
            window_retry = max(
                0.0,
                timestamps[0] + config.LOGIN_CODE_REQUEST_WINDOW_SECONDS - now,
            )

        retry = max(interval_retry, window_retry)
        if retry <= 0:
            return LoginCodeRateLimitResult(allowed=True)
        reason = (
            "window"
            if window_retry >= interval_retry and window_retry > 0
            else "interval"
        )
        return LoginCodeRateLimitResult(
            allowed=False,
            reason=reason,
            retry_after_seconds=max(1, math.ceil(retry)),
        )

    def check(
        self, user_id: int, *, now: Optional[float] = None
    ) -> LoginCodeRateLimitResult:
        """Check without consuming a request slot, pruning expired history."""
        user_id = int(user_id)
        if DataManager.is_admin(user_id) or not DataManager.is_data_ready():
            return LoginCodeRateLimitResult(allowed=True)
        current = time.time() if now is None else float(now)
        with self._lock:
            stored = DataManager.get_login_code_request_timestamps(user_id)
            recent = self._recent(stored, current)
            if recent != stored and not DataManager.set_login_code_request_timestamps(
                user_id, recent
            ):
                return LoginCodeRateLimitResult(allowed=False, reason="storage")
            return self._evaluate(recent, current)

    def acquire(
        self, user_id: int, *, now: Optional[float] = None
    ) -> LoginCodeRateLimitResult:
        """Atomically consume a slot immediately before calling Telegram."""
        user_id = int(user_id)
        if DataManager.is_admin(user_id) or not DataManager.is_data_ready():
            return LoginCodeRateLimitResult(allowed=True)
        current = time.time() if now is None else float(now)
        with self._lock:
            stored = DataManager.get_login_code_request_timestamps(user_id)
            recent = self._recent(stored, current)
            result = self._evaluate(recent, current)
            if not result.allowed:
                if recent != stored and not DataManager.set_login_code_request_timestamps(
                    user_id, recent
                ):
                    return LoginCodeRateLimitResult(allowed=False, reason="storage")
                return result
            recent.append(current)
            if not DataManager.set_login_code_request_timestamps(user_id, recent):
                return LoginCodeRateLimitResult(allowed=False, reason="storage")
            return result

    def prune_all(self, *, now: Optional[float] = None) -> bool:
        """Remove expired persisted history during startup recovery."""
        if not DataManager.is_data_ready():
            # Production calls this only after a successful load. Returning true
            # keeps isolated bootstrap tests and pre-load diagnostics side-effect free.
            return True
        current = time.time() if now is None else float(now)
        with self._lock:
            for user_id in DataManager.iter_login_code_request_rate_users():
                stored = DataManager.get_login_code_request_timestamps(user_id)
                recent = self._recent(stored, current)
                if recent != stored and not DataManager.set_login_code_request_timestamps(
                    user_id, recent
                ):
                    return False
        return True


def render_login_code_rate_limit(
    result: LoginCodeRateLimitResult, language: str
) -> str:
    if result.reason == "storage":
        return LoginCodeRateLimitMessage(
            t(language, "auth.login_code_rate_storage_failed")
        )
    window_key = (
        "auth.login_code_rate_window_minutes"
        if result.window_seconds % 60 == 0
        else "auth.login_code_rate_window_seconds"
    )
    window_value = (
        result.window_seconds // 60
        if result.window_seconds % 60 == 0
        else result.window_seconds
    )
    rule = t(
        language,
        "auth.login_code_rate_rule",
        interval=result.min_interval_seconds,
        window=t(language, window_key, value=window_value),
        max_requests=result.max_requests,
    )
    return LoginCodeRateLimitMessage(
        t(
            language,
            (
                "auth.login_code_rate_window_exceeded"
                if result.reason == "window"
                else "auth.login_code_rate_interval_exceeded"
            ),
            seconds=result.retry_after_seconds,
            rule=rule,
        )
    )


login_code_request_rate_limiter = LoginCodeRequestRateLimiter()
