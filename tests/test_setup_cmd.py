"""notion-worklog setup / uninstall — 대화형 흐름을 가짜 입력과 가짜 Notion 으로 검증한다."""
import json

import pytest

from notion_worklog import config, setup_cmd

DB_URL = "https://www.notion.so/team/Log-0123456789abcdef0123456789abcdef?v=aaaabbbbccccddddeeeeffff00001111"
DB_ID = "01234567-89ab-cdef-0123-456789abcdef"
PAGE_ID = "11112222-3333-4444-5555-666677778888"


class FakeApi:
    def __init__(self, token, valid=True, databases=None, children=None):
        self.token, self.valid = token, valid
        self.databases = databases if databases is not None else {
            DB_ID: {"id": DB_ID, "title": [], "properties": {"Name": {"type": "title"}}},
        }
        self.children = children or {}
        self.added = []

    def me(self):
        if not self.valid:
            raise RuntimeError("HTTP 401")
        return {"object": "user"}

    def get_database(self, database_id):
        if database_id not in self.databases:
            raise RuntimeError("HTTP 400")
        return self.databases[database_id]

    def list_children(self, page_id):
        if page_id not in self.children:
            raise RuntimeError("HTTP 404")
        return self.children[page_id]

    def add_properties(self, database_id, properties):
        self.added.append(properties)
        return {}


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / ".notion-worklog"
    monkeypatch.setattr(config, "CONFIG_PATH", data / "config.json")
    monkeypatch.setattr(config, "TOKEN_PATH", data / "token")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    (tmp_path / ".claude").mkdir()
    return tmp_path


def no_question(prompt=""):
    raise AssertionError("묻지 말아야 할 것을 물었다: {}".format(prompt))


def run_setup(home, argv, api=None, ask=no_question, secret="tok-123", out=None):
    api = api or FakeApi(secret)
    return setup_cmd.main(
        ["setup"] + argv,
        make_api=lambda token: api, home=home, ask=ask,
        ask_secret=lambda prompt="": secret, out=(out or (lambda *a: None)),
    ), api


def test_setup_saves_config_token_and_registers_hooks(home):
    code, api = run_setup(home, ["--database", DB_URL, "--lang", "en"])

    assert code == 0
    saved = config.load_config()
    assert saved["database_id"] == DB_ID
    assert saved["lang"] == "en"
    assert saved["columns"] == {"title": "Name", "date": "Date", "project": "Project",
                                "session": "Session ID", "agent": "Agent"}
    assert config.load_token() == "tok-123"
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "notion_worklog hook --agent claude" in json.dumps(settings)
    assert not (home / ".codex").exists()        # 설치 안 된 에이전트는 건드리지 않는다


def test_setup_asks_for_the_table_when_not_given(home):
    answers = iter([DB_URL])

    code, _ = run_setup(home, ["--lang", "ko"], ask=lambda prompt="": next(answers))

    assert code == 0
    assert config.load_config()["database_id"] == DB_ID


def test_setup_lets_you_pick_when_a_page_holds_several_tables(home):
    db_b = "aaaabbbb-cccc-dddd-eeee-ffff00002222"
    api = FakeApi("tok-123", databases={
        db_b: {"id": db_b, "title": [], "properties": {"Name": {"type": "title"}}},
    }, children={PAGE_ID: [
        {"id": "db-a", "type": "child_database", "child_database": {"title": "A"}},
        {"id": db_b, "type": "child_database", "child_database": {"title": "B"}},
    ]})

    code, _ = run_setup(home, ["--database", PAGE_ID, "--lang", "en", "--no-hooks"],
                        api=api, ask=lambda prompt="": "2")

    assert code == 0
    assert config.load_config()["database_id"] == db_b


def test_setup_with_a_bad_token_saves_nothing(home):
    code, _ = run_setup(home, ["--database", DB_URL, "--lang", "en"],
                        api=FakeApi("bad", valid=False))

    assert code != 0
    assert config.load_config() == {}
    assert config.load_token() == ""
    assert not (home / ".claude" / "settings.json").exists()


def test_setup_with_an_unreachable_table_saves_nothing(home):
    code, _ = run_setup(home, ["--database", DB_URL, "--lang", "en"],
                        api=FakeApi("tok-123", databases={}))

    assert code != 0
    assert config.load_config() == {}


def test_setup_never_prints_the_token(home):
    printed = []

    run_setup(home, ["--database", DB_URL, "--lang", "en"],
              out=lambda *a: printed.append(" ".join(map(str, a))))

    assert printed
    assert not any("tok-123" in line for line in printed)


def test_uninstall_removes_hooks_but_keeps_settings(home):
    run_setup(home, ["--database", DB_URL, "--lang", "en"])

    code = setup_cmd.main(["uninstall"], home=home, out=lambda *a: None)

    assert code == 0
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "notion_worklog" not in json.dumps(settings)
    assert config.load_config()["database_id"] == DB_ID
