# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from pathlib import Path

from project_info import PROJECT_ATTRIBUTION, PROJECT_AUTHOR, PROJECT_HANDLE


ROOT = Path(__file__).resolve().parents[1]
COPYRIGHT = "Copyright (c) 2026 秦屿泊 (@qinyubo)"
SPDX = "SPDX-License-Identifier: MIT"


def test_project_license_and_attribution_files():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    assert COPYRIGHT in license_text
    assert PROJECT_AUTHOR in notice and PROJECT_HANDLE in notice
    assert PROJECT_ATTRIBUTION in readme


def test_every_tracked_python_file_has_mit_header():
    excluded_parts = {".git", "dist", "__pycache__", ".pytest_cache"}
    files = [
        path for path in ROOT.rglob("*.py")
        if not excluded_parts.intersection(path.relative_to(ROOT).parts)
    ]
    assert files
    missing = []
    for path in files:
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:5])
        if COPYRIGHT not in head or SPDX not in head:
            missing.append(str(path.relative_to(ROOT)))
    assert not missing
