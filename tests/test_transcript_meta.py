from datetime import datetime, timedelta, timezone
from pathlib import Path

from notion_worklog.agents import claude as transcript

KST = timezone(timedelta(hours=9))
FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return transcript.read_entries(FIXTURES / name)


def test_read_entries_parses_all_lines():
    entries = load("with_aititle.jsonl")
    assert len(entries) > 10
    assert all(isinstance(e, dict) for e in entries)


def test_read_entries_skips_broken_lines(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"a":1}\n이건 JSON이 아님\n{"b":2}\n', encoding="utf-8")

    assert transcript.read_entries(path) == [{"a": 1}, {"b": 2}]


def test_started_at_converts_utc_to_kst():
    when = transcript.started_at(load("with_aititle.jsonl"))
    # 원본 첫 timestamp 는 2026-09-01T12:37:13.245Z → KST 21:37
    assert when.hour == 21
    assert when.strftime("%Y-%m-%d") == "2026-09-01"


def test_project_name_is_last_path_segment():
    assert transcript.project_name(load("with_aititle.jsonl")) == "Projects"


def test_project_name_falls_back_to_cwd_argument():
    assert transcript.project_name([], "/a/b/my-app") == "my-app"


def test_title_prefers_ai_title():
    entries = load("with_aititle.jsonl")
    when = transcript.started_at(entries)
    assert transcript.title(entries, "Projects", when) == "Skill-creator 플러그인 설치"


def test_title_uses_last_prompt_when_no_ai_title():
    # 폴백 2순위. 실제 픽스처에는 이 조합이 없어 합성 입력으로 검증한다.
    entries = [
        {"type": "last-prompt", "lastPrompt": "버그 고쳐줘\n두 번째 줄"},
    ]
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    assert transcript.title(entries, "P", when) == "버그 고쳐줘"


def test_title_uses_first_real_prompt_when_no_ai_title_or_last_prompt():
    # 폴백 3순위. 노이즈 엔트리를 건너뛰고 진짜 프롬프트를 골라야 한다.
    entries = [
        {"type": "user", "uuid": "u1", "isMeta": True,
         "message": {"content": "메타는 무시"}},
        {"type": "user", "uuid": "u2",
         "message": {"content": "<command-name>/plugin</command-name>"}},
        {"type": "user", "uuid": "u3",
         "message": {"content": "진짜 요청입니다\n둘째 줄"}},
    ]
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)

    assert transcript.title(entries, "P", when) == "진짜 요청입니다"


def test_title_final_fallback_when_no_candidates_at_all():
    # no_aititle 은 aiTitle·lastPrompt·진짜 프롬프트가 전부 없다 (실측 확인).
    entries = load("no_aititle.jsonl")
    when = transcript.started_at(entries)

    assert transcript.title(entries, "Projects", when) == "Projects 세션 " + when.strftime("%H:%M")


def test_title_final_fallback_uses_project_and_time():
    # no_prompt 는 슬래시 커맨드뿐이라 제목 후보가 하나도 없다
    entries = load("no_prompt.jsonl")
    when = transcript.started_at(entries)
    result = transcript.title(entries, "Projects", when)

    assert result == "Projects 세션 " + when.strftime("%H:%M")


def test_title_is_capped_at_100_chars():
    entries = [{"type": "ai-title", "aiTitle": "가" * 300}]
    result = transcript.title(entries, "P", datetime.now(KST))
    assert len(result) == 100


def test_title_skips_whitespace_only_ai_title():
    entries = [
        {"type": "ai-title", "aiTitle": "   "},
        {"type": "last-prompt", "lastPrompt": "두 번째 후보"},
    ]
    when = datetime(2026, 9, 3, 10, 0, tzinfo=KST)

    assert transcript.title(entries, "P", when) == "두 번째 후보"


def test_title_skips_whitespace_only_last_prompt():
    entries = [
        {"type": "last-prompt", "lastPrompt": "  \n  "},
        {"type": "user", "uuid": "u1", "message": {"content": "세 번째 후보"}},
    ]
    when = datetime(2026, 9, 3, 10, 0, tzinfo=KST)

    assert transcript.title(entries, "P", when) == "세 번째 후보"


def test_title_reaches_final_fallback_when_every_candidate_is_blank():
    entries = [
        {"type": "ai-title", "aiTitle": ""},
        {"type": "last-prompt", "lastPrompt": "   "},
    ]
    when = datetime(2026, 9, 3, 10, 0, tzinfo=KST)

    assert transcript.title(entries, "P", when) == "P 세션 10:00"


def test_started_at_skips_non_string_timestamp():
    entries = [
        {"timestamp": 1788355268},
        {"timestamp": "2026-09-01T12:00:00Z"},
    ]

    assert transcript.started_at(entries).hour == 21


def test_started_at_returns_none_when_no_usable_timestamp():
    assert transcript.started_at([{"timestamp": 123}, {"timestamp": None}]) is None
