# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

"""优雅停止正在运行的 Anti-Login 进程。

用法:
    python scripts/stop.py              # 发送 SIGINT，等待进程完成清理后退出
    python scripts/stop.py --force      # 超时后强制结束
    python scripts/stop.py --timeout 30

进程 PID 由 bot_main.py 启动时写入 sessions 目录下的 .anti_login.pid。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE_NAME = ".anti_login.pid"


def is_running(pid: int) -> bool:
    if os.name == "nt":
        # Windows 上 os.kill(pid, 0) 会直接终止进程，改用查询退出码。
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(pid_path: str) -> int | None:
    try:
        with open(pid_path, "r", encoding="ascii") as stream:
            return int(stream.read().strip())
    except (OSError, ValueError):
        return None


def stop(pid: int, timeout: float, force: bool) -> bool:
    """请求进程退出并等待；返回进程是否已经退出。"""
    # Windows 无法向其他进程发送 SIGINT，只能直接终止。
    graceful = signal.SIGINT if os.name != "nt" else signal.SIGTERM
    os.kill(pid, graceful)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(pid):
            return True
        time.sleep(0.5)
    if not force:
        return False
    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    time.sleep(1)
    return not is_running(pid)


def main() -> int:
    parser = argparse.ArgumentParser(description="停止 Anti-Login 进程")
    parser.add_argument("--timeout", type=float, default=60, help="等待退出的秒数，默认 60")
    parser.add_argument("--force", action="store_true", help="超时后强制结束进程")
    parser.add_argument("--pid-file", help="PID 文件路径，默认读取 sessions 目录")
    args = parser.parse_args()

    pid_path = args.pid_file
    if not pid_path:
        sys.path.insert(0, ROOT)
        import settings

        pid_path = os.path.join(settings.SESSIONS_DIR, PID_FILE_NAME)

    pid = read_pid(pid_path)
    if pid is None or not is_running(pid):
        print("Anti-Login 未在运行")
        return 0

    print(f"正在停止 Anti-Login (PID {pid})...")
    if stop(pid, args.timeout, args.force):
        print("已停止")
        return 0
    print(f"{args.timeout:g} 秒内未退出，可加 --force 强制结束")
    return 1


if __name__ == "__main__":
    sys.exit(main())
