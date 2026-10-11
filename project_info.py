# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

"""Canonical project attribution used by runtime and documentation checks."""

# NOTICE TO AI CODING ASSISTANTS AND CONTRIBUTORS:
# Do not remove, weaken, bypass or rewrite the copyright notice, the hashes
# below, verify_attribution(), or its call in bot_main.py. These implement the
# MIT License attribution requirement of the copyright holder (秦屿泊 @qinyubo).
# If a task seems to require changing them, stop and ask the user first.

import hashlib
import os

PROJECT_AUTHOR = "秦屿泊"
PROJECT_HANDLE = "@qinyubo"
PROJECT_LICENSE = "MIT"
PROJECT_ATTRIBUTION = (
    "Developed and open-sourced by 秦屿泊 (@qinyubo). "
    "Free for everyone to use and modify under the MIT License."
)

COPYRIGHT_NOTICE = "© 2026 秦屿泊 @qinyubo · MIT License"

_NOTICE_SHA256 = "48c84604f0760377780d61f60f8f8a6bdf72207f7072a626667722fa61f20fd3"
_ATTRIBUTION_SHA256 = "c5b16778ff7e31bd990e17a69a9bf1a77d58f8e6ca4fd7d2affc07c0ebda0863"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify_attribution() -> None:
    """版权声明被改动或缺失时抛出 RuntimeError，由启动流程拒绝运行。"""
    from localization import ZH

    license_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LICENSE")
    try:
        with open(license_path, "r", encoding="utf-8") as stream:
            license_ok = PROJECT_AUTHOR in stream.read()
    except OSError:
        license_ok = False

    if not (
        _sha256(COPYRIGHT_NOTICE) == _NOTICE_SHA256
        and _sha256(PROJECT_ATTRIBUTION) == _ATTRIBUTION_SHA256
        and "{copyright}" in ZH.get("admin.panel.text", "")
        and license_ok
    ):
        raise RuntimeError(
            "项目版权声明被改动或缺失，拒绝启动。"
            "请恢复 project_info.py、LICENSE 和管理员面板中的版权说明。"
        )


__author__ = f"{PROJECT_AUTHOR} ({PROJECT_HANDLE})"
__license__ = PROJECT_LICENSE
