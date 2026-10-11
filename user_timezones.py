# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def china_tz() -> tzinfo:
    try:
        return ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        # tzdata is an application dependency, but retain a fixed offset for
        # minimal Python installations so reminder delivery never crashes.
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def timezone_text(value: datetime) -> str:
    return value.astimezone(china_tz()).strftime("%Y-%m-%d %H:%M:%S")
