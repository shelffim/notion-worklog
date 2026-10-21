from pathlib import Path

from notion_worklog.agents import claude as transcript

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return transcript.read_entries(FIXTURES / name)


def test_conversation_keeps_only_real_messages():
    messages = transcript.conversation(load("with_aititle.jsonl"))

    assert messages
    assert all(m["role"] in ("user", "assistant") for m in messages)
    assert all(m["text"] for m in messages)
    assert all(m["uuid"] for m in messages)


def test_conversation_drops_slash_command_noise():
    messages = transcript.conversation(load("with_aititle.jsonl"))
    joined = "\n".join(m["text"] for m in messages)

    for tag in ("<command-name>", "<command-message>",
                "<local-command-stdout>", "<local-command-caveat>"):
        assert tag not in joined


def test_conversation_finds_the_single_real_prompt():
    messages = transcript.conversation(load("with_aititle.jsonl"))
    prompts = [m["text"] for m in messages if m["role"] == "user"]

    assert prompts == ["skill-creator 플러그인 설치해줘"]


def test_conversation_excludes_tool_results():
    """tool_result / tool_use 본문이 대화에서 빠지는지 실제 픽스처로 확인한다.

    tool_result 는 user 역할로 들어오지만 text 블록이 없어 빈 문자열이 된다.
    아래 문자열들은 with_aititle.jsonl 안에 실제로 들어 있는 도구 입출력이라,
    필터가 사라지면 그대로 본문에 새어 들어온다.
    """
    raw = (FIXTURES / "with_aititle.jsonl").read_text(encoding="utf-8")
    leaks = [
        "Configured marketplaces:",                       # Bash tool_result 내용
        "Successfully installed plugin",                  # Bash tool_result 내용
        "claude plugin marketplace list",                 # tool_use 의 command
        "installed_plugins.json",                         # tool_result 내용
    ]
    # 픽스처가 바뀌어 이 문자열이 사라지면 테스트가 다시 공허해지므로 먼저 확인한다
    assert all(leak in raw for leak in leaks)

    joined = "\n".join(m["text"] for m in transcript.conversation(load("with_aititle.jsonl")))

    for leak in leaks:
        assert leak not in joined


def test_conversation_of_session_without_any_real_prompt():
    """no_aititle.jsonl 은 사람이 친 프롬프트가 하나도 없는 세션이다.

    (진짜 사용자 입력은 전부 슬래시 커맨드 찌꺼기·에이전트 알림이라 걸러진다)
    그래도 어시스턴트 답변 2개는 남아야 한다 — 빈 페이지가 되면 안 된다.
    """
    messages = transcript.conversation(load("no_aititle.jsonl"))

    assert len(messages) == 2
    assert [m["role"] for m in messages] == ["assistant", "assistant"]
    assert all(m["text"] and m["uuid"] for m in messages)
    assert "statusline-setup" in messages[0]["text"]


def test_conversation_of_session_with_nothing_to_record():
    # no_prompt.jsonl 은 기록할 대화가 0개다. 예외 없이 빈 리스트여야 한다.
    entries = load("no_prompt.jsonl")

    assert entries              # 파일 자체는 비어 있지 않다
    assert transcript.conversation(entries) == []
    assert transcript.conversation(entries, after_uuid=None) == []


def test_conversation_after_uuid_returns_only_newer():
    entries = load("with_aititle.jsonl")
    everything = transcript.conversation(entries)
    assert len(everything) >= 2

    cut = everything[0]["uuid"]
    rest = transcript.conversation(entries, after_uuid=cut)

    assert len(rest) == len(everything) - 1
    assert all(m["uuid"] != cut for m in rest)


def test_conversation_unknown_uuid_returns_empty():
    entries = load("with_aititle.jsonl")
    assert transcript.conversation(entries, after_uuid="존재하지-않는-uuid") == []


def test_conversation_handles_empty_entries():
    assert transcript.conversation([]) == []
