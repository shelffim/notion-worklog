"""명령줄 진입점.

    notion-worklog setup       토큰·Notion 테이블·훅 등록 (대화형)
    notion-worklog uninstall   등록한 훅 제거
    notion-worklog hook ...    에이전트 훅이 부르는 진입점 (사람이 부를 일은 없다)

hook 은 어떤 경우에도 exit 0 이고 stdout 에 아무것도 쓰지 않는다. 실패는
~/.notion-worklog/error.log 로만 남긴다. 그래서 hook 경로에서는 표준 라이브러리
밖의 모듈(requests 등)을 가드 안에서만 불러온다.
"""
import json
import subprocess
import sys
import traceback

from . import config

AGENT_KEYS = ("claude", "codex")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["hook"]:
        _hook_main(argv[1:])
        sys.exit(0)
    from . import setup_cmd
    sys.exit(setup_cmd.main(argv))


# --- hook -----------------------------------------------------------------

def _hook_main(args):
    """argparse 를 쓰지 않는다 — 인자 오류에도 stderr 출력 없이 exit 0 이어야 한다."""
    try:
        agent_key = None
        detach = False
        rest = list(args)
        while rest:
            arg = rest.pop(0)
            if arg == "--agent" and rest:
                agent_key = rest.pop(0)
            elif arg == "--detach":
                detach = True
            else:
                config.log_error("hook: 알 수 없는 인자 {!r}".format(arg))
                return
        if agent_key not in AGENT_KEYS:
            config.log_error("hook: --agent 는 {} 중 하나여야 한다 (받은 값: {!r})".format(
                "/".join(AGENT_KEYS), agent_key))
            return

        raw = sys.stdin.buffer.read()
        if detach:
            _spawn_detached(agent_key, raw)
            return
        _run_hook(agent_key, raw)
    except BaseException:
        # SystemExit/KeyboardInterrupt 까지 삼킨다 — 훅은 exit 0 이어야 한다.
        config.log_error("hook:\n{}".format(traceback.format_exc(limit=3)))


def _spawn_detached(agent_key, raw):
    """페이로드를 분리된 자식에게 넘기고 곧바로 돌아온다.

    Codex 의 SessionEnd 훅은 동기 실행이고 최대 3초다. 동기화는 수십 초 걸릴 수
    있으므로 새 세션(start_new_session)의 자식이 맡는다 — 부모가 끝나거나 Codex
    가 부모를 정리해도 자식은 살아남는다.
    """
    child = subprocess.Popen(
        [sys.executable, "-m", "notion_worklog", "hook", "--agent", agent_key],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    child.stdin.write(raw)
    child.stdin.close()


def _run_hook(agent_key, raw):
    try:
        payload = json.loads(raw)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return

    try:
        from . import sync
    except BaseException:
        # venv 손상, 의존성 누락 등. SystemExit 까지 잡아야 exit 0 이 지켜진다.
        config.log_error("import 실패:\n{}".format(traceback.format_exc(limit=3)))
        return

    try:
        sync.run(payload, agent_key)
    except Exception:
        config.log_error(traceback.format_exc(limit=3))
