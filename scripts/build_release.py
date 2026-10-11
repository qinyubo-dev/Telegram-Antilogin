# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

"""Create a deployable source archive while keeping AGENTS.md out of Git."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
DEPLOYMENT_FILES = (
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "bot_main.py",
    "china_time.py",
    "config.example.py",
    "localization.py",
    "project_info.py",
    "requirements.txt",
    "settings.py",
)
DEPLOYMENT_DIRECTORIES = (
    "accounts",
    "handlers",
    "payments",
    "reminders",
    "scripts",
    "storage",
)


def _archive_members() -> list[Path]:
    members = [PROJECT_ROOT / name for name in DEPLOYMENT_FILES]
    for directory in DEPLOYMENT_DIRECTORIES:
        members.extend((PROJECT_ROOT / directory).rglob("*.py"))

    missing = [path.relative_to(PROJECT_ROOT) for path in members if not path.is_file()]
    if missing:
        joined = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(f"Deployment package is missing required files: {joined}")
    return sorted(members)


def _safe_version(value: str) -> str:
    if not value or any(char not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-_" for char in value):
        raise ValueError("Version may contain only letters, numbers, dots, hyphens, and underscores.")
    return value


def build_release(version: str) -> tuple[Path, Path]:
    version = _safe_version(version)
    archive_root = f"Anti-login-{version}"
    DIST_DIR.mkdir(exist_ok=True)
    archive_path = DIST_DIR / f"{archive_root}.zip"
    checksum_path = DIST_DIR / f"{archive_root}.zip.sha256"

    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for member in _archive_members():
            relative_name = member.relative_to(PROJECT_ROOT).as_posix()
            archive.write(member, f"{archive_root}/{relative_name}")

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    return archive_path, checksum_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version, for example v1.0.1")
    args = parser.parse_args()
    archive_path, checksum_path = build_release(args.version)
    print(f"Created {archive_path.relative_to(PROJECT_ROOT)}")
    print(f"Created {checksum_path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
