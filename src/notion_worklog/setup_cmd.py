"""notion-worklog setup / uninstall — 사람이 직접 부르는 명령들."""
import argparse
import getpass
import os

from . import __version__, config, install, schema
from .text import LANGS

INTEGRATIONS_URL = "https://www.notion.so/profile/integrations"


def _parser():
    parser = argparse.ArgumentParser(
        prog="notion-worklog",
        description="Claude Code·Codex 세션을 Notion 테이블에 자동으로 기록합니다.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="토큰·테이블을 설정하고 훅을 등록합니다")
    setup.add_argument("--database", help="Notion 테이블 URL 또는 id (인라인 테이블이면 그 페이지 URL)")
    setup.add_argument("--lang", choices=LANGS, help="새로 만드는 열 이름과 본문 머리글의 언어")
    setup.add_argument("--agents", help="훅을 등록할 에이전트, 쉼표로 구분 (claude,codex). "
                                        "기본: 설치된 것을 자동 감지")
    setup.add_argument("--no-hooks", action="store_true", help="훅은 등록하지 않습니다")

    sub.add_parser("uninstall", help="등록한 훅을 제거합니다 (설정과 토큰은 남깁니다)")
    return parser


def _default_api(token):
    from .notion import Notion
    return Notion(token, None)


def main(argv, make_api=None, home=None, ask=input, ask_secret=getpass.getpass, out=print):
    args = _parser().parse_args(argv)
    if args.command == "uninstall":
        return _uninstall(home, out)
    return _setup(args, make_api or _default_api, home, ask, ask_secret, out)


def _setup(args, make_api, home, ask, ask_secret, out):
    current = config.load_config()

    # 1. 토큰
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        existing = config.load_token()
        hint = " (Enter: 저장된 토큰 유지)" if existing else ""
        out("Notion 통합 토큰이 필요합니다. 없으면 {} 에서 만드세요.".format(INTEGRATIONS_URL))
        token = ask_secret("Notion 통합 토큰{}: ".format(hint)).strip() or existing
    if not token:
        out("토큰이 없어 중단합니다.")
        return 1
    api = make_api(token)
    try:
        api.me()
    except Exception:
        out("토큰을 확인할 수 없습니다. 토큰을 다시 복사해 주세요.")
        return 1

    # 2. 테이블
    target = args.database
    if not target:
        hint = " (Enter: 지금 테이블 유지)" if current.get("database_id") else ""
        target = ask("기록할 Notion 테이블 URL{}: ".format(hint)).strip() or current.get("database_id")
    if not target:
        out("테이블 URL 이 없어 중단합니다.")
        return 1
    try:
        database = schema.resolve_database(api, schema.parse_id(target))
    except schema.SeveralDatabases as several:
        for number, (_, title) in enumerate(several.candidates, start=1):
            out("  {}. {}".format(number, title or "(제목 없음)"))
        choice = ask("이 페이지에 테이블이 여러 개 있습니다. 번호를 고르세요: ").strip()
        try:
            chosen = several.candidates[int(choice) - 1][0]
            database = api.get_database(chosen)
        except (ValueError, IndexError):
            out("잘못된 번호입니다.")
            return 1
        except Exception:
            out("그 테이블을 읽을 수 없습니다. 통합이 연결돼 있는지 확인하세요.")
            return 1
    except (ValueError, LookupError) as error:
        out(str(error))
        return 1

    # 3. 언어와 열
    lang = args.lang or current.get("lang")
    if not lang:
        lang = (ask("언어 / language [ko/en] (기본 en): ").strip().lower() or "en")
        if lang not in LANGS:
            out("ko 또는 en 중에서 골라 주세요.")
            return 1
    try:
        columns, created = schema.ensure_columns(api, database, lang)
    except ValueError as error:
        out(str(error))
        return 1

    # 4. 저장 — 여기까지 모두 성공했을 때만
    config.save_token(token)
    config.save_config({"database_id": database["id"], "columns": columns, "lang": lang})
    out("테이블: {}".format(database["id"]))
    out("열: {}".format(", ".join("{}={}".format(role, name) for role, name in columns.items())))
    if created:
        out("새로 만든 열: {}".format(", ".join(created)))
    out("설정 저장: {} (토큰은 {} 에 소유자 전용 권한으로)".format(config.CONFIG_PATH, config.TOKEN_PATH))

    # 5. 훅
    if args.no_hooks:
        return 0
    agents = ([a.strip() for a in args.agents.split(",") if a.strip()]
              if args.agents else install.detect_agents(home))
    unknown = [a for a in agents if a not in ("claude", "codex")]
    if unknown:
        out("알 수 없는 에이전트: {}".format(", ".join(unknown)))
        return 1
    if not agents:
        out("Claude Code(~/.claude)나 Codex(~/.codex)를 찾지 못해 훅을 등록하지 않았습니다.")
        return 0
    for agent in agents:
        path = install.install(agent, install.settings_path(agent, home))
        out("훅 등록: {} → {}".format(agent, path))
    if "codex" in agents:
        out("Codex: 다음에 codex 를 열면 /hooks 에서 notion-worklog 훅을 신뢰(trust)해 주세요. "
            "신뢰하기 전에는 실행되지 않습니다.")
    out("완료. 다음 세션부터 Notion 에 기록됩니다.")
    return 0


def _uninstall(home, out):
    for agent in ("claude", "codex"):
        path = install.settings_path(agent, home)
        removed = install.uninstall(path)
        if removed:
            out("훅 제거: {} ({}개)".format(path, removed))
    out("설정과 토큰은 {} 에 남아 있습니다. 완전히 지우려면 그 폴더를 지우세요.".format(config.HOME))
    return 0
