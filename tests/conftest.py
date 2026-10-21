"""테스트 공통 설정.

요청 간격(notion.MIN_REQUEST_INTERVAL)은 진짜 time.sleep 을 부른다. 가짜 세션을
쓰는 단위 테스트까지 그 대가를 치르면 전체 실행이 0.3초에서 4초로 늘어나므로
테스트 동안에는 0 으로 내린다.

간격 자체의 동작은 tests/test_notion.py 의 pacing 테스트들이 가짜 시계와 가짜
sleep 으로 직접 검증하고, 프로덕션 기본값은 production_pace 픽스처로 확인한다.
"""
import pytest

from notion_worklog import notion

# autouse 픽스처가 상수를 낮추기 전, import 시점의 진짜 기본값을 붙잡아 둔다.
PRODUCTION_PACE = notion.MIN_REQUEST_INTERVAL


@pytest.fixture(autouse=True)
def no_real_pacing(monkeypatch):
    monkeypatch.setattr(notion, "MIN_REQUEST_INTERVAL", 0)


@pytest.fixture
def production_pace():
    return PRODUCTION_PACE


@pytest.fixture(autouse=True)
def fixed_timezone(monkeypatch):
    """제목·날짜가 기계의 로컬 시간대에 따라 달라지지 않게 KST 로 고정한다."""
    from datetime import timedelta, timezone

    from notion_worklog import config

    monkeypatch.setattr(config, "TZ", timezone(timedelta(hours=9), "KST"))
