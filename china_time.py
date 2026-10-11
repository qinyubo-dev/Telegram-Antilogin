# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def timezone_text(value: datetime) -> str:
    return value.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
