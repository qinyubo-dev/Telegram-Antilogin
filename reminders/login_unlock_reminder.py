# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from accounts import account_runtime
from localization import t
from storage.data_manager import DataManager
import settings as config
from user_timezones import default_timezone, timezone_text


logger = logging.getLogger(__name__)
MAX_REMINDER_MINUTES = 43200


class ReminderScheduleValidationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LoginUnlockScheduleResult:
    status: str
    phone: str
    wait_seconds: int
    unlock_at: datetime
    reminder_times: tuple[datetime, ...] = ()
    used: int = 0
    limit: Optional[int] = 0


def normalize_phone(phone: str) -> str:
    return re.sub(r"[^\d+]", "", str(phone or ""))


def phone_key(phone: str) -> str:
    return "".join(char for char in normalize_phone(phone) if char.isdigit())


def parse_utc(value: object) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def beijing_text(value: datetime) -> str:
    """Backward-compatible helper retained for existing callers and tests."""
    return timezone_text(value, "Asia/Shanghai")


def format_offset(offset_seconds: int, language: str = "zh") -> str:
    seconds = max(1, int(offset_seconds))
    if seconds < 60:
        return t(language, "login_unlock.offset_seconds", value=seconds)
    return t(language, "login_unlock.offset_minutes", value=seconds // 60)


def format_limit(limit: Optional[int], language: str = "zh") -> str:
    return t(language, "login_unlock.unlimited") if limit is None else str(limit)


def parse_schedule_offsets(text: object, count: int) -> List[int]:
    try:
        count = int(count)
    except (TypeError, ValueError) as error:
        raise ReminderScheduleValidationError("count") from error
    if count < 1 or count > 5:
        raise ReminderScheduleValidationError("count")

    tokens = str(text or "").strip().lower().split()
    if len(tokens) != count:
        raise ReminderScheduleValidationError("item_count")

    offsets: List[int] = []
    for index, token in enumerate(tokens):
        match = re.fullmatch(r"(\d+)([ms])", token)
        if not match:
            raise ReminderScheduleValidationError("format")
        value = int(match.group(1))
        unit = match.group(2)
        is_last = index == len(tokens) - 1
        if value <= 0:
            raise ReminderScheduleValidationError("range")
        if unit == "s":
            if not is_last:
                raise ReminderScheduleValidationError("seconds_last")
            if value > 59:
                raise ReminderScheduleValidationError("seconds_range")
            offset = value
        else:
            if value > MAX_REMINDER_MINUTES:
                raise ReminderScheduleValidationError("minutes_range")
            offset = value * 60
        offsets.append(offset)

    if any(left <= right for left, right in zip(offsets, offsets[1:])):
        raise ReminderScheduleValidationError("descending")
    return offsets


class LoginUnlockReminderSystem:
    def __init__(self, bot):
        self.bot = bot
        self.monitoring_task: Optional[asyncio.Task] = None
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._inflight: set[tuple[int, str, str]] = set()

    @staticmethod
    def limit_for_user(user_id: int) -> Optional[int]:
        if DataManager.is_admin(int(user_id)):
            return None
        subscription = DataManager.get_subscription(int(user_id)) or {}
        plan_id = str(subscription.get("plan_id", "")).lower()
        if plan_id == "pro":
            return None
        value = config.LOGIN_UNLOCK_MONITOR_LIMITS.get(plan_id)
        return int(value) if value is not None else 0

    @staticmethod
    def has_access(user_id: int) -> bool:
        return DataManager.is_admin(int(user_id)) or DataManager.has_active_subscription(int(user_id))

    def quota_status(self, user_id: int, phone: str = "") -> Dict:
        records = DataManager.get_login_unlock_reminders(int(user_id))
        key = phone_key(phone)
        limit = self.limit_for_user(user_id)
        used = len(records)
        existing = bool(key and key in records)
        return {
            "used": used,
            "limit": limit,
            "existing": existing,
            "full": not existing and limit is not None and used >= limit,
        }

    @staticmethod
    def _nodes(unlock_at: datetime, now: datetime) -> List[Dict]:
        schedule = DataManager.get_login_unlock_reminder_schedule()
        nodes = []
        for offset in schedule["offsets_seconds"]:
            remind_at = unlock_at - timedelta(seconds=int(offset))
            if remind_at <= now:
                continue
            nodes.append({
                "offset_seconds": int(offset),
                "remind_at": utc_iso(remind_at),
                "retry_at": None,
            })
        return nodes

    async def schedule(
        self,
        user_id: int,
        phone: str,
        wait_seconds: int,
        *,
        now: Optional[datetime] = None,
    ) -> LoginUnlockScheduleResult:
        user_id = int(user_id)
        normalized = normalize_phone(phone)
        key = phone_key(normalized)
        wait_seconds = max(1, int(wait_seconds))
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        unlock_at = now + timedelta(seconds=wait_seconds)

        async with self._lock:
            records = DataManager.get_login_unlock_reminders(user_id)
            limit = self.limit_for_user(user_id)
            removed_expired = False
            for record_key, record in list(records.items()):
                record_unlock = parse_utc(record.get("unlock_at"))
                if record_unlock is None or record_unlock <= now:
                    records.pop(record_key, None)
                    removed_expired = True
            if removed_expired and not DataManager.set_login_unlock_reminders(user_id, records):
                return LoginUnlockScheduleResult(
                    "save_failed", normalized, wait_seconds, unlock_at,
                    used=len(records), limit=limit,
                )
            used = len(records)
            if not self.has_access(user_id):
                return LoginUnlockScheduleResult(
                    "no_access", normalized, wait_seconds, unlock_at, used=used, limit=limit
                )
            if key not in records and limit is not None and used >= limit:
                return LoginUnlockScheduleResult(
                    "full", normalized, wait_seconds, unlock_at, used=used, limit=limit
                )

            nodes = self._nodes(unlock_at, now)
            if not nodes:
                if key in records:
                    records.pop(key, None)
                    if not DataManager.set_login_unlock_reminders(user_id, records):
                        return LoginUnlockScheduleResult(
                            "save_failed", normalized, wait_seconds, unlock_at,
                            used=used, limit=limit,
                        )
                return LoginUnlockScheduleResult(
                    "immediate", normalized, wait_seconds, unlock_at, used=len(records), limit=limit
                )

            records[key] = {
                "phone": normalized,
                "created_at": utc_iso(now),
                "official_wait_seconds": wait_seconds,
                "unlock_at": utc_iso(unlock_at),
                "nodes": nodes,
            }
            if not DataManager.set_login_unlock_reminders(user_id, records):
                return LoginUnlockScheduleResult(
                    "save_failed", normalized, wait_seconds, unlock_at, used=used, limit=limit
                )
            self._changed.set()
            return LoginUnlockScheduleResult(
                "scheduled",
                normalized,
                wait_seconds,
                unlock_at,
                tuple(parse_utc(node["remind_at"]) for node in nodes),
                len(records),
                limit,
            )

    def render_schedule_result(
        self,
        result: LoginUnlockScheduleResult,
        language: str,
        timezone_name: Optional[str] = None,
    ) -> str:
        timezone_name = timezone_name or default_timezone(language)
        unlock = timezone_text(result.unlock_at, timezone_name)
        quota = f"{result.used} / {format_limit(result.limit, language)}"
        if result.status == "scheduled":
            reminder_list = "\n".join(
                t(
                    language,
                    "login_unlock.reminder_time_line",
                    time=timezone_text(value, timezone_name),
                )
                for value in result.reminder_times if value is not None
            )
            return t(
                language,
                "login_unlock.scheduled",
                phone=result.phone,
                seconds=result.wait_seconds,
                unlock=unlock,
                reminders=reminder_list,
                quota=quota,
            )
        if result.status == "immediate":
            return t(
                language,
                "login_unlock.immediate",
                phone=result.phone,
                seconds=result.wait_seconds,
                unlock=unlock,
            )
        if result.status == "full":
            return t(
                language,
                "login_unlock.full",
                phone=result.phone,
                seconds=result.wait_seconds,
                unlock=unlock,
                quota=quota,
            )
        if result.status == "no_access":
            return t(language, "common.no_access")
        return t(language, "login_unlock.save_failed", unlock=unlock)

    async def remove(self, user_id: int, phone: str) -> bool:
        async with self._lock:
            records = DataManager.get_login_unlock_reminders(int(user_id))
            key = phone_key(phone)
            if key not in records:
                return True
            if any(
                claim_user == int(user_id) and claim_key == key
                for claim_user, claim_key, _ in self._inflight
            ):
                return False
            records.pop(key, None)
            saved = DataManager.set_login_unlock_reminders(int(user_id), records)
            if saved:
                self._changed.set()
            return saved

    async def list_records(self, user_id: int) -> List[Dict]:
        await self.reconcile_user(int(user_id))
        records = DataManager.get_login_unlock_reminders(int(user_id))
        return sorted(
            records.values(),
            key=lambda item: item.get("unlock_at", ""),
        )

    async def reconcile_user(self, user_id: int, *, now: Optional[datetime] = None) -> bool:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        async with self._lock:
            records = DataManager.get_login_unlock_reminders(int(user_id))
            if not records:
                return True
            changed = False
            if not self.has_access(user_id):
                records = {}
                changed = True
            else:
                for key, record in list(records.items()):
                    unlock_at = parse_utc(record.get("unlock_at"))
                    if unlock_at is None or unlock_at <= now:
                        records.pop(key, None)
                        changed = True
            if changed:
                saved = DataManager.set_login_unlock_reminders(int(user_id), records)
                if saved:
                    self._changed.set()
                return saved
            return True

    async def recalculate_all(self, *, now: Optional[datetime] = None) -> bool:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        ok = True
        async with self._lock:
            for user_id in DataManager.iter_login_unlock_reminder_users():
                records = DataManager.get_login_unlock_reminders(user_id)
                changed = False
                if not self.has_access(user_id):
                    records = {}
                    changed = True
                else:
                    for key, record in list(records.items()):
                        unlock_at = parse_utc(record.get("unlock_at"))
                        if unlock_at is None or unlock_at <= now:
                            records.pop(key, None)
                            changed = True
                            continue
                        new_nodes = self._nodes(unlock_at, now)
                        if new_nodes != record.get("nodes", []):
                            record["nodes"] = new_nodes
                            changed = True
                        if not new_nodes:
                            records.pop(key, None)
                if changed and not DataManager.set_login_unlock_reminders(user_id, records):
                    ok = False
            self._changed.set()
        return ok

    async def _send(self, user_id: int, record: Dict, node: Dict) -> bool:
        from accounts.account_manager import AccountManager

        language = DataManager.get_user_language(user_id)
        unlock_at = parse_utc(record.get("unlock_at"))
        if unlock_at is None:
            return True
        text = t(
            language,
            "login_unlock.notification",
            phone=record.get("phone", ""),
            remaining=format_offset(int(node["offset_seconds"]), language),
            unlock=timezone_text(unlock_at, DataManager.get_user_timezone(user_id)),
        )
        return bool(await AccountManager._safe_send_bot_message(
            self.bot,
            user_id,
            text,
            context=f"login_unlock:{phone_key(record.get('phone', ''))}",
        ))

    async def _collect_due(self, now: datetime):
        due = []
        next_at: Optional[datetime] = None
        async with self._lock:
            for user_id in DataManager.iter_login_unlock_reminder_users():
                records = DataManager.get_login_unlock_reminders(user_id)
                changed = False
                if not self.has_access(user_id):
                    if records:
                        DataManager.set_login_unlock_reminders(user_id, {})
                    continue
                for key, record in list(records.items()):
                    unlock_at = parse_utc(record.get("unlock_at"))
                    if unlock_at is None or unlock_at <= now:
                        records.pop(key, None)
                        changed = True
                        continue
                    for node in list(record.get("nodes", [])):
                        remind_at = parse_utc(node.get("remind_at"))
                        retry_at = parse_utc(node.get("retry_at")) if node.get("retry_at") else None
                        if remind_at is None:
                            record["nodes"].remove(node)
                            changed = True
                            continue
                        eligible_at = max(value for value in (remind_at, retry_at) if value is not None)
                        claim = (user_id, key, node.get("remind_at", ""))
                        if eligible_at <= now and claim not in self._inflight:
                            self._inflight.add(claim)
                            due.append((user_id, key, record, node, claim))
                        elif eligible_at > now and (next_at is None or eligible_at < next_at):
                            next_at = eligible_at
                if changed:
                    DataManager.set_login_unlock_reminders(user_id, records)
        return due, next_at

    async def _finish_delivery(self, item, success: bool, now: datetime) -> None:
        user_id, key, _record, node, claim = item
        async with self._lock:
            self._inflight.discard(claim)
            records = DataManager.get_login_unlock_reminders(user_id)
            current = records.get(key)
            if not current:
                return
            unlock_at = parse_utc(current.get("unlock_at"))
            match = next(
                (
                    candidate for candidate in current.get("nodes", [])
                    if candidate.get("remind_at") == node.get("remind_at")
                ),
                None,
            )
            if not match:
                return
            if success or unlock_at is None or unlock_at <= now:
                current["nodes"].remove(match)
            else:
                retry_at = min(
                    unlock_at,
                    now + timedelta(seconds=config.LOGIN_UNLOCK_RETRY_SECONDS),
                )
                match["retry_at"] = utc_iso(retry_at)
            if not current.get("nodes"):
                records.pop(key, None)
            if DataManager.set_login_unlock_reminders(user_id, records):
                self._changed.set()
            else:
                logger.error(
                    "登录解限提醒发送结果持久化失败: 用户ID=%s, 手机号键=%s",
                    user_id,
                    key,
                )

    async def _run(self) -> None:
        while True:
            try:
                # Clear before inspecting state so changes arriving during the
                # scan or delivery phase remain visible to the subsequent wait.
                self._changed.clear()
                now = datetime.now(timezone.utc)
                due, next_at = await self._collect_due(now)
                for item in due:
                    success = await self._send(item[0], item[2], item[3])
                    await self._finish_delivery(item, success, datetime.now(timezone.utc))
                now = datetime.now(timezone.utc)
                timeout = 60.0
                if next_at is not None:
                    timeout = max(0.05, min(timeout, (next_at - now).total_seconds()))
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except account_runtime.NotifyBotFatalError:
                raise
            except Exception:
                logger.exception("登录解限提醒调度异常")
                await asyncio.sleep(5)

    async def start_monitoring(self):
        if self.monitoring_task and not self.monitoring_task.done():
            return self.monitoring_task
        self.monitoring_task = asyncio.create_task(
            self._run(), name="login-unlock-reminders"
        )
        return self.monitoring_task

    async def stop_monitoring(self) -> None:
        task = self.monitoring_task
        self.monitoring_task = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
