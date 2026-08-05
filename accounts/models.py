# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class SessionCleanupResult:
    ok: bool
    action: str
    reason: str
    path: str = ""


@dataclass
class ExistingAccountCheck:
    action: str
    phone: str
    message: str
    status: str = ""
    reason: str = ""


@dataclass
class AccountTransferResult:
    ok: bool
    code: str
    message: str
    phone: str = ""
    from_user_id: int = 0
    to_user_id: int = 0
    target_notified: bool = False
    message_key: str = ""
    message_values: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountCleanupResult:
    status: str
    chats_deleted: int = 0
    contacts_deleted: int = 0
    errors: List[str] = field(default_factory=list)
