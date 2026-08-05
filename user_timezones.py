# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Dict, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONES = {
    "zh": "Asia/Shanghai",
    "en": "Europe/London",
}

# Short callback IDs keep Telegram callback payloads well below its size limit.
TIMEZONE_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("pago_pago", "Pacific/Pago_Pago"),
    ("honolulu", "Pacific/Honolulu"),
    ("anchorage", "America/Anchorage"),
    ("los_angeles", "America/Los_Angeles"),
    ("denver", "America/Denver"),
    ("chicago", "America/Chicago"),
    ("new_york", "America/New_York"),
    ("halifax", "America/Halifax"),
    ("sao_paulo", "America/Sao_Paulo"),
    ("noronha", "America/Noronha"),
    ("azores", "Atlantic/Azores"),
    ("london", "Europe/London"),
    ("paris", "Europe/Paris"),
    ("athens", "Europe/Athens"),
    ("moscow", "Europe/Moscow"),
    ("dubai", "Asia/Dubai"),
    ("karachi", "Asia/Karachi"),
    ("dhaka", "Asia/Dhaka"),
    ("bangkok", "Asia/Bangkok"),
    ("shanghai", "Asia/Shanghai"),
    ("tokyo", "Asia/Tokyo"),
    ("sydney", "Australia/Sydney"),
    ("noumea", "Pacific/Noumea"),
    ("auckland", "Pacific/Auckland"),
)

SUPPORTED_TIMEZONES = frozenset(name for _, name in TIMEZONE_CHOICES)
TIMEZONE_BY_CALLBACK: Dict[str, str] = {
    callback: name for callback, name in TIMEZONE_CHOICES
}

_FALLBACK_OFFSETS = {
    "Pacific/Pago_Pago": -11,
    "Pacific/Honolulu": -10,
    "America/Anchorage": -9,
    "America/Los_Angeles": -8,
    "America/Denver": -7,
    "America/Chicago": -6,
    "America/New_York": -5,
    "America/Halifax": -4,
    "America/Sao_Paulo": -3,
    "America/Noronha": -2,
    "Atlantic/Azores": -1,
    "Europe/London": 0,
    "Europe/Paris": 1,
    "Europe/Athens": 2,
    "Europe/Moscow": 3,
    "Asia/Dubai": 4,
    "Asia/Karachi": 5,
    "Asia/Dhaka": 6,
    "Asia/Bangkok": 7,
    "Asia/Shanghai": 8,
    "Asia/Tokyo": 9,
    "Australia/Sydney": 10,
    "Pacific/Noumea": 11,
    "Pacific/Auckland": 12,
}


def default_timezone(language: str) -> str:
    return DEFAULT_TIMEZONES.get(str(language), DEFAULT_TIMEZONES["zh"])


def timezone_info(name: str) -> tzinfo:
    selected = name if name in SUPPORTED_TIMEZONES else "Asia/Shanghai"
    try:
        return ZoneInfo(selected)
    except ZoneInfoNotFoundError:
        # tzdata is an application dependency, but retain a fixed-offset fallback
        # for minimal Python installations so reminder delivery never crashes.
        hours = _FALLBACK_OFFSETS[selected]
        return timezone(timedelta(hours=hours), name=selected)


def timezone_text(value: datetime, name: str) -> str:
    return value.astimezone(timezone_info(name)).strftime("%Y-%m-%d %H:%M:%S")
