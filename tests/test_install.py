"""에이전트 설정 파일에 훅을 등록·해제한다. 사용자의 다른 설정은 건드리지 않는다."""
import json

from notion_worklog import install

PY = "/opt/tools/notion worklog/bin/python"      # 공백이 든 경로도 따옴표로 지켜야 한다


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def commands(settings, event):
    return [h["command"] for group in settings["hooks"].get(event, []) for h in group["hooks"]]


OTHER = {
    "model": "opus",
    "hooks": {
        "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
    },
}


def test_claude_registers_async_stop_and_session_end(tmp_path):
    path = tmp_path / "settings.json"

    install.install("claude", path, python=PY)

    settings = read(path)
    for event in ("Stop", "SessionEnd"):
        (entry,) = [h for g in settings["hooks"][event] for h in g["hooks"]]
        assert entry["command"] == "'{}' -m notion_worklog hook --agent claude".format(PY)
        assert entry["async"] is True
        assert entry["timeout"] == 120


def test_claude_keeps_other_settings_and_hooks(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(OTHER), encoding="utf-8")

    install.install("claude", path, python=PY)

    settings = read(path)
    assert settings["model"] == "opus"
    assert settings["hooks"]["SessionStart"] == OTHER["hooks"]["SessionStart"]
    assert "say done" in commands(settings, "Stop")
    assert (tmp_path / "settings.json.notion-worklog.bak").exists()   # 고치기 전 사본


def test_installing_twice_does_not_duplicate(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(OTHER), encoding="utf-8")

    install.install("claude", path, python=PY)
    install.install("claude", path, python="/new/python")

    stop = commands(read(path), "Stop")
    assert stop == ["say done", "/new/python -m notion_worklog hook --agent claude"]


def test_codex_hooks_detach_instead_of_running_in_the_background(tmp_path):
    """Codex 는 끝날 때 async 훅을 죽인다 (codex exec 실측: async Stop 훅이 기록을
    하나도 남기지 못했다). 그래서 두 훅 모두 동기로 불려 분리된 자식에게 일을 넘기고
    곧바로 돌아온다. SessionEnd 는 최대 3초다."""
    path = tmp_path / "hooks.json"

    install.install("codex", path, python=PY)

    settings = read(path)
    for event, limit in (("Stop", 10), ("SessionEnd", 3)):
        (handler,) = [h for g in settings["hooks"][event] for h in g["hooks"]]
        assert handler["command"].endswith("hook --agent codex --detach")
        assert handler["timeout"] <= limit
        assert "async" not in handler


def test_uninstall_removes_only_our_entries(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(OTHER), encoding="utf-8")
    install.install("claude", path, python=PY)

    removed = install.uninstall(path)

    settings = read(path)
    assert removed == 2
    assert settings["hooks"]["SessionStart"] == OTHER["hooks"]["SessionStart"]
    assert commands(settings, "Stop") == ["say done"]
    assert "SessionEnd" not in settings["hooks"]          # 우리 것만 있던 이벤트는 비운다


def test_uninstall_of_a_missing_file_is_a_no_op(tmp_path):
    assert install.uninstall(tmp_path / "nope.json") == 0
    assert not (tmp_path / "nope.json").exists()


def test_detect_agents_by_their_home_directories(tmp_path):
    (tmp_path / ".claude").mkdir()

    assert install.detect_agents(tmp_path) == ["claude"]

    (tmp_path / ".codex").mkdir()
    assert install.detect_agents(tmp_path) == ["claude", "codex"]
