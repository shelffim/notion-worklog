import json
import os
import stat

from notion_worklog import config


def test_load_config_reads_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"database_id": "db-1", "lang": "ko"}), encoding="utf-8")

    assert config.load_config(path) == {"database_id": "db-1", "lang": "ko"}


def test_load_config_is_empty_when_missing_broken_or_not_an_object(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{이건 JSON 이 아님", encoding="utf-8")
    listed = tmp_path / "list.json"
    listed.write_text("[1, 2]", encoding="utf-8")

    assert config.load_config(tmp_path / "nope.json") == {}
    assert config.load_config(broken) == {}
    assert config.load_config(listed) == {}


def test_token_env_var_wins_over_file(tmp_path, monkeypatch):
    path = tmp_path / "token"
    path.write_text("from-file\n", encoding="utf-8")

    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    assert config.load_token(path) == "from-file"

    monkeypatch.setenv("NOTION_TOKEN", " from-env ")
    assert config.load_token(path) == "from-env"


def test_token_is_empty_when_nothing_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    assert config.load_token(tmp_path / "nope") == ""


def test_secrets_are_written_owner_only(tmp_path):
    home = tmp_path / "home"
    config.save_token("secret-value", home / "token")
    config.save_config({"database_id": "db-1"}, home / "config.json")

    for name in ("token", "config.json"):
        assert stat.S_IMODE(os.stat(str(home / name)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(str(home)).st_mode) == 0o700
    assert config.load_token(home / "token") == "secret-value"


def test_log_error_accepts_non_string_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")

    config.log_error(ValueError("boom"))
    config.log_error(123)

    assert "boom" in (tmp_path / "error.log").read_text(encoding="utf-8")


def test_log_error_swallows_non_oserror_failure(monkeypatch):
    # OSError 가 아닌 실패도 삼켜야 한다. open() 이 TypeError 를 내는 입력을 쓴다.
    # (수정 전의 `except OSError` 로는 이 테스트가 실패한다)
    monkeypatch.setattr(config, "ERROR_LOG", object())

    config.log_error("아무거나")  # 예외가 새어 나오면 실패


def test_log_error_swallows_write_failure(tmp_path, monkeypatch):
    # 쓸 수 없는 경로(OSError 계열)도 삼켜야 한다
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "없는디렉터리" / "error.log")

    config.log_error("아무거나")
