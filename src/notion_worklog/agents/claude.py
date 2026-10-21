"""Claude Code 세션 jsonl 파싱. 네트워크 접근 없음 — 전부 순수 함수."""
import re

from ..transcript import fallback_title, parse_timestamp, read_entries  # noqa: F401

NAME = "Claude Code"
SHORT = "Claude"

# type == "user" 로 들어오지만 사람이 친 입력이 아닌 것들.
# 실제 픽스처에서 관찰된 태그만 넣는다.
NOISE_PREFIX = re.compile(
    r"^\s*<(command-name|command-message|command-args"
    r"|local-command-stdout|local-command-caveat"
    r"|task-notification|system-reminder)\b"
)

TITLE_MAX = 100


def message_text(entry):
    """엔트리의 사람이 읽을 텍스트.

    content 가 문자열이면 그대로, 리스트면 type == "text" 블록만 모은다.
    tool_use / tool_result 블록은 빠지므로 결과가 빈 문자열이 된다.
    """
    message = entry.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


def is_real_user_prompt(entry):
    """사람이 직접 친 프롬프트인지."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    text = message_text(entry)
    return bool(text) and NOISE_PREFIX.match(text) is None


def started_at(entries):
    """세션 시작 시각을 설정 시간대의 aware datetime 으로. 없으면 None."""
    for entry in entries:
        parsed = parse_timestamp(entry.get("timestamp"))
        if parsed is not None:
            return parsed
    return None


def project_name(entries, fallback_cwd=""):
    """cwd 의 마지막 경로 조각. 없으면 인자로 받은 cwd, 그것도 없으면 unknown."""
    for entry in entries:
        cwd = entry.get("cwd")
        if cwd:
            return cwd.rstrip("/").split("/")[-1]
    if fallback_cwd:
        return fallback_cwd.rstrip("/").split("/")[-1]
    return "unknown"


def title(entries, project, when, lang="ko"):
    """제목 4단계 폴백.

    1. Claude Code 가 붙인 aiTitle
    2. last-prompt 엔트리의 lastPrompt
    3. 첫 진짜 사용자 프롬프트의 첫 줄
    4. "<프로젝트> 세션 HH:MM"
    """
    # 각 단계는 strip 후 비어 있지 않을 때만 채택한다. 공백만 있는 값은
    # 다음 단계로 넘긴다 — 빈 제목이 Notion 으로 가는 것을 막는다.
    for entry in reversed(entries):
        if entry.get("type") == "ai-title":
            candidate = str(entry.get("aiTitle") or "").strip()
            if candidate:
                return candidate[:TITLE_MAX]

    for entry in reversed(entries):
        if entry.get("type") == "last-prompt":
            candidate = str(entry.get("lastPrompt") or "").strip()
            if candidate:
                return candidate.splitlines()[0][:TITLE_MAX]

    for entry in entries:
        if is_real_user_prompt(entry):
            first_line = message_text(entry).splitlines()[0].strip()
            if first_line:
                return first_line[:TITLE_MAX]

    return fallback_title(project, when, lang)


def conversation(entries, after_uuid=None):
    """대화를 [{"role", "text", "uuid"}] 로.

    after_uuid 를 주면 그 엔트리 **다음**부터만 돌려준다.
    after_uuid 가 목록에 없으면 빈 리스트를 돌려준다 — 이미 다 기록했거나
    다른 세션의 워터마크라는 뜻이므로, 중복 기록보다 아무것도 안 하는 편이 안전하다.
    """
    if after_uuid is not None:
        uuids = [e.get("uuid") for e in entries]
        if after_uuid not in uuids:
            return []

    started = after_uuid is None
    messages = []

    for entry in entries:
        if not started:
            if entry.get("uuid") == after_uuid:
                started = True
            continue

        uuid = entry.get("uuid")
        if not uuid:
            continue

        if is_real_user_prompt(entry):
            messages.append({"role": "user", "text": message_text(entry), "uuid": uuid})
        elif entry.get("type") == "assistant":
            text = message_text(entry)
            if text:
                messages.append({"role": "assistant", "text": text, "uuid": uuid})

    return messages
