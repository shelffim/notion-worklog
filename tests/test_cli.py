"""훅 진입점. 어떤 경우에도 exit 0, stdout 무음.

stdout 이 "{" 로 시작해 "}" 로 끝나면 Claude Code 가 JSON 으로 파싱해 hook error
알림을 띄우고, Codex 는 Stop 훅의 비JSON stdout 을 오류로 본다.
"""
import io
import json
import subprocess
import sys
import time

import pytest

from notion_worklog import cli


def hook(tmp_path, stdin, *args, env_extra=None):
    env = {"PATH": "/usr/bin:/bin", "NOTION_WORKLOG_HOME": str(tmp_path / "home")}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-m", "notion_worklog", "hook", *args],
        input=stdin, capture_output=True, text=True, env=env, timeout=60,
    )


def error_log(tmp_path):
    path = tmp_path / "home" / "error.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_garbage_stdin_exits_zero_silently(tmp_path):
    result = hook(tmp_path, "이건 JSON이 아님", "--agent", "claude")

    assert result.returncode == 0
    assert result.stdout == ""


def test_missing_setup_exits_zero_silently_and_leaves_a_trace(tmp_path):
    payload = json.dumps({"session_id": "x", "transcript_path": "/없음", "cwd": "/tmp"})

    result = hook(tmp_path, payload, "--agent", "claude")

    assert result.returncode == 0
    assert result.stdout == ""
    assert "setup" in error_log(tmp_path)


@pytest.mark.parametrize("args", [("--agent", "cursor"), (), ("--bogus",)])
def test_bad_arguments_exit_zero_silently(tmp_path, args):
    result = hook(tmp_path, "{}", *args)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_import_failure_exits_zero_silently(tmp_path):
    """임포트 중 SystemExit 이 나도 exit 0 을 지켜야 한다.

    SystemExit 은 Exception 하위가 아니므로 좁게 잡으면 그대로 프로세스를 죽인다.
    """
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "requests.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")

    result = hook(tmp_path, '{"session_id":"x"}', "--agent", "claude",
                  env_extra={"PYTHONPATH": str(shadow)})

    assert result.returncode == 0
    assert result.stdout == ""
    assert "import" in error_log(tmp_path)


class FakePipe(io.BytesIO):
    def close(self):
        self.sent = self.getvalue()
        super().close()


class FakeProc:
    spawned = []

    def __init__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.stdin = FakePipe()
        FakeProc.spawned.append(self)


def test_detach_hands_stdin_to_a_background_process(monkeypatch):
    """Codex 의 SessionEnd 는 동기 실행에 최대 3초다. 일은 분리된 자식이 한다."""
    FakeProc.spawned = []
    monkeypatch.setattr(cli.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"session_id":"s"}')))

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["hook", "--agent", "codex", "--detach"])

    assert exit_info.value.code == 0
    (child,) = FakeProc.spawned
    assert child.args[1:] == ["-m", "notion_worklog", "hook", "--agent", "codex"]
    assert child.kwargs["start_new_session"] is True
    assert child.stdin.sent == b'{"session_id":"s"}'


def test_detached_child_really_runs(tmp_path):
    """부모는 곧바로 끝나고, 자식이 페이로드를 받아 처리한다 (설정이 없어 로그만 남는다)."""
    payload = json.dumps({"session_id": "x", "transcript_path": "/없음"})

    started = time.monotonic()
    result = hook(tmp_path, payload, "--agent", "codex", "--detach")
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert result.stdout == ""
    assert elapsed < 3
    deadline = time.monotonic() + 15
    while "setup" not in error_log(tmp_path) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert "setup" in error_log(tmp_path)
