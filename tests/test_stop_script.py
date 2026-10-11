# Copyright (c) 2026 秦屿泊 (@qinyubo)
# SPDX-License-Identifier: MIT

import importlib.util
import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "stop.py")
spec = importlib.util.spec_from_file_location("stop_script", SCRIPT)
stop_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stop_script)


def test_read_pid_handles_missing_and_invalid_file(tmp_path):
    assert stop_script.read_pid(str(tmp_path / "missing")) is None
    bad = tmp_path / "bad.pid"
    bad.write_text("abc")
    assert stop_script.read_pid(str(bad)) is None
    good = tmp_path / "good.pid"
    good.write_text("123\n")
    assert stop_script.read_pid(str(good)) == 123


def test_main_reports_not_running_without_pid_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["stop.py", "--pid-file", str(tmp_path / "none.pid")])
    assert stop_script.main() == 0
    assert "未在运行" in capsys.readouterr().out


def test_stop_terminates_running_process(tmp_path, monkeypatch):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pid_file = tmp_path / "bot.pid"
        pid_file.write_text(str(process.pid))
        monkeypatch.setattr(
            sys, "argv", ["stop.py", "--pid-file", str(pid_file), "--timeout", "10"]
        )
        assert stop_script.main() == 0
        process.wait(timeout=10)
        assert process.returncode is not None
    finally:
        if process.poll() is None:
            process.kill()
