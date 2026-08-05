# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

"""Build the sanitized runtime archive attached to GitHub Releases."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT_FILES = (
    "AUTHORS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "bot_main.py",
    "config.example.py",
    "localization.py",
    "migrate_runtime_data.py",
    "project_info.py",
    "requirements.txt",
    "settings.py",
    "user_timezones.py",
)
RUNTIME_PACKAGES = ("accounts", "handlers", "payments", "reminders", "storage")
ARCHIVE_COMMENT = (
    b"Anti-Login v1.0.0 | Developed and open-sourced by Qin Yubo "
    b"(@qinyubo) | MIT License"
)


def release_files(root: Path) -> list[Path]:
    files = [root / name for name in ROOT_FILES]
    for package in RUNTIME_PACKAGES:
        files.extend(sorted((root / package).glob("*.py")))
    missing = [str(path.relative_to(root)) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"release inputs missing: {', '.join(missing)}")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def build_release(root: Path, output: Path) -> Path:
    root = root.resolve()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        archive.comment = ARCHIVE_COMMENT
        for path in release_files(root):
            archive.write(path, path.relative_to(root).as_posix())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build_release(args.root, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
