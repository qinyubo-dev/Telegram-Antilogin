# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import aiohttp
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping

from telethon.errors import RPCError

from accounts import account_runtime
from storage.data_manager import DataManager
from localization import localized_payment_error, t
import settings as config


logger = logging.getLogger(__name__)


def _display_percent(value: Any) -> str:
    try:
        text = format(Decimal(str(value)).quantize(Decimal('0.1')), 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


class PaymentSystem:
    """OkayPay HMAC-SHA256 client with automatic pending-order polling."""

    def __init__(self):
        self.api_url = "https://api.okaypay.me/shop/"
        self.merchant_id = config.MERCHANT_ID
        self.token = config.PAYMENT_TOKEN
        self.return_url = getattr(config, "PAYMENT_RETURN_URL", "https://t.me/AntiQin_bot")
        self.poll_interval = float(getattr(config, "PAYMENT_POLL_INTERVAL_SECONDS", 5))
        self.auto_check_window = float(getattr(config, "PAYMENT_AUTO_CHECK_WINDOW_SECONDS", 300))
        self.order_expiry_window = float(getattr(config, "PAYMENT_ORDER_EXPIRY_SECONDS", 7200))
        self.request_timeout = float(getattr(config, "PAYMENT_REQUEST_TIMEOUT_SECONDS", 30))
        self.request_concurrency = max(
            1, int(getattr(config, "PAYMENT_REQUEST_CONCURRENCY", 2))
        )
        self.retry_backoff_max = max(
            self.poll_interval,
            float(getattr(config, "PAYMENT_RETRY_BACKOFF_MAX_SECONDS", 60)),
        )
        self.provider_failure_threshold = max(
            1, int(getattr(config, "PAYMENT_PROVIDER_FAILURE_THRESHOLD", 3))
        )
        self.provider_cooldown = max(
            self.poll_interval,
            float(getattr(config, "PAYMENT_PROVIDER_COOLDOWN_SECONDS", 30)),
        )
        self.pending_orders: Dict[str, Dict] = DataManager.get_payment_orders()
        self.processed_orders = {
            order_id
            for order_id, order in self.pending_orders.items()
            if isinstance(order, dict) and order.get("processed")
        }
        self._order_locks: Dict[str, asyncio.Lock] = {}
        self._subscription_user_locks: Dict[int, asyncio.Lock] = {}
        self._session: aiohttp.ClientSession | None = None
        self._request_semaphore = asyncio.Semaphore(self.request_concurrency)
        self._provider_state_lock = asyncio.Lock()
        self._provider_failure_count = 0
        self._provider_open_until = 0.0
        self._provider_half_open_token: object | None = None
        self._order_retry_state: Dict[str, Dict[str, float]] = {}
        self._active_order_ids: set[str] = set()
        self.monitoring_task: asyncio.Task | None = None
        self.bot = None
        self._rebuild_active_order_ids()

    def _save_orders(self) -> bool:
        saved = DataManager.save_payment_orders(self.pending_orders)
        if saved:
            self._rebuild_active_order_ids()
        return saved

    @staticmethod
    def _is_active_order(order: Mapping[str, Any]) -> bool:
        if order.get("legacy_origin") == "vip_purchase":
            return False
        if order.get("processed"):
            return False
        status = str(order.get("status", "pending"))
        if status in {"cancelled", "expired"}:
            return False
        if order.get("needs_manual_review") or status == "paid":
            return True
        return not order.get("auto_check_stopped")

    def _rebuild_active_order_ids(self) -> None:
        self._active_order_ids = {
            str(order_id)
            for order_id, order in self.pending_orders.items()
            if isinstance(order, dict) and self._is_active_order(order)
        }
        for order_id in list(self._order_retry_state):
            if order_id not in self._active_order_ids:
                self._order_retry_state.pop(order_id, None)
        for order_id, lock in list(self._order_locks.items()):
            if order_id not in self._active_order_ids and not lock.locked():
                self._order_locks.pop(order_id, None)

    def _get_order_lock(self, order_id: str) -> asyncio.Lock:
        lock = self._order_locks.get(order_id)
        if lock is None:
            lock = asyncio.Lock()
            self._order_locks[order_id] = lock
        return lock

    def _get_subscription_user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._subscription_user_locks.get(int(user_id))
        if lock is None:
            lock = asyncio.Lock()
            self._subscription_user_locks[int(user_id)] = lock
        return lock

    def _order_check_is_due(self, order_id: str, now: float) -> bool:
        state = self._order_retry_state.get(order_id)
        return not state or now >= state.get("next_check_at", 0)

    def _update_order_retry(self, order_id: str, result: Mapping[str, Any], now: float) -> None:
        if result.get("success") or not result.get("retryable"):
            self._order_retry_state.pop(order_id, None)
            return
        previous = self._order_retry_state.get(order_id, {})
        failures = int(previous.get("failures", 0)) + 1
        delay = min(self.poll_interval * (2 ** (failures - 1)), self.retry_backoff_max)
        retry_after = result.get("retry_after")
        if isinstance(retry_after, (int, float)):
            delay = max(delay, float(retry_after))
        self._order_retry_state[order_id] = {
            "failures": failures,
            "next_check_at": now + delay,
        }

    def _has_open_subscription_order(self, user_id: int) -> bool:
        return any(
            order.get('type') == 'subscription_purchase'
            and int(order.get('user_id', 0) or 0) == int(user_id)
            and not order.get('processed')
            and order.get('status') not in {'cancelled', 'expired'}
            for order in self.pending_orders.values()
        )

    @staticmethod
    def classify_admin_order(order: Mapping[str, Any]) -> str:
        if order.get("legacy_origin") == "vip_purchase":
            return "completed" if order.get("processed") else "closed"
        if order.get("processed"):
            return "completed"
        status = str(order.get("status", "pending"))
        if order.get("needs_manual_review") or status == "paid":
            return "review"
        if status in {"expired", "cancelled"}:
            return "closed"
        return "active"

    def get_order_snapshot(self, order_id: str) -> Dict[str, Any] | None:
        order = self.pending_orders.get(str(order_id))
        return copy.deepcopy(order) if isinstance(order, dict) else None

    def get_order_retry_snapshot(self, order_id: str) -> Dict[str, Any]:
        return copy.deepcopy(self._order_retry_state.get(str(order_id), {}))

    def list_admin_orders(
        self, category: str = "review", query: str = "", page: int = 0, page_size: int = 20
    ) -> Dict[str, Any]:
        """Return immutable, newest-first order rows for the administrator UI."""
        category = category if category in {"review", "active", "completed", "closed", "all"} else "review"
        query = str(query or "").strip()
        rows = []
        for order_id, order in self.pending_orders.items():
            if not isinstance(order, dict):
                continue
            classification = self.classify_admin_order(order)
            if category != "all" and classification != category:
                continue
            if query and query not in {
                str(order_id), str(order.get("unique_id", "")), str(order.get("user_id", ""))
            }:
                continue
            rows.append({
                "order_id": str(order_id),
                "classification": classification,
                "created_time": order.get("created_time", 0),
                "status": order.get("status", "unknown"),
                "processed": bool(order.get("processed")),
                "needs_manual_review": bool(order.get("needs_manual_review")),
                "user_id": order.get("user_id"),
                "type": order.get("type"),
                "amount": order.get("amount"),
                "coin": order.get("coin"),
                "legacy_read_only": order.get("legacy_origin") == "vip_purchase",
            })
        rows.sort(
            key=lambda item: float(item["created_time"])
            if isinstance(item.get("created_time"), (int, float)) else 0,
            reverse=True,
        )
        page_size = max(1, min(int(page_size), 50))
        max_page = max(0, (len(rows) - 1) // page_size) if rows else 0
        page = max(0, min(int(page), max_page))
        start = page * page_size
        return {
            "items": copy.deepcopy(rows[start:start + page_size]),
            "page": page,
            "max_page": max_page,
            "total": len(rows),
        }

    def get_admin_report(self, days: int = 1, now: float | None = None) -> Dict[str, Any]:
        """Return subscription revenue and first-time paying users for local calendar days."""
        days = int(days)
        if days not in {1, 7, 30}:
            raise ValueError("days must be one of 1, 7, or 30")
        current = datetime.fromtimestamp(time.time() if now is None else float(now))
        start = current.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        end = current
        start_ts = start.timestamp()
        end_ts = end.timestamp()
        amounts: Dict[str, Decimal] = {}
        first_paid_by_user: Dict[int, float] = {}

        for order in self.pending_orders.values():
            if not isinstance(order, dict) or order.get("type") != "subscription_purchase":
                continue
            if order.get("legacy_origin") == "vip_purchase":
                continue
            if not order.get("processed") or order.get("status") != "paid":
                continue
            fulfilled_time = order.get("fulfilled_time")
            if not isinstance(fulfilled_time, (int, float)):
                continue
            try:
                user_id = int(order.get("user_id"))
            except (TypeError, ValueError):
                continue
            fulfilled_time = float(fulfilled_time)
            previous = first_paid_by_user.get(user_id)
            if previous is None or fulfilled_time < previous:
                first_paid_by_user[user_id] = fulfilled_time
            if start_ts <= fulfilled_time <= end_ts:
                try:
                    amount = Decimal(str(order.get("amount")))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                coin = str(order.get("coin") or "UNKNOWN").upper()
                amounts[coin] = amounts.get(coin, Decimal("0")) + amount

        return {
            "days": days,
            "start_time": start_ts,
            "end_time": end_ts,
            "amounts": {
                coin: format(amount.normalize(), "f")
                for coin, amount in sorted(amounts.items())
            },
            "new_paid_users": sum(
                1 for paid_at in first_paid_by_user.values()
                if start_ts <= paid_at <= end_ts
            ),
        }

    def get_user_order_summaries(self, user_id: int, limit: int = 5) -> Dict[str, Any]:
        rows = []
        for order_id, order in self.pending_orders.items():
            if not isinstance(order, dict):
                continue
            try:
                matches = int(order.get("user_id")) == int(user_id)
            except (TypeError, ValueError):
                matches = False
            if matches:
                rows.append({
                    "order_id": str(order_id),
                    "status": order.get("status", "unknown"),
                    "processed": bool(order.get("processed")),
                    "type": order.get("type"),
                    "legacy_read_only": order.get("legacy_origin") == "vip_purchase",
                    "amount": order.get("amount"),
                    "coin": order.get("coin"),
                    "created_time": order.get("created_time", 0),
                })
        rows.sort(
            key=lambda item: float(item["created_time"])
            if isinstance(item.get("created_time"), (int, float)) else 0,
            reverse=True,
        )
        return {"total": len(rows), "items": copy.deepcopy(rows[:max(0, int(limit))])}

    def set_bot(self, bot):
        self.bot = bot

    def bind_order_message(self, order_id: str, chat_id: int, message_id: int) -> bool:
        """Persist the Telegram message that should be removed when an order expires."""
        order = self.pending_orders.get(str(order_id))
        if not order:
            return False
        order['order_message_chat_id'] = int(chat_id)
        order['order_message_id'] = int(message_id)
        return self._save_orders()

    async def _delete_expired_order_message(self, order_id: str) -> None:
        order = self.pending_orders.get(order_id) or {}
        if not self.bot or order.get('order_message_deleted'):
            return
        chat_id = order.get('order_message_chat_id')
        message_id = order.get('order_message_id')
        if chat_id is None or message_id is None:
            return
        try:
            await self.bot.delete_messages(int(chat_id), [int(message_id)])
            order['order_message_deleted'] = True
            order['order_message_deleted_time'] = time.time()
            self._save_orders()
        except Exception as exc:
            logger.warning(
                "删除过期支付消息失败: order_id=%s, error=%s",
                order_id, type(exc).__name__,
            )

    async def _expire_order_if_due(self, order_id: str, now: float | None = None) -> bool:
        """Expire an unpaid local order after one last provider status check."""
        now = time.time() if now is None else float(now)
        async with self._get_order_lock(order_id):
            order = self.pending_orders.get(order_id)
            if not order or order.get('processed'):
                return False
            if order.get('status') in {'paid', 'cancelled', 'expired'}:
                return False
            created_time = order.get('created_time')
            if not isinstance(created_time, (int, float)):
                return False
            if now - float(created_time) < self.order_expiry_window:
                return False

            result = await self._check_order_status_unlocked(order_id)
            self._update_order_retry(order_id, result, now)
            # Expiration is allowed only after OkayPay explicitly confirms that
            # the order is still pending. A paid order must never be overwritten
            # when fulfillment fails, and an unavailable provider is not proof
            # that the order is unpaid.
            if (
                order.get('status') == 'paid'
                or order.get('needs_manual_review')
                or not result.get('success')
                or result.get('status') != 'pending'
            ):
                return False

            previous_order = dict(order)
            order.update({
                'status': 'expired',
                'expired_time': now,
                'auto_check_stopped': True,
                'auto_check_stop_reason': 'local_order_expired',
            })
            if not self._save_orders():
                order.clear()
                order.update(previous_order)
                return False

        self._rebuild_active_order_ids()
        await self._delete_expired_order_message(order_id)
        logger.info('支付订单已在本地过期: %s', order_id)
        return True

    async def _expire_stale_subscription_orders_for_user(self, user_id: int) -> None:
        now = time.time()
        stale_ids = [
            order_id
            for order_id, order in self.pending_orders.items()
            if order.get('type') == 'subscription_purchase'
            and int(order.get('user_id', 0) or 0) == int(user_id)
            and not order.get('processed')
            and order.get('status') not in {'paid', 'cancelled', 'expired'}
            and isinstance(order.get('created_time'), (int, float))
            and now - float(order['created_time']) >= self.order_expiry_window
        ]
        for order_id in stale_ids:
            await self._expire_order_if_due(order_id, now)

    async def _notify_admins(self, message, notified_field: str, order_id: str) -> None:
        """Best-effort, per-admin notification with persistent deduplication."""
        if not self.bot:
            return
        order = self.pending_orders.get(order_id)
        if not order:
            return
        notified = {
            int(admin_id)
            for admin_id in order.get(notified_field, [])
            if str(admin_id).lstrip('-').isdigit()
        }
        changed = False
        for admin_id in getattr(config, 'ADMIN_IDS', []):
            admin_id = int(admin_id)
            if admin_id in notified:
                continue
            try:
                language = DataManager.get_user_language(admin_id)
                localized_message = (
                    message(admin_id, language) if callable(message) else message
                )
                await self.bot.send_message(admin_id, localized_message)
                account_runtime.mark_notify_bot_healthy()
                notified.add(admin_id)
                changed = True
            except account_runtime.NotifyBotFatalError:
                raise
            except account_runtime.NOTIFY_BOT_FATAL_ERRORS as exc:
                account_runtime.raise_notify_bot_fatal(
                    exc, "支付通知管理员时发现主 Bot 授权失效"
                )
            except Exception as exc:
                if isinstance(exc, (RPCError, ConnectionError, TimeoutError, OSError)):
                    account_runtime.mark_notify_bot_degraded(exc)
                logger.warning(
                    "通知管理员失败: order_id=%s, admin_id=%s, error=%s",
                    order_id, admin_id, type(exc).__name__,
                )
        if changed:
            order[notified_field] = sorted(notified)
            self._save_orders()

    async def _notify_admin_new_subscription(self, order_id: str) -> None:
        order = self.pending_orders.get(order_id) or {}
        if order.get('type') != 'subscription_purchase' or order.get('change_type') != 'new':
            return
        is_custom_plus = self._is_custom_plus(order)

        def message(_admin_id, language):
            raw_plan_id = str(order.get("plan_id") or "-").lower()
            try:
                plan_name = t(language, f"admin.notification.plan_name.{raw_plan_id}")
            except KeyError:
                plan_name = raw_plan_id.upper()
            plan_title = t(
                language, "admin.notification.plan",
                badge=DataManager.get_subscription_badge(raw_plan_id),
                plan=plan_name,
                custom=t(language, "admin.notification.custom_plan_suffix")
                if is_custom_plus else "",
            )
            seat_line = (
                t(language, "admin.notification.custom_seats", quota=order.get("quota"))
                if is_custom_plus else ""
            )
            return t(
                language, "admin.notification.new_subscription",
                user_id=order.get("user_id"), plan=plan_title, seat_line=seat_line,
                days=order.get("period_days", 30), amount=order.get("amount"),
                coin=order.get("coin", "USDT"), order_id=order_id,
            )

        await self._notify_admins(
            message,
            'admin_new_subscription_notified_to', order_id,
        )

    async def _notify_admin_order_exception(self, order_id: str, reason: str) -> None:
        order = self.pending_orders.get(order_id) or {}

        def message(_admin_id, language):
            try:
                reason_text = t(language, f"admin.reason.{reason}")
            except KeyError:
                reason_text = str(reason)
            unknown = t(language, "admin.common.unknown")
            return t(
                language, "admin.notification.order_exception", reason=reason_text,
                user_id=order.get("user_id", unknown),
                order_type=order.get("type", unknown), amount=order.get("amount", unknown),
                coin=order.get("coin", "USDT"), status=order.get("status", unknown),
                order_id=order_id,
            )

        await self._notify_admins(
            message,
            f'admin_exception_{reason}_notified_to', order_id,
        )

    @staticmethod
    def _stringify_signature_value(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, Decimal):
            return format(value, "f")
        return str(value)

    @classmethod
    def _flatten_signature_values(
        cls, payload: Mapping[str, Any], prefix: str = ""
    ) -> Dict[str, str]:
        flattened: Dict[str, str] = {}
        for key, value in payload.items():
            key = str(key)
            if not prefix and key == "sign":
                continue
            path = f"{prefix}.{key}" if prefix else key
            if value is None or value == "":
                continue
            if isinstance(value, Mapping):
                flattened.update(cls._flatten_signature_values(value, path))
            else:
                flattened[path] = cls._stringify_signature_value(value)
        return flattened

    @classmethod
    def build_base(cls, payload: Mapping[str, Any]) -> str:
        flattened = cls._flatten_signature_values(payload)
        ordered = sorted(flattened.items(), key=lambda item: item[0].encode("utf-8"))
        return "&".join(f"{key}={value}" for key, value in ordered)

    @classmethod
    def calculate_signature(cls, payload: Mapping[str, Any], token: str) -> str:
        base = cls.build_base(payload)
        return hmac.new(token.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest().upper()

    @classmethod
    def verify_signature(cls, payload: Mapping[str, Any], token: str) -> bool:
        received = payload.get("sign")
        if not isinstance(received, str) or len(received) != 64:
            return False
        expected = cls.calculate_signature(payload, token)
        return hmac.compare_digest(received.upper(), expected)

    def sign(self, data: Mapping[str, Any]) -> Dict[str, Any]:
        """Return a newly signed request without mutating the caller's mapping."""
        signed = {key: value for key, value in data.items() if value is not None and value != ""}
        signed.update({
            "id": self.merchant_id,
            "timestamp": int(time.time()),
            "nonce": secrets.token_hex(16),
        })
        signed["sign"] = self.calculate_signature(signed, self.token)
        return signed

    def verify(self, payload: Mapping[str, Any]) -> bool:
        return self.verify_signature(payload, self.token)

    @staticmethod
    def _normalize_amount(value: Any) -> str:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("金额格式不合法") from exc
        if not amount.is_finite() or amount <= 0:
            raise ValueError("金额必须大于0")
        normalized = format(amount, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return normalized or "0"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout)
            )
        return self._session

    @staticmethod
    def _safe_response_summary(response_text: str, limit: int = 160) -> str:
        summary = " ".join(str(response_text).split())[:limit]
        return re.sub(
            r'(?i)("?(?:sign|token|nonce|api_hash|authorization)"?\s*[:=]\s*)[^\s,&}\"]+',
            r'\1<redacted>',
            summary,
        )

    async def _reserve_provider_request(self) -> tuple[bool, object | None]:
        async with self._provider_state_lock:
            now = time.monotonic()
            if self._provider_open_until <= 0:
                return True, None
            if now < self._provider_open_until:
                return False, None
            if self._provider_half_open_token is not None:
                return False, None
            probe_token = object()
            self._provider_half_open_token = probe_token
            return True, probe_token

    async def _release_provider_probe(self, probe_token: object | None) -> None:
        if probe_token is None:
            return
        async with self._provider_state_lock:
            if self._provider_half_open_token is probe_token:
                self._provider_half_open_token = None

    async def _record_provider_result(
        self, result: Mapping[str, Any], probe_token: object | None = None
    ) -> None:
        retryable = bool(result.get("retryable"))
        error_kind = result.get("error_kind")
        async with self._provider_state_lock:
            now = time.monotonic()
            is_current_probe = (
                probe_token is not None
                and self._provider_half_open_token is probe_token
            )
            if probe_token is not None and not is_current_probe:
                return
            if is_current_probe:
                self._provider_half_open_token = None

            if retryable:
                if self._provider_open_until > 0 and not is_current_probe:
                    return
                self._provider_failure_count += 1
                if (
                    is_current_probe
                    or self._provider_failure_count >= self.provider_failure_threshold
                ):
                    self._provider_failure_count = 0
                    self._provider_open_until = now + self.provider_cooldown
                    logger.warning(
                        "OkayPay 熔断已开启: error_kind=%s, cooldown_seconds=%s",
                        error_kind,
                        self.provider_cooldown,
                    )
                return

            # A late response from a request admitted before the circuit opened
            # must not close it. Only a half-open probe can recover an open circuit.
            if self._provider_open_until > 0 and not is_current_probe:
                return
            recovered = self._provider_open_until > 0
            self._provider_failure_count = 0
            self._provider_open_until = 0.0
            if recovered:
                logger.info("OkayPay 熔断已恢复")

    def _parse_api_response(
        self,
        endpoint: str,
        response_text: str,
        http_status: int = 200,
        content_type: str = "",
    ) -> Dict[str, Any]:
        response_meta = {
            "http_status": http_status,
            "content_type": content_type,
            "response_length": len(response_text),
        }
        if http_status == 429:
            logger.warning(
                "OkayPay %s 请求受限: HTTP 429, content_type=%s, response_length=%s, summary=%s",
                endpoint, content_type, len(response_text),
                self._safe_response_summary(response_text),
            )
            return {
                "success": False,
                "error": "支付API请求过于频繁，请稍后重试",
                "error_kind": "rate_limited",
                "retryable": True,
                **response_meta,
            }
        if http_status >= 500:
            logger.warning(
                "OkayPay %s 服务异常: HTTP %s, content_type=%s, response_length=%s, summary=%s",
                endpoint, http_status, content_type, len(response_text),
                self._safe_response_summary(response_text),
            )
            return {
                "success": False,
                "error": "支付服务暂时不可用，请稍后重试",
                "error_kind": "http_5xx",
                "retryable": True,
                **response_meta,
            }
        try:
            payload = json.loads(response_text, parse_float=Decimal)
        except json.JSONDecodeError:
            logger.warning(
                "OkayPay %s 返回非JSON响应: HTTP %s, content_type=%s, response_length=%s, summary=%s",
                endpoint, http_status, content_type, len(response_text),
                self._safe_response_summary(response_text),
            )
            return {
                "success": False,
                "error": "支付API返回格式错误",
                "error_kind": "invalid_response",
                "retryable": True,
                **response_meta,
            }
        if not isinstance(payload, dict):
            return {
                "success": False,
                "error": "支付API返回格式错误",
                "error_kind": "invalid_response",
                "retryable": True,
                **response_meta,
            }

        code = payload.get("code")
        is_success = (
            http_status == 200
            and payload.get("status") == "success"
            and str(code) == "200"
        )
        if not is_success:
            return {
                "success": False,
                "error": payload.get("msg", "支付API请求失败"),
                "response": payload,
                "error_kind": "business_error" if http_status == 200 else "invalid_response",
                "retryable": False,
                "fallback_allowed": http_status == 200,
                **response_meta,
            }
        if str(payload.get("id", "")) != str(self.merchant_id):
            logger.error("OkayPay %s 响应商户ID不匹配", endpoint)
            return {
                "success": False,
                "error": "支付API响应商户不匹配",
                "error_kind": "security_error",
                "retryable": False,
                **response_meta,
            }
        if not self.verify(payload):
            logger.error("OkayPay %s 成功响应验签失败", endpoint)
            return {
                "success": False,
                "error": "支付API响应验签失败",
                "error_kind": "security_error",
                "retryable": False,
                **response_meta,
            }
        return {
            "success": True,
            "data": payload.get("data", {}),
            "response": payload,
            **response_meta,
        }

    async def _signed_request(self, endpoint: str, data: Mapping[str, Any]) -> Dict[str, Any]:
        signed_data = self.sign(data)
        async with self._request_semaphore:
            allowed, probe_token = await self._reserve_provider_request()
            if not allowed:
                retry_after = max(0.0, self._provider_open_until - time.monotonic())
                return {
                    "success": False,
                    "error": "支付服务暂时不可用，请稍后重试",
                    "error_kind": "circuit_open",
                    "retryable": True,
                    "retry_after": retry_after,
                }
            try:
                try:
                    session = await self._get_session()
                    async with session.post(f"{self.api_url}{endpoint}", data=signed_data) as response:
                        response_text = await response.text()
                        http_status = response.status
                        content_type = response.headers.get("Content-Type", "")
                    result = self._parse_api_response(
                        endpoint, response_text, http_status, content_type
                    )
                except asyncio.TimeoutError:
                    result = {
                        "success": False,
                        "error": "支付请求超时，请稍后重试",
                        "error_kind": "timeout",
                        "retryable": True,
                    }
                except aiohttp.ClientError as exc:
                    logger.warning("OkayPay %s 网络请求失败: %s", endpoint, type(exc).__name__)
                    result = {
                        "success": False,
                        "error": "支付网络请求失败，请稍后重试",
                        "error_kind": "network_error",
                        "retryable": True,
                    }
                except Exception as exc:
                    logger.exception("OkayPay %s 请求异常", endpoint)
                    result = {
                        "success": False,
                        "error": f"支付系统异常: {str(exc)}",
                        "error_kind": "internal_error",
                        "retryable": False,
                    }
                await self._record_provider_result(result, probe_token)
                return result
            except asyncio.CancelledError:
                await self._release_provider_probe(probe_token)
                raise

    async def create_payment_link(
        self,
        unique_id: str,
        amount: Any,
        coin: str = "USDT",
        name: str | None = None,
        return_url: str | None = None,
        _order_metadata: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if not unique_id or not name or not return_url:
            return {"success": False, "error": "unique_id、name、return_url 均为必填参数"}
        try:
            amount_text = self._normalize_amount(amount)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        coin = str(coin).upper()
        result = await self._signed_request("payLink", {
            "unique_id": unique_id,
            "name": name,
            "amount": amount_text,
            "return_url": return_url,
            "coin": coin,
        })
        if not result["success"]:
            return result

        order_data = result.get("data")
        if not isinstance(order_data, dict):
            return {"success": False, "error": "支付API返回数据不完整"}
        order_id = order_data.get("order_id")
        pay_url = order_data.get("pay_url")
        if not order_id or not pay_url or str(order_data.get("status")) != "0":
            return {"success": False, "error": "支付API返回数据不完整"}

        local_order = {
            "unique_id": unique_id,
            "amount": amount_text,
            "coin": coin,
            "name": name,
            "return_url": return_url,
            "created_time": time.time(),
            "status": "pending",
            "processed": False,
            "auto_check_stopped": False,
        }
        if _order_metadata:
            local_order.update(dict(_order_metadata))
        self.pending_orders[str(order_id)] = local_order
        if not self._save_orders():
            await self._notify_admin_order_exception(str(order_id), 'local_order_save_failed')
            return {"success": False, "error": "支付订单保存失败，请联系管理员"}
        logger.info("支付链接创建成功: %s, unique_id: %s", order_id, unique_id)
        return {"success": True, "order_id": str(order_id), "pay_url": str(pay_url)}

    async def query_order_by_id(self, order_id: str) -> Dict[str, Any]:
        result = await self._signed_request("checkTransferByTxid", {"txid": order_id})
        if not result["success"]:
            return result
        data = result.get("data")
        if not isinstance(data, dict):
            return {"success": False, "error": "查单返回数据不完整"}
        status = data.get("status")
        if str(status) not in {"0", "1"}:
            return {"success": False, "error": "查单返回未知状态"}
        return {
            **data,
            "success": True,
            "status": "paid" if str(status) == "1" else "pending",
        }

    async def query_order_by_unique_id(self, unique_id: str) -> Dict[str, Any]:
        result = await self._signed_request("checkDeposit", {"unique_id": unique_id})
        if not result["success"]:
            return result
        data = result.get("data")
        if not isinstance(data, dict):
            return {"success": False, "error": "查单返回数据不完整"}
        status = data.get("status")
        if str(status) not in {"0", "1"}:
            return {"success": False, "error": "查单返回未知状态"}
        return {
            **data,
            "success": True,
            "status": "paid" if str(status) == "1" else "pending",
        }

    def _validate_paid_order(self, order_id: str, remote: Mapping[str, Any]) -> tuple[bool, str]:
        local = self.pending_orders[order_id]
        checks = {
            "order_id": (str(remote.get("order_id", "")), str(order_id)),
            "unique_id": (str(remote.get("unique_id", "")), str(local.get("unique_id", ""))),
            "coin": (str(remote.get("coin", "")).upper(), str(local.get("coin", "")).upper()),
        }
        for field, (actual, expected) in checks.items():
            if not actual or actual != expected:
                return False, f"支付订单{field}校验失败"
        try:
            actual_amount = self._normalize_amount(remote.get("amount"))
            expected_amount = self._normalize_amount(local.get("amount"))
        except ValueError:
            return False, "支付订单金额校验失败"
        if actual_amount != expected_amount:
            return False, "支付订单金额不一致"
        return True, ""

    async def check_order_status(self, order_id: str) -> Dict[str, Any]:
        async with self._get_order_lock(order_id):
            result = await self._check_order_status_unlocked(order_id)
        self._rebuild_active_order_ids()
        if not result.get("success") and result.get("error"):
            order = self.pending_orders.get(order_id, {})
            user_id = order.get("user_id")
            language = (
                DataManager.get_user_language(int(user_id))
                if user_id is not None else "zh"
            )
            result["error"] = localized_payment_error(
                language, result.get("error")
            )
        return result

    async def _check_order_status_unlocked(self, order_id: str) -> Dict[str, Any]:
        if order_id not in self.pending_orders:
            return {"success": False, "error": "订单不存在"}
        order = self.pending_orders[order_id]
        if order.get("legacy_origin") == "vip_purchase":
            return {"success": False, "error": "遗留订阅订单仅供查看"}
        if order.get("processed"):
            return {"success": True, "status": "paid", "already_processed": True}
        if order.get("status") == "expired":
            return {"success": False, "status": "expired", "error": "订单已过期"}
        if order.get("status") == "cancelled":
            return {"success": False, "status": "cancelled", "error": "订单已取消"}

        remote = await self.query_order_by_id(order_id)
        if not remote.get("success") and remote.get("fallback_allowed"):
            unique_id = order.get("unique_id")
            if not unique_id:
                return {"success": False, "error": "旧订单缺少unique_id，只能由管理员处理"}
            remote = await self.query_order_by_unique_id(str(unique_id))
        if not remote.get("success"):
            return remote
        if remote.get("status") != "paid":
            return {"success": True, "status": "pending"}

        valid, error = self._validate_paid_order(order_id, remote)
        if not valid:
            logger.error("拒绝处理订单 %s: %s", order_id, error)
            order.update({
                'payment_validation_error': error,
                'needs_manual_review': True,
                'manual_review_reason': 'paid_order_validation_failed',
                'auto_check_stopped': True,
                'auto_check_stop_reason': 'paid_order_validation_failed',
            })
            await self._notify_admin_order_exception(order_id, 'paid_order_validation_failed')
            return {"success": False, "error": error}

        order.update({
            "status": "paid",
            "paid_amount": self._normalize_amount(remote.get("amount")),
            "pay_user_id": remote.get("pay_user_id"),
            "verified_time": time.time(),
        })
        fulfilled = await self._process_paid_order_unlocked(order_id)
        if not fulfilled:
            return {"success": False, "error": "支付已确认，但订阅发放失败，请联系管理员"}
        return {
            "success": True,
            "status": "paid",
            "amount": order.get("paid_amount"),
            "pay_user_id": order.get("pay_user_id"),
        }

    async def process_paid_order(self, order_id: str) -> bool:
        async with self._get_order_lock(order_id):
            result = await self._process_paid_order_unlocked(order_id)
        self._rebuild_active_order_ids()
        return result

    async def _process_paid_order_unlocked(self, order_id: str) -> bool:
        order = self.pending_orders.get(order_id)
        if not order or order.get("processed") or order.get("status") != "paid":
            return False
        if order.get("type") == "subscription_purchase":
            if not DataManager.fulfill_subscription_payment(order_id, self.pending_orders):
                reason = order.get('manual_review_reason') or 'fulfillment_failed'
                await self._notify_admin_order_exception(order_id, reason)
                return False
            self.processed_orders.add(order_id)
            await self._notify_fulfilled_order(order_id)
            await self._notify_admin_new_subscription(order_id)
            return True
        if order.get('type') == 'payment_test':
            order.update({
                'processed': True,
                'status': 'paid',
                'fulfilled_time': time.time(),
            })
            if not self._save_orders():
                await self._notify_admin_order_exception(order_id, 'fulfillment_failed')
                return False
            self.processed_orders.add(order_id)
            return True
        await self._notify_admin_order_exception(order_id, 'fulfillment_failed')
        return False

    async def _notify_fulfilled_order(self, order_id: str) -> None:
        order = self.pending_orders[order_id]
        if (
            not self.bot
            or order.get("success_notified")
            or order.get("type") != "subscription_purchase"
        ):
            return
        try:
            user_id = int(order["user_id"])
            language = DataManager.get_user_language(user_id)
            quota = order.get('quota')
            quota_text = t(language, "plans.quota_unlimited") if quota is None else str(quota)
            plan = str(order.get('plan_id', '')).upper()
            if order.get('billing_mode') == 'prorated_upgrade':
                snapshot = order.get('upgrade_snapshot') or {}
                detail = t(
                    language,
                    "payment.fulfilled_upgrade",
                    plan=plan,
                    quota=quota_text,
                    amount=order.get('amount'),
                    expiry=snapshot.get('target_expires_at', ''),
                )
            else:
                period_days = int(order.get('period_days', 30))
                discount = ""
                if period_days > 30 and Decimal(str(
                    order.get('actual_discount_percent', '0')
                )) > 0:
                    discount = t(
                        language,
                        "payment.fulfilled_discount",
                        percent=_display_percent(order.get('actual_discount_percent', '0')),
                    )
                detail = t(
                    language,
                    "payment.fulfilled_standard",
                    plan=plan,
                    quota=quota_text,
                    days=period_days,
                    discount=discount,
                )
            subscription = DataManager.get_subscription(int(order['user_id'])) or {}
            if subscription.get('selection_required'):
                detail += t(language, "payment.fulfilled_selection")
            await self.bot.send_message(
                user_id,
                t(language, "payment.confirmed", detail=detail,
                  amount=order['amount'], coin=order['coin'], order_id=order_id),
            )
            account_runtime.mark_notify_bot_healthy()
            order["success_notified"] = True
            order["success_notified_time"] = time.time()
            self._save_orders()
        except account_runtime.NotifyBotFatalError:
            raise
        except account_runtime.NOTIFY_BOT_FATAL_ERRORS as exc:
            account_runtime.raise_notify_bot_fatal(
                exc, "发送支付成功通知时发现主 Bot 授权失效"
            )
        except Exception as exc:
            if isinstance(exc, (RPCError, ConnectionError, TimeoutError, OSError)):
                account_runtime.mark_notify_bot_degraded(exc)
            logger.warning("支付成功但通知用户失败: 订单 %s: %s", order_id, type(exc).__name__)

    @staticmethod
    def _is_custom_plus(quote: Mapping[str, Any]) -> bool:
        plan_id = str(quote.get('plan_id', '')).lower()
        if plan_id != 'plus':
            return False
        if int(quote.get('addon') or 0) > 0:
            return True
        quota = quote.get('quota')
        base_quota = DataManager.get_subscription_catalog().get('plus', {}).get('quota', 10)
        try:
            return quota is not None and int(quota) > int(base_quota)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _subscription_plan_title(
        quote: Mapping[str, Any], language: str = "zh"
    ) -> str:
        plan_id = str(quote.get('plan_id', '')).lower()
        key = f"payment.plan.{plan_id}"
        title = (
            t(language, key)
            if key in {"payment.plan.go", "payment.plan.plus", "payment.plan.pro"}
            else str(quote.get('plan_name', plan_id)).upper()
        )
        if PaymentSystem._is_custom_plus(quote):
            title += t(language, "payment.plan.custom")
        badge = DataManager.get_subscription_badge(plan_id)
        return f'{badge} {title}'

    @staticmethod
    def _subscription_payment_name(
        quote: Mapping[str, Any], upgrade: bool = False, language: str = "zh"
    ) -> str:
        """生成展示在支付渠道中的尊享订阅名称。"""
        title = PaymentSystem._subscription_plan_title(quote, language)
        is_custom_plus = PaymentSystem._is_custom_plus(quote)
        quota = quote.get('quota')
        seat_suffix = (
            t(language, "payment.provider.seats", quota=quota)
            if is_custom_plus else ""
        )
        if upgrade:
            upgrade_target = (
                t(language, "payment.provider.target_seats", quota=quota)
                if is_custom_plus else ""
            )
            return t(
                language,
                "payment.provider.upgrade",
                title=title,
                target=upgrade_target,
            )
        return t(
            language,
            "payment.provider.subscription",
            title=title,
            days=int(quote.get('period_days', 30)),
            seats=seat_suffix,
        )

    async def create_subscription_payment(
        self, user_id: int, plan_id: str, quota: int | None = None,
        unique_id: str | None = None, period_days: int = 30,
    ) -> Dict[str, Any]:
        async with self._get_subscription_user_lock(user_id):
            if DataManager.is_admin(user_id):
                return {
                    'success': False,
                    'error': t(DataManager.get_user_language(user_id), "payment.error.admin"),
                }
            await self._expire_stale_subscription_orders_for_user(user_id)
            if self._has_open_subscription_order(user_id):
                return {
                    'success': False,
                    'error': t(DataManager.get_user_language(user_id), "payment.error.open_order"),
                }
            try:
                base_quote = DataManager.quote_subscription(plan_id, quota, 30)
            except ValueError as error:
                return {
                    'success': False,
                    'error': localized_payment_error(
                        DataManager.get_user_language(user_id), error
                    ),
                }
            change = DataManager.classify_subscription_change(
                user_id, base_quote['plan_id'], base_quote['quota']
            )
            if change == 'conflict':
                return {
                    'success': False,
                    'error': t(DataManager.get_user_language(user_id), "payment.error.scheduled_conflict"),
                }
            try:
                quote = (
                    base_quote if change == 'upgrade'
                    else DataManager.quote_subscription(plan_id, quota, period_days)
                )
            except ValueError as error:
                return {
                    'success': False,
                    'error': localized_payment_error(
                        DataManager.get_user_language(user_id), error
                    ),
                }
            upgrade_snapshot = None
            amount = quote['price']
            billing_mode = 'full_period'
            if change == 'upgrade':
                try:
                    upgrade_snapshot = DataManager.quote_subscription_upgrade(
                        user_id, quote['plan_id'], quote['quota']
                    )
                except ValueError as error:
                    return {
                        'success': False,
                        'error': localized_payment_error(
                            DataManager.get_user_language(user_id), error
                        ),
                    }
                amount = upgrade_snapshot['amount']
                billing_mode = 'prorated_upgrade'
            if unique_id is None:
                unique_id = (
                    f"sub-{user_id}-{quote['plan_id']}-{time.time_ns() // 1_000_000}-"
                    f"{secrets.token_hex(4)}"
                )
            language = DataManager.get_user_language(user_id)
            name = self._subscription_payment_name(
                quote,
                upgrade=upgrade_snapshot is not None,
                language=language,
            )
            metadata = {
                'user_id': int(user_id), 'type': 'subscription_purchase',
                'plan_id': quote['plan_id'], 'quota': quote['quota'],
                'addon': quote['addon'], 'period_days': 30,
                'pricing_days': quote.get('pricing_days', quote.get('period_days', 30)),
                'change_type': change, 'billing_mode': billing_mode,
                'catalog_price': quote.get('monthly_catalog_price', quote['price']),
                'list_price': quote.get('list_price', quote['price']),
                'configured_discount_percent': quote.get('configured_discount_percent', '0'),
                'actual_discount_percent': quote.get('actual_discount_percent', '0'),
                'discount_amount': quote.get('discount_amount', '0'),
                'effective_monthly_price': quote.get('effective_monthly_price', quote['price']),
            }
            metadata['period_days'] = 30 if upgrade_snapshot else quote['period_days']
            if upgrade_snapshot:
                metadata['upgrade_snapshot'] = upgrade_snapshot
            result = await self.create_payment_link(
                unique_id=unique_id,
                amount=amount,
                coin='USDT',
                name=name,
                return_url=self.return_url,
                _order_metadata=metadata,
            )
            if not result.get("success"):
                result["error"] = localized_payment_error(
                    language, result.get("error")
                )
            return result

    async def cancel_order(self, order_id: str, user_id: int) -> Dict[str, Any]:
        try:
            return await self._cancel_order(order_id, user_id)
        finally:
            self._rebuild_active_order_ids()

    async def _cancel_order(self, order_id: str, user_id: int) -> Dict[str, Any]:
        """Cancel a locally pending order after one final remote status check."""
        language = DataManager.get_user_language(user_id)
        async with self._get_order_lock(order_id):
            order = self.pending_orders.get(order_id)
            if not order or order.get('user_id') != int(user_id):
                return {'success': False, 'error': t(language, "payment.error.order_owner")}
            if order.get('processed') or order.get('status') == 'paid':
                return {'success': False, 'status': 'paid', 'error': t(language, "payment.error.order_paid")}
            if order.get('status') == 'cancelled':
                return {'success': True, 'status': 'cancelled', 'already_cancelled': True}

            status = await self._check_order_status_unlocked(order_id)
            if status.get('success') and status.get('status') == 'paid':
                return {'success': False, 'status': 'paid', 'error': t(language, "payment.error.order_active")}
            if not status.get('success'):
                return {
                    'success': False,
                    'error': t(
                        language,
                        "payment.error.cancel_check",
                        error=localized_payment_error(language, status.get('error', 'unknown')),
                    ),
                }

            previous_order = dict(order)
            order.update({
                'status': 'cancelled',
                'cancelled_time': time.time(),
                'cancelled_by_user': True,
                'auto_check_stopped': True,
                'auto_check_stop_reason': 'cancelled_by_user',
            })
            if not self._save_orders():
                order.clear()
                order.update(previous_order)
                return {'success': False, 'error': t(language, "payment.error.cancel_save")}
            logger.info('用户取消支付订单: %s, user_id=%s', order_id, user_id)
            return {'success': True, 'status': 'cancelled'}

    async def find_order_by_unique_id(self, unique_id: str) -> str | None:
        for order_id, order in self.pending_orders.items():
            if order.get("unique_id") == unique_id:
                return order_id
        return None

    async def _monitor_pending_orders_once(self) -> None:
        now = time.time()
        stopped_changed = False
        active_order_ids = []
        manual_review_orders = []
        expiring_order_ids = []
        expired_message_order_ids = []
        for order_id in list(self._active_order_ids):
            order = self.pending_orders.get(order_id)
            if not isinstance(order, dict):
                self._active_order_ids.discard(order_id)
                continue
            if order.get("processed"):
                continue
            if order.get('needs_manual_review'):
                manual_review_orders.append((
                    order_id,
                    order.get('manual_review_reason') or 'fulfillment_failed',
                ))
            if order.get('status') == 'expired':
                if not order.get('order_message_deleted'):
                    expired_message_order_ids.append(order_id)
                continue
            if order.get("status") == "cancelled":
                continue
            created_time = order.get("created_time")
            if not isinstance(created_time, (int, float)):
                if not order.get("auto_check_stopped"):
                    order["auto_check_stopped"] = True
                    order["auto_check_stop_reason"] = "legacy_order_missing_created_time"
                    stopped_changed = True
                continue
            if (
                order.get('status') == 'pending'
                and now - float(created_time) >= self.order_expiry_window
            ):
                if self._order_check_is_due(order_id, now):
                    expiring_order_ids.append(order_id)
                continue
            if now - float(created_time) >= self.auto_check_window:
                if not order.get("auto_check_stopped"):
                    order["auto_check_stopped"] = True
                    order["auto_check_stopped_time"] = now
                    stopped_changed = True
                continue
            if self._order_check_is_due(order_id, now):
                active_order_ids.append(order_id)

        if stopped_changed:
            self._save_orders()
        if manual_review_orders:
            await asyncio.gather(*(
                self._notify_admin_order_exception(order_id, reason)
                for order_id, reason in manual_review_orders
            ))
        if expiring_order_ids:
            await asyncio.gather(*(
                self._expire_order_if_due(order_id, now)
                for order_id in expiring_order_ids
            ))
        if expired_message_order_ids:
            await asyncio.gather(*(
                self._delete_expired_order_message(order_id)
                for order_id in expired_message_order_ids
            ))
        async def check_one(order_id: str) -> None:
            current = self.pending_orders.get(order_id)
            if not current or current.get("processed"):
                return
            if current.get("status") in {"cancelled", "expired", "paid"}:
                return
            result = await self.check_order_status(order_id)
            current = self.pending_orders.get(order_id)
            if not current or current.get("processed"):
                return
            if current.get("status") in {"cancelled", "expired", "paid"}:
                return
            if result.get("status") in {"cancelled", "expired", "paid"}:
                return
            self._update_order_retry(order_id, result, time.time())
            if not result.get("success") and not result.get("retryable"):
                logger.warning("自动查单失败: %s: %s", order_id, result.get("error"))

        if active_order_ids:
            await asyncio.gather(*(check_one(order_id) for order_id in active_order_ids))
        self._rebuild_active_order_ids()

    async def _monitor_pending_orders(self) -> None:
        while True:
            try:
                await self._monitor_pending_orders_once()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except account_runtime.NotifyBotFatalError:
                raise
            except Exception:
                logger.exception("支付订单自动检测异常")
                await asyncio.sleep(self.poll_interval)

    def _monitoring_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error("支付订单监控意外退出: %s", error)

    async def start_monitoring(self) -> asyncio.Task:
        if self.monitoring_task and not self.monitoring_task.done():
            return self.monitoring_task
        self.monitoring_task = asyncio.create_task(
            self._monitor_pending_orders(), name="payment-order-monitor"
        )
        self.monitoring_task.add_done_callback(self._monitoring_done)
        return self.monitoring_task

    async def stop_monitoring(self) -> None:
        if self.monitoring_task:
            self.monitoring_task.cancel()
            await asyncio.gather(self.monitoring_task, return_exceptions=True)
            self.monitoring_task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
