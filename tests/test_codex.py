"""Codex rollout 어댑터. 픽스처는 실제 rollout 의 구조를 본뜬 합성 데이터다."""
from datetime import timedelta, timezone
from pathlib import Path

from notion_worklog import config, sync
from notion_worklog.agents import codex

FIXTURE = Path(__file__).parent / "fixtures" / "codex_rollout.jsonl"
KST = timezone(timedelta(hours=9))


def load():
    return codex.read_entries(str(FIXTURE))


def test_conversation_keeps_only_real_messages_in_order():
    messages = codex.conversation(load())

    assert [(m["role"], m["text"]) for m in messages] == [
        ("user", "로그인 버그 고쳐줘\n세션이 자꾸 끊겨"),
        ("assistant", "파일을 살펴볼게요."),
        ("assistant", "고쳤습니다. 토큰 갱신 순서를 바꿨어요."),
        ("user", "이 문서 요약해줘"),
        ("assistant", "요약입니다."),
    ]


def test_injected_context_is_dropped():
    joined = "\n".join(m["text"] for m in codex.conversation(load()))

    for noise in ("<environment_context>", "<recommended_plugins>", "AGENTS.md",
                  "permissions instructions", "Use pnpm."):
        assert noise not in joined


def test_tool_calls_and_reasoning_are_dropped():
    raw = FIXTURE.read_text(encoding="utf-8")
    leaks = ["SECRET_TOOL_INPUT", "TOOL_OUTPUT_LEAK", "생각 중"]
    assert all(leak in raw for leak in leaks)   # 픽스처가 바뀌면 테스트가 공허해진다

    joined = "\n".join(m["text"] for m in codex.conversation(load()))

    for leak in leaks:
        assert leak not in joined


def test_messages_without_id_use_their_line_number():
    uuids = [m["uuid"] for m in codex.conversation(load())]

    assert uuids == ["msg_u1", "msg_a1", "msg_a2", "line-14", "line-15"]


def test_after_uuid_returns_only_newer():
    after_id = codex.conversation(load(), after_uuid="msg_a2")
    after_line = codex.conversation(load(), after_uuid="line-14")

    assert [m["uuid"] for m in after_id] == ["line-14", "line-15"]
    assert [m["uuid"] for m in after_line] == ["line-15"]


def test_unknown_watermark_returns_empty():
    assert codex.conversation(load(), after_uuid="없는-id") == []


def test_started_at_and_project_come_from_session_meta(monkeypatch):
    monkeypatch.setattr(config, "TZ", KST)

    when = codex.started_at(load())

    assert (when.hour, when.minute) == (14, 0)          # 05:00Z → KST 14:00
    assert codex.project_name(load(), "/다른/경로") == "my-app"


def test_project_falls_back_to_cwd_argument():
    assert codex.project_name([], "/a/b/other-app") == "other-app"
    assert codex.project_name([], "") == "unknown"


def test_title_is_first_line_of_first_real_prompt(monkeypatch):
    monkeypatch.setattr(config, "TZ", KST)
    entries = load()

    assert codex.title(entries, "my-app", codex.started_at(entries)) == "로그인 버그 고쳐줘"


def test_title_falls_back_when_there_is_no_prompt(monkeypatch):
    monkeypatch.setattr(config, "TZ", KST)
    entries = [e for e in load() if not (e.get("payload") or {}).get("role") == "user"]
    when = codex.started_at(entries)

    assert codex.title(entries, "my-app", when, "en") == "my-app session 14:00"


class RecordingApi:
    def __init__(self):
        self.created, self.appended = [], []
        self.refused = 0

    def find_page(self, session_id, prop_name):
        return None

    def create_page(self, props, fallback_props=None):
        self.created.append(props)
        return "page-new"

    def append_blocks(self, page_id, blocks):
        self.appended.append(blocks)

    def upload_file(self, filename, data, content_type="text/markdown"):
        self.document = data.decode("utf-8")
        return "upload-1"

    def attach_file(self, page_id, file_upload_id, name="session.md"):
        return "block-1"


def test_sync_records_the_agent_and_labels_codex_messages():
    api = RecordingApi()
    payload = {"session_id": "0199-codex-thread", "transcript_path": str(FIXTURE),
               "cwd": "/Users/me/work/my-app", "hook_event_name": "Stop"}
    columns = {"title": "Name", "date": "Date", "project": "Project",
               "session": "Session ID", "agent": "Agent"}

    sync.sync(payload, api, {}, agent=codex, columns=columns, lang="en")

    props = api.created[0]
    assert props["Agent"] == {"select": {"name": "Codex"}}
    assert props["Project"] == {"select": {"name": "my-app"}}
    headings = [b["heading_3"]["rich_text"][0]["text"]["content"]
                for batch in api.appended for b in batch if b["type"] == "heading_3"]
    assert headings == ["🧑 User", "🤖 Codex", "🤖 Codex", "🧑 User", "🤖 Codex"]
    assert "- Agent: Codex" in api.document
    assert "## Codex" in api.document


def test_null_transcript_path_does_nothing():
    api = RecordingApi()

    sync.sync({"session_id": "s", "transcript_path": None}, api, {}, agent=codex)

    assert api.created == []
