"""사람에게 보이는 문자열. setup 에서 고른 언어(ko/en)로 고정된다.

언어를 세션 도중에 바꾸면 안 된다 — 본문 블록과 마커 문구가 바뀌어, 결과를 모르는
append 를 대조할 때(sync._landed_blocks) 페이지와 맞지 않게 된다.
"""

TEXT = {
    "ko": {
        "user": "🧑 나",
        "doc_user": "사용자",
        "session": "세션",
        "unknown_time": "시각 미상",
        "doc_project": "프로젝트",
        "doc_started": "시작",
        "doc_session": "세션ID",
        "doc_agent": "에이전트",
        "doc_note": (
            "AI 코딩 에이전트 세션의 대화 원문입니다. Notion 페이지 본문에서는 길이 제한으로\n"
            "잘리거나 API 앞단 필터에 거부돼 마커로 대체된 메시지가 있을 수 있지만,\n"
            "이 파일에는 그 메시지들이 원문 그대로 들어 있습니다.\n"
            "\n"
            "페이지 본문과 같은 기준으로 추려진 사람/에이전트의 대화만 담깁니다 —\n"
            "도구 호출과 그 결과, 슬래시 커맨드 찌꺼기, 시스템 알림은 양쪽 모두에서\n"
            "빠집니다."
        ),
        "marker": (
            "⚠️ 이 메시지는 Notion API 앞단 보안 필터가 거부해 본문에 실을 수 "
            "없었습니다. 원문은 이 페이지에 첨부된 session.md 에 있습니다."
        ),
        "columns": {
            "date": "날짜",
            "project": "프로젝트",
            "session": "세션ID",
            "agent": "에이전트",
        },
    },
    "en": {
        "user": "🧑 User",
        "doc_user": "User",
        "session": "session",
        "unknown_time": "unknown time",
        "doc_project": "Project",
        "doc_started": "Started",
        "doc_session": "Session ID",
        "doc_agent": "Agent",
        "doc_note": (
            "Full transcript of an AI coding agent session. In the Notion page body some\n"
            "messages may be truncated by length limits or replaced by a marker when the\n"
            "API's front filter refused them; this file keeps them verbatim.\n"
            "\n"
            "It holds the same human/agent conversation as the page body — tool calls\n"
            "and their results, slash-command residue and system notices are left out\n"
            "of both."
        ),
        "marker": (
            "⚠️ The Notion API's front-end security filter refused this message, so it "
            "could not be placed in the page body. The original is in the attached session.md."
        ),
        "columns": {
            "date": "Date",
            "project": "Project",
            "session": "Session ID",
            "agent": "Agent",
        },
    },
}

LANGS = tuple(TEXT)


def text(lang):
    """lang 에 맞는 문자열 묶음. 모르는 언어면 한국어."""
    return TEXT.get(lang) or TEXT["ko"]
