# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from __future__ import annotations

import subprocess
import sys


PRODUCTION_TARGETS = (
    "accounts",
    "handlers",
    "payments",
    "reminders",
    "storage",
    "bot_main",
    "localization",
    "migrate_runtime_data",
    "settings",
)

CORE_GROUPS = {
    "payments": "payments/*",
    "storage": "storage/*",
    "account support": ",".join(
        (
            "accounts/account_runtime.py",
            "accounts/account_session_files.py",
            "accounts/login_code_monitor.py",
            "accounts/session_upload.py",
        )
    ),
}


def run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def main() -> None:
    coverage_args = [item for target in PRODUCTION_TARGETS for item in ("--cov", target)]
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *coverage_args,
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-report=xml:.pytest_cache/coverage.xml",
            "--cov-report=json:.pytest_cache/coverage.json",
            "--cov-fail-under=84",
        ]
    )
    for name, include in CORE_GROUPS.items():
        print(f"Checking {name} coverage >= 90%")
        run(
            [
                sys.executable,
                "-m",
                "coverage",
                "report",
                f"--include={include}",
                "--fail-under=90",
            ]
        )


if __name__ == "__main__":
    main()
