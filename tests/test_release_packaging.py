# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

from pathlib import Path
from zipfile import ZipFile

from scripts.build_release import ARCHIVE_COMMENT, build_release


def test_release_archive_uses_runtime_allowlist(tmp_path):
    output = tmp_path / "Anti-Login-v1.0.0.zip"
    build_release(Path(__file__).resolve().parents[1], output)
    with ZipFile(output) as archive:
        names = set(archive.namelist())
        assert archive.comment == ARCHIVE_COMMENT
    assert "LICENSE" in names
    assert "NOTICE" in names
    assert "project_info.py" in names
    assert "tests/test_release_packaging.py" not in names
    assert "requirements-dev.txt" not in names
    assert not any("__pycache__" in name or name.endswith(".session") for name in names)
