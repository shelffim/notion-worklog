"""에이전트와 무관한 트랜스크립트 도구. 네트워크 접근 없음 — 전부 순수 함수.

에이전트별 파싱(대화 추출, 제목, 시작 시각)은 agents/ 아래 어댑터가 맡는다.
"""
import json
from datetime import datetime

from . import config
from .text import text as _text


def read_entries(path):
    """jsonl 을 dict 리스트로. 깨진 줄은 건너뛴다.

    각 dict 에는 물리적 줄 번호를 "_line" 으로 붙이지 않는다 — 필요한 어댑터
    (codex)가 read_numbered 를 따로 쓴다.
    """
    return [entry for _, entry in read_numbered(path)]


def read_numbered(path):
    """jsonl 을 (줄 번호, dict) 리스트로. 깨진 줄은 건너뛰되 번호는 그대로 센다.

    트랜스크립트는 덧붙이기만 하므로 줄 번호는 한 번 정해지면 바뀌지 않는다.
    메시지 id 가 없는 형식에서 워터마크로 쓸 수 있다.
    """
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            for number, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    entries.append((number, parsed))
    except (OSError, TypeError):
        return []
    return entries


def parse_timestamp(stamp):
    """ISO 8601 문자열을 설정 시간대의 aware datetime 으로. 못 읽으면 None."""
    # 문자열이 아닌 timestamp(정수 등)는 건너뛴다 — .replace 가 터진다
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(config.TZ)


def fallback_title(project, when, lang="ko"):
    """대화 내용이 전혀 섞이지 않은 제목.

    어댑터 title() 의 마지막 단계이자, Cloudflare WAF 가 제목 때문에 요청을
    거부할 때 notion.py 가 제목을 통째로 갈아끼우는 데 쓰는 값이기도 하다. 그래서
    이 문자열에는 트랜스크립트에서 온 텍스트가 한 글자도 들어가면 안 된다 —
    project 는 cwd 의 마지막 경로 조각이고 when 은 타임스탬프다.
    """
    stamp = when.strftime("%H:%M") if when else "??:??"
    return "{} {} {}".format(project, _text(lang)["session"], stamp)


def document(messages, title, project, when, session_id, agent_name, agent_short,
             lang="ko"):
    """세션 전체를 마크다운 문자열로. 네트워크 없음, 순수 함수.

    인라인 블록은 MAX_TEXT_PER_BLOCK 로 잘리고 <script 가 든 본문은 Cloudflare
    WAF 가 거부하므로, 이 함수가 만든 문자열은 별도 파일(session.md)로 첨부돼
    그 우회로가 된다 — 그래서 여기서는 아무 것도 자르거나 치환하지 않는다.
    messages 는 워터마크 없이 뽑은 세션 전체여야 한다.
    """
    t = _text(lang)
    when_text = when.strftime("%Y-%m-%d %H:%M %Z").strip() if when else t["unknown_time"]

    header = "\n".join([
        "# {}".format(title),
        "",
        "- {}: {}".format(t["doc_project"], project),
        "- {}: {}".format(t["doc_started"], when_text),
        "- {}: {}".format(t["doc_session"], session_id),
        "- {}: {}".format(t["doc_agent"], agent_name),
        "",
        t["doc_note"],
        "",
        "---",
    ])

    sections = [header]
    for message in messages:
        role_header = t["doc_user"] if message["role"] == "user" else agent_short
        sections.append("## {}\n\n{}".format(role_header, message["text"]))

    return "\n\n".join(sections) + "\n"
