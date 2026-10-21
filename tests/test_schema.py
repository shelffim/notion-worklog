"""setup 이 테이블 URL 을 해석하고 열을 갖추는 로직."""
import pytest

from notion_worklog import schema

DB = "0123456789abcdef0123456789abcdef"
DB_DASHED = "01234567-89ab-cdef-0123-456789abcdef"
PAGE = "11112222333344445555666677778888"
PAGE_DASHED = "11112222-3333-4444-5555-666677778888"


@pytest.mark.parametrize("text", [
    "https://www.notion.so/myteam/{}?v=aaaabbbbccccddddeeeeffff00001111".format(DB),
    "https://www.notion.so/myteam/Work-Log-{}?v=aaaabbbbccccddddeeeeffff00001111".format(DB),
    "https://myteam.notion.site/{}".format(DB),
    DB,
    DB_DASHED,
    "  {}  ".format(DB),
])
def test_parse_id_accepts_urls_and_raw_ids(text):
    assert schema.parse_id(text) == DB_DASHED


@pytest.mark.parametrize("text", ["", "https://www.notion.so/", "not an id", "abc123"])
def test_parse_id_rejects_text_without_an_id(text):
    with pytest.raises(ValueError):
        schema.parse_id(text)


def database(db_id, title, properties):
    return {"object": "database", "id": db_id,
            "title": [{"plain_text": title, "text": {"content": title}}],
            "properties": {name: {"type": kind} for name, kind in properties.items()}}


class FakeApi:
    def __init__(self, databases=None, children=None):
        self.databases = databases or {}
        self.children = children or {}
        self.added = []

    def get_database(self, database_id):
        if database_id not in self.databases:
            raise RuntimeError("HTTP 400")       # 페이지 id 를 넣으면 Notion 은 400 을 낸다
        return self.databases[database_id]

    def list_children(self, page_id):
        if page_id not in self.children:
            raise RuntimeError("HTTP 404")
        return self.children[page_id]

    def add_properties(self, database_id, properties):
        self.added.append((database_id, properties))
        db = self.databases[database_id]
        for name, spec in properties.items():
            db["properties"][name] = {"type": next(iter(spec))}
        return db


def child_db(db_id, title):
    return {"id": db_id, "type": "child_database", "child_database": {"title": title}}


def test_resolve_returns_the_database_itself():
    api = FakeApi(databases={DB_DASHED: database(DB_DASHED, "기록", {"Name": "title"})})

    assert schema.resolve_database(api, DB_DASHED)["id"] == DB_DASHED


def test_resolve_finds_the_inline_database_inside_a_page():
    """인라인 테이블은 URL 에 자기 id 가 안 나온다 — 담고 있는 페이지 id 가 나온다."""
    api = FakeApi(
        databases={DB_DASHED: database(DB_DASHED, "기록", {"Name": "title"})},
        children={PAGE_DASHED: [{"id": "x", "type": "paragraph"}, child_db(DB_DASHED, "기록")]},
    )

    assert schema.resolve_database(api, PAGE_DASHED)["id"] == DB_DASHED


def test_resolve_reports_every_candidate_when_a_page_has_several_tables():
    api = FakeApi(children={PAGE_DASHED: [child_db("db-a", "A"), child_db("db-b", "B")]})

    with pytest.raises(schema.SeveralDatabases) as info:
        schema.resolve_database(api, PAGE_DASHED)

    assert info.value.candidates == [("db-a", "A"), ("db-b", "B")]


def test_resolve_explains_sharing_when_nothing_is_reachable():
    with pytest.raises(LookupError) as info:
        schema.resolve_database(FakeApi(), DB_DASHED)

    assert "연결" in str(info.value)


def test_ensure_columns_reuses_existing_korean_columns_and_adds_only_the_missing():
    db = database(DB_DASHED, "기록", {
        "작업": "title", "날짜": "date", "프로젝트": "select", "세션ID": "rich_text",
    })
    api = FakeApi(databases={DB_DASHED: db})

    columns, created = schema.ensure_columns(api, db, "ko")

    assert columns == {"title": "작업", "date": "날짜", "project": "프로젝트",
                       "session": "세션ID", "agent": "에이전트"}
    assert created == ["에이전트"]
    assert api.added == [(DB_DASHED, {"에이전트": {"select": {}}})]


def test_ensure_columns_reuses_columns_named_in_the_other_language():
    db = database(DB_DASHED, "Log", {"Name": "title", "Date": "date"})
    api = FakeApi(databases={DB_DASHED: db})

    columns, created = schema.ensure_columns(api, db, "ko")

    assert columns["title"] == "Name"            # 제목 열은 이름이 무엇이든 그대로 쓴다
    assert columns["date"] == "Date"             # 다른 언어 이름이어도 형식이 맞으면 재사용
    assert created == ["프로젝트", "세션ID", "에이전트"]


def test_ensure_columns_creates_english_names_for_a_bare_table():
    db = database(DB_DASHED, "Log", {"Name": "title"})
    api = FakeApi(databases={DB_DASHED: db})

    columns, created = schema.ensure_columns(api, db, "en")

    assert columns == {"title": "Name", "date": "Date", "project": "Project",
                       "session": "Session ID", "agent": "Agent"}
    assert created == ["Date", "Project", "Session ID", "Agent"]


def test_ensure_columns_does_nothing_when_everything_exists():
    db = database(DB_DASHED, "Log", {"Name": "title", "Date": "date", "Project": "select",
                                     "Session ID": "rich_text", "Agent": "select"})
    api = FakeApi(databases={DB_DASHED: db})

    _, created = schema.ensure_columns(api, db, "en")

    assert created == []
    assert api.added == []


def test_ensure_columns_refuses_a_same_named_column_of_another_type():
    """같은 이름의 다른 형식 열을 덮어쓰면 사용자의 데이터가 망가진다."""
    db = database(DB_DASHED, "Log", {"Name": "title", "Date": "rich_text"})
    api = FakeApi(databases={DB_DASHED: db})

    with pytest.raises(ValueError) as info:
        schema.ensure_columns(api, db, "en")

    assert "Date" in str(info.value)
    assert api.added == []
