# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import os


def session_related_paths(session_path: str):
    if not session_path:
        return []
    return [
        session_path,
        session_path + "-journal",
        session_path + "-shm",
        session_path + "-wal",
    ]


def safe_remove_session_files(session_path: str):
    for p in session_related_paths(session_path):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
