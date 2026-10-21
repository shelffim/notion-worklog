"""에이전트 설정 파일에 훅을 등록·해제한다.

Claude Code 는 ~/.claude/settings.json, Codex 는 ~/.codex/hooks.json 을 쓴다. 둘 다
{"hooks": {이벤트: [{"hooks": [핸들러, ...]}, ...]}} 모양이다. 우리 핸들러는 명령에
MARK 가 들어 있는 것으로 알아본다 — 사용자의 다른 훅과 설정은 그대로 둔다.
"""
import json
import shlex
import shutil
import sys
from pathlib import Path

MARK = "-m notion_worklog hook"

# 훅이 Notion 과 주고받는 동안 SIGKILL 되지 않을 예산. 백그라운드(async)로 돌아
# 사용자를 붙잡지 않으므로 넉넉히 준다.
HOOK_TIMEOUT = 120
# Codex 는 끝날 때 async 훅을 죽인다 — codex exec 실측에서 async Stop 훅은 기록을
# 하나도 남기지 못했다. 그래서 Codex 훅은 둘 다 동기로 불려 --detach 로 분리된
# 자식에게 일을 넘기고 곧바로 돌아온다. SessionEnd 는 최대 3초다.
CODEX_STOP_TIMEOUT = 10
CODEX_SESSION_END_TIMEOUT = 3


def settings_path(agent_key, home=None):
    home = Path(home) if home is not None else Path.home()
    if agent_key == "claude":
        return home / ".claude" / "settings.json"
    return home / ".codex" / "hooks.json"


def detect_agents(home=None):
    """설정 디렉터리가 있는 에이전트들."""
    home = Path(home) if home is not None else Path.home()
    return [key for key, name in (("claude", ".claude"), ("codex", ".codex"))
            if (home / name).is_dir()]


def hook_command(agent_key, python=None, detach=False):
    """PATH 에 기대지 않도록 이 패키지가 설치된 파이썬의 절대 경로로 부른다."""
    command = "{} {} --agent {}".format(shlex.quote(python or sys.executable), MARK, agent_key)
    return command + " --detach" if detach else command


def _handlers(agent_key, python):
    if agent_key == "codex":
        detached = hook_command(agent_key, python, detach=True)
        return {
            "Stop": {"type": "command", "command": detached, "timeout": CODEX_STOP_TIMEOUT},
            "SessionEnd": {"type": "command", "command": detached,
                           "timeout": CODEX_SESSION_END_TIMEOUT},
        }
    background = {"type": "command", "command": hook_command(agent_key, python),
                  "timeout": HOOK_TIMEOUT, "async": True}
    return {"Stop": background, "SessionEnd": dict(background)}


def _load(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("{} 의 최상위가 JSON 객체가 아닙니다".format(path))
    return data


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _remove_ours(data):
    """우리 핸들러를 지우고 지운 개수를 돌려준다. 비게 된 그룹·이벤트도 지운다."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = [h for h in handlers if MARK not in str((h or {}).get("command", ""))]
            removed += len(handlers) - len(kept)
            if kept or len(kept) == len(handlers):
                kept_groups.append(dict(group, hooks=kept))
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return removed


def install(agent_key, path=None, python=None):
    """훅을 등록한다. 두 번 불러도 한 벌만 남는다. 고치기 전 파일은 .bak 로 남긴다."""
    path = Path(path) if path is not None else settings_path(agent_key)
    data = _load(path)
    if path.exists():
        shutil.copy2(str(path), str(path) + ".notion-worklog.bak")

    _remove_ours(data)
    hooks = data.setdefault("hooks", {})
    for event, handler in _handlers(agent_key, python).items():
        hooks.setdefault(event, []).append({"hooks": [handler]})
    _save(path, data)
    return path


def uninstall(path):
    """path 에서 우리 훅을 지우고 지운 개수를 돌려준다. 파일이 없으면 0."""
    path = Path(path)
    if not path.exists():
        return 0
    data = _load(path)
    removed = _remove_ours(data)
    if removed:
        shutil.copy2(str(path), str(path) + ".notion-worklog.bak")
        _save(path, data)
    return removed
