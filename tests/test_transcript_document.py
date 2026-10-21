"""document() — session.md 내용을 만드는 순수 함수 테스트.

Notion 인라인 본문은 블록당 1900자로 잘리고, API 앞단 Cloudflare WAF 는
<script 가 든 본문을 거부한다. document() 는 그 우회로로 첨부할 session.md 의
내용을 만든다 — 자르지도, 치환하지도 않은 세션 전체의 원문 스냅샷이어야 한다.
"""
from datetime import datetime, timedelta, timezone

from notion_worklog import transcript
from notion_worklog.agents import claude

KST = timezone(timedelta(hours=9), "KST")


def document(entries, title, project, when, session_id):
    """Claude 어댑터로 대화를 뽑아 문서를 만든다 (기존 테스트의 호출 모양 유지)."""
    return transcript.document(claude.conversation(entries), title, project, when,
                               session_id, claude.NAME, claude.SHORT, "ko")


def entry(role, text, uuid):
    """conversation() 이 그대로 통과시킬 최소 엔트리를 만든다."""
    entry_type = "user" if role == "user" else "assistant"
    return {"type": entry_type, "uuid": uuid, "message": {"content": text}}


def test_document_has_header():
    entries = [entry("user", "안녕하세요", "u1")]
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    result = document(entries, "제목입니다", "Projects", when, "sess-123")

    assert "# 제목입니다" in result
    assert "- 프로젝트: Projects" in result
    assert "- 세션ID: sess-123" in result
    assert "- 시작: 2026-09-02 21:30 KST" in result
    assert "- 에이전트: Claude Code" in result


def test_document_when_none():
    entries = [entry("user", "안녕하세요", "u1")]

    result = document(entries, "제목", "Projects", None, "sess-123")

    assert "- 시작: 시각 미상" in result


def test_document_includes_both_roles():
    entries = [
        entry("user", "질문입니다", "u1"),
        entry("assistant", "답변입니다", "a1"),
    ]
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    result = document(entries, "제목", "Projects", when, "sess-123")

    # 머리글과 본문 사이 빈 줄 하나, 정확한 형식 그대로.
    assert "## 사용자\n\n질문입니다" in result
    assert "## Claude\n\n답변입니다" in result
    # 입력 순서(사용자 먼저)가 그대로 보존돼야 한다.
    assert result.index("## 사용자") < result.index("## Claude")


def test_document_preserves_script_tag_verbatim():
    """이 태스크의 핵심: 어떤 문자도 치환·이스케이프하지 않는다는 증명."""
    entries = [entry("user", "<script> 삽입 막는 법?", "u1")]
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    result = document(entries, "제목", "Projects", when, "sess-123")

    assert "<script> 삽입 막는 법?" in result


def test_document_does_not_truncate():
    long_text = "가" * 5000
    entries = [entry("user", long_text, "u1")]
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    result = document(entries, "제목", "Projects", when, "sess-123")

    assert long_text in result


def test_document_ignores_watermark():
    """document() 는 after_uuid 인자를 받지 않는다 — 항상 세션 전체를 담는다."""
    entries = [
        entry("user", "첫메시지", "u1"),
        entry("assistant", "둘째메시지", "a1"),
        entry("user", "셋째메시지", "u2"),
    ]
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    result = document(entries, "제목", "Projects", when, "sess-123")

    assert result.count("첫메시지") == 1
    assert result.count("둘째메시지") == 1
    assert result.count("셋째메시지") == 1


def test_document_empty_conversation():
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    result = document([], "제목", "Projects", when, "sess-123")

    # 예외 없이 머리말만 담긴 문자열 — --- 아래는 비어 있다.
    assert result.endswith("---\n")
    assert "## " not in result


def test_document_ends_with_single_newline():
    entries = [entry("user", "안녕하세요", "u1")]
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    result = document(entries, "제목", "Projects", when, "sess-123")

    assert result.endswith("\n")
    assert not result.endswith("\n\n")
