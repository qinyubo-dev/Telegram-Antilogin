# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import settings as config


logger = logging.getLogger(__name__)

ADMIN_AUDIT_FILE = getattr(
    config, "ADMIN_AUDIT_FILE", os.path.join("storage", "admin_audit.jsonl")
)
ADMIN_AUDIT_RETENTION_DAYS = int(
    getattr(config, "ADMIN_AUDIT_RETENTION_DAYS", 180)
)

_PHONE_KEY_RE = re.compile(r"phone|mobile|selected_accounts|hosted_accounts", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(
    r"token|signature|session|pay_url|return_url|api_hash|password",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_LONG_DIGIT_RE = re.compile(r"(?<!\d)\+?\d[\d\s()\-]{7,}\d(?!\d)")
_EXEMPT_AUDIT_ID = "__first_admin_audit_exempt__"


def _is_audit_exempt_admin(admin_id: Any) -> bool:
    """Return whether the actor is the primary (first configured) admin."""
    admin_ids = getattr(config, "ADMIN_IDS", [])
    if not admin_ids or admin_id is None:
        return False
    try:
        return int(admin_id) == int(admin_ids[0])
    except (TypeError, ValueError):
        return False


def mask_phone(value: Any) -> str:
    digits = "".join(character for character in str(value) if character.isdigit())
    if not digits:
        return ""
    visible = digits[-4:] if len(digits) > 4 else digits[-2:]
    return f"***{visible}"


def phone_digest(value: Any) -> str:
    digits = "".join(character for character in str(value) if character.isdigit())
    return hashlib.sha256(digits.encode("utf-8")).hexdigest() if digits else ""


def _sanitize(value: Any, key: str = "") -> Any:
    if _SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if _PHONE_KEY_RE.search(key):
        if "hash" in key.casefold() or "digest" in key.casefold():
            return str(value)
        if isinstance(value, (list, tuple, set)):
            return [mask_phone(item) for item in value]
        return mask_phone(value)
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item, key) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sanitize_error(value: Any) -> Optional[str]:
    if not value:
        return None
    text = _URL_RE.sub("[URL REDACTED]", str(value))
    text = _LONG_DIGIT_RE.sub(lambda match: mask_phone(match.group(0)), text)
    return text[:500]


class AdminAuditLog:
    """Append-only administrator audit trail with bounded local retention."""

    _lock = threading.RLock()
    _last_prune_date = None

    @classmethod
    def _path(cls) -> str:
        return os.path.abspath(ADMIN_AUDIT_FILE)

    @classmethod
    def _append(cls, entry: Dict[str, Any]) -> bool:
        path = cls._path()
        try:
            with cls._lock:
                cls._prune_if_due_locked()
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "a", encoding="utf-8") as stream:
                    json.dump(entry, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            return True
        except Exception:
            logger.critical("管理员审计日志写入失败", exc_info=True)
            return False

    @classmethod
    def record_attempt(
        cls,
        admin_id: int,
        action: str,
        target_type: str = "system",
        target_id: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        if _is_audit_exempt_admin(admin_id):
            return _EXEMPT_AUDIT_ID
        audit_id = uuid.uuid4().hex
        entry = {
            "audit_id": audit_id,
            "timestamp": datetime.now().isoformat(),
            "admin_id": int(admin_id),
            "action": str(action),
            "target_type": str(target_type),
            "target_id": None if target_id is None else str(target_id),
            "result": "attempt",
            "before": None,
            "after": None,
            "metadata": _sanitize(metadata or {}),
            "error": None,
        }
        return audit_id if cls._append(entry) else None

    @classmethod
    def record_result(
        cls,
        audit_id: Optional[str],
        result: str,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        *,
        admin_id: Optional[int] = None,
        action: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if result not in {"success", "failed", "cancelled"}:
            raise ValueError("invalid audit result")
        if audit_id == _EXEMPT_AUDIT_ID or _is_audit_exempt_admin(admin_id):
            return True
        if audit_id and (admin_id is None or action is None or target_type is None):
            history = cls.get_by_audit_id(audit_id)
            source = history[-1] if history else {}
            admin_id = source.get("admin_id") if admin_id is None else admin_id
            action = source.get("action") if action is None else action
            target_type = source.get("target_type") if target_type is None else target_type
            target_id = source.get("target_id") if target_id is None else target_id
        if _is_audit_exempt_admin(admin_id):
            return True
        if admin_id is None or action is None:
            logger.critical("管理员审计结果缺少管理员或动作信息")
            return False
        entry = {
            "audit_id": audit_id or uuid.uuid4().hex,
            "timestamp": datetime.now().isoformat(),
            "admin_id": int(admin_id),
            "action": str(action),
            "target_type": str(target_type or "system"),
            "target_id": None if target_id is None else str(target_id),
            "result": result,
            "before": _sanitize(before) if before is not None else None,
            "after": _sanitize(after) if after is not None else None,
            "metadata": _sanitize(metadata or {}),
            "error": _sanitize_error(error),
        }
        return cls._append(entry)

    @classmethod
    def _read_entries_locked(cls) -> list[Dict[str, Any]]:
        try:
            with open(cls._path(), "r", encoding="utf-8") as stream:
                entries = []
                for line in stream:
                    try:
                        item = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(item, dict):
                        entries.append(item)
                return entries
        except FileNotFoundError:
            return []
        except Exception:
            logger.exception("读取管理员审计日志失败")
            return []

    @classmethod
    def query(
        cls,
        filters: Optional[Dict[str, Any]] = None,
        page: int = 0,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        filters = filters or {}
        with cls._lock:
            entries = cls._read_entries_locked()
        for field in ("admin_id", "action", "target_id", "result"):
            expected = filters.get(field)
            if expected not in (None, ""):
                entries = [item for item in entries if str(item.get(field)) == str(expected)]
        if filters.get("exclude_attempt"):
            entries = [item for item in entries if item.get("result") != "attempt"]
        entries.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
        page_size = max(1, min(int(page_size), 100))
        max_page = max(0, (len(entries) - 1) // page_size) if entries else 0
        page = max(0, min(int(page), max_page))
        start = page * page_size
        return {
            "items": entries[start:start + page_size],
            "page": page,
            "max_page": max_page,
            "total": len(entries),
        }

    @classmethod
    def get_by_audit_id(cls, audit_id: str) -> list[Dict[str, Any]]:
        with cls._lock:
            entries = cls._read_entries_locked()
        return sorted(
            [item for item in entries if item.get("audit_id") == audit_id],
            key=lambda item: str(item.get("timestamp", "")),
        )

    @classmethod
    def prune(cls, retention_days: int = ADMIN_AUDIT_RETENTION_DAYS) -> bool:
        with cls._lock:
            return cls._prune_locked(retention_days)

    @classmethod
    def _prune_if_due_locked(cls) -> None:
        today = datetime.now().date()
        if cls._last_prune_date == today:
            return
        if cls._prune_locked(ADMIN_AUDIT_RETENTION_DAYS):
            cls._last_prune_date = today

    @classmethod
    def _prune_locked(cls, retention_days: int) -> bool:
        path = cls._path()
        if not os.path.exists(path):
            cls._last_prune_date = datetime.now().date()
            return True
        cutoff = datetime.now() - timedelta(days=max(1, int(retention_days)))
        temp_path = None
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix="admin_audit.", suffix=".tmp", dir=os.path.dirname(path) or ".", text=True
            )
            with open(path, "r", encoding="utf-8") as source, os.fdopen(
                fd, "w", encoding="utf-8"
            ) as target:
                for line in source:
                    keep = True
                    try:
                        item = json.loads(line)
                        timestamp = datetime.fromisoformat(str(item.get("timestamp", "")))
                        keep = timestamp >= cutoff
                    except Exception:
                        logger.error("审计日志包含无法解析的记录，已原样保留")
                    if keep:
                        target.write(line if line.endswith("\n") else line + "\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, path)
            temp_path = None
            cls._last_prune_date = datetime.now().date()
            return True
        except Exception:
            logger.exception("清理管理员审计日志失败")
            return False
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


def audit_file_path() -> str:
    return os.path.abspath(ADMIN_AUDIT_FILE)
