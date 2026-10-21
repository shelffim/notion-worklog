"""Codex CLI rollout jsonl 파싱. 네트워크 접근 없음 — 전부 순수 함수.

rollout 한 줄은 {"timestamp", "type", "payload"} 이다. 대화는
type == "response_item" 이고 payload.type == "message" 인 줄에 있다.
event_msg 의 agent_message/user_message 는 같은 내용의 사본이므로 쓰지 않는다.
"""
from ..transcript import fallback_title, parse_timestamp, read_numbered

NAME = "Codex"
SHORT = "Codex"

TITLE_MAX = 100

# role == "user" 로 들어오지만 사람이 친 입력이 아닌 content 항목들. Codex 가
# 턴마다 끼워 넣는 맥락이다. 실제 rollout 에서 관찰된 것만 넣는다.
NOISE_PREFIXES = (
    "<environment_context>",
    "<recommended_plugins>",
    "<user_instructions>",
    "# AGENTS.md instructions",
)

# 파일을 언급하면 Codex 가 요청을 이 머리글들로 감싼다. 요청 부분만 남긴다.
FILES_HEADER = "# Files mentioned by the user"
REQUEST_HEADER = "## My request for Codex:"


def read_entries(path):
    """rollout 을 엔트리 리스트로. 메시지 id 가 없는 옛 형식을 위해 줄 번호를 붙인다."""
    return [dict(entry, _line=number) for number, entry in read_numbered(path)]


def _message(entry):
    """대화 메시지면 payload, 아니면 None."""
    if entry.get("type") != "response_item":
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    if payload.get("role") not in ("user", "assistant"):
        return None
    return payload


def _clean_user_item(text):
    stripped = text.lstrip()
    if stripped.startswith(NOISE_PREFIXES):
        return ""
    if stripped.startswith(FILES_HEADER) and REQUEST_HEADER in stripped:
        return stripped.split(REQUEST_HEADER, 1)[1]
    return text


def _text(payload):
    parts = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict) or item.get("type") not in ("input_text", "output_text"):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        if payload.get("role") == "user":
            text = _clean_user_item(text)
        text = text.strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def conversation(entries, after_uuid=None):
    """대화를 [{"role", "text", "uuid"}] 로.

    uuid 는 메시지 id, 없으면 "line-<줄 번호>" 다. after_uuid 를 주면 그 메시지
    **다음**부터만 돌려주고, 목록에 없으면 빈 리스트를 돌려준다 — 중복 기록보다
    아무것도 안 하는 편이 안전하다.
    """
    messages = []
    for entry in entries:
        payload = _message(entry)
        if payload is None:
            continue
        text = _text(payload)
        if not text:
            continue
        uuid = payload.get("id") or "line-{}".format(entry.get("_line"))
        messages.append({"role": payload["role"], "text": text, "uuid": uuid})

    if after_uuid is None:
        return messages
    for index, message in enumerate(messages):
        if message["uuid"] == after_uuid:
            return messages[index + 1:]
    return []


def _meta(entries):
    for entry in entries:
        if entry.get("type") == "session_meta" and isinstance(entry.get("payload"), dict):
            return entry["payload"]
    return {}


def started_at(entries):
    """세션 시작 시각. session_meta 가 없으면 첫 타임스탬프."""
    parsed = parse_timestamp(_meta(entries).get("timestamp"))
    if parsed is not None:
        return parsed
    for entry in entries:
        parsed = parse_timestamp(entry.get("timestamp"))
        if parsed is not None:
            return parsed
    return None


def project_name(entries, fallback_cwd=""):
    """cwd 의 마지막 경로 조각. session_meta → 인자로 받은 cwd → unknown."""
    for cwd in (_meta(entries).get("cwd"), fallback_cwd):
        if isinstance(cwd, str) and cwd.rstrip("/"):
            return cwd.rstrip("/").split("/")[-1]
    return "unknown"


def title(entries, project, when, lang="ko"):
    """첫 진짜 사용자 프롬프트의 첫 줄. 없으면 대화가 섞이지 않은 대체 제목."""
    for message in conversation(entries):
        if message["role"] == "user":
            first_line = message["text"].splitlines()[0].strip()
            if first_line:
                return first_line[:TITLE_MAX]
    return fallback_title(project, when, lang)
