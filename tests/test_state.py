import json
import os
import time

from notion_worklog import state


def test_load_missing_file_returns_empty(tmp_path):
    assert state.load(tmp_path / "nope.json") == {}


def test_load_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{망가진 JSON", encoding="utf-8")

    assert state.load(path) == {}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    data = state.put({}, "sess-1", "page-1", "uuid-1")

    state.save(path, data)

    assert state.get(state.load(path), "sess-1")["page_id"] == "page-1"


def test_save_leaves_no_temp_files(tmp_path):
    path = tmp_path / "state.json"
    state.save(path, {"a": {"page_id": "p", "last_uuid": "u", "updated": 0}})

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


def test_save_replaces_previous_contents(tmp_path):
    path = tmp_path / "state.json"
    state.save(path, state.put({}, "a", "page-a", "uuid-a"))
    state.save(path, state.put({}, "b", "page-b", "uuid-b"))

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert set(loaded) == {"b"}


def test_get_returns_none_for_unknown_session():
    assert state.get({}, "sess-x") is None


def test_put_records_all_three_fields():
    data = state.put({}, "sess-1", "page-1", "uuid-1")
    entry = data["sess-1"]

    assert entry["page_id"] == "page-1"
    assert entry["last_uuid"] == "uuid-1"
    assert entry["updated"] > 0


def test_prune_drops_entries_older_than_ttl():
    now = 1_000_000_000
    data = {
        "old": {"page_id": "p1", "last_uuid": "u1", "updated": now - state.TTL_SECONDS - 1},
        "new": {"page_id": "p2", "last_uuid": "u2", "updated": now - 10},
    }

    assert set(state.prune(data, now=now)) == {"new"}


def test_prune_drops_malformed_entries():
    assert state.prune({"bad": "문자열"}, now=0) == {}


def test_prune_drops_entry_with_non_numeric_updated():
    # updated 가 숫자가 아닌 항목은 크래시가 아니라 제외로 처리해야 한다
    data = {
        "bad-str":  {"page_id": "p", "last_uuid": "u", "updated": "문자열"},
        "bad-none": {"page_id": "p", "last_uuid": "u", "updated": None},
        "good":     {"page_id": "p", "last_uuid": "u", "updated": 999},
    }

    assert set(state.prune(data, now=1000)) == {"good"}


def test_prune_drops_entry_missing_updated():
    data = {"no-field": {"page_id": "p", "last_uuid": "u"}}

    assert state.prune(data, now=1000) == {}


def test_load_returns_empty_on_deeply_nested_json(tmp_path):
    # json.load 는 깊은 중첩에서 RecursionError 를 낸다 — ValueError 가 아니다
    path = tmp_path / "state.json"
    path.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")

    assert state.load(path) == {}


def test_save_uses_atomic_replace(tmp_path, monkeypatch):
    # 임시 파일 + os.replace 경로를 실제로 지나는지 검증한다.
    # open(path,'w') 로 직접 쓰는 구현이라면 이 테스트가 실패한다.
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (calls.append((src, dst)), real_replace(src, dst))[1])

    path = tmp_path / "state.json"
    state.save(path, {"a": {"page_id": "p", "last_uuid": "u", "updated": 1}})

    assert len(calls) == 1
    assert calls[0][1] == str(path)


# --- 잘린 항목 걸러내기 -----------------------------------------

def test_prune_drops_entry_missing_page_id():
    # updated 만 보던 시절에는 이런 항목이 통과해 sync 의 cached["page_id"] 에서
    # 매 턴 KeyError 를 냈다. 훅은 무음이라 이 조용한 반복 실패가 가장 비싸다.
    data = {"truncated": {"last_uuid": "u", "updated": 999}}

    assert state.prune(data, now=1000) == {}


def test_prune_drops_entry_missing_last_uuid():
    data = {"truncated": {"page_id": "p", "updated": 999}}

    assert state.prune(data, now=1000) == {}


def test_prune_drops_entry_with_wrong_types():
    data = {
        "page-id-none": {"page_id": None, "last_uuid": "u", "updated": 999},
        "page-id-empty": {"page_id": "", "last_uuid": "u", "updated": 999},
        "page-id-number": {"page_id": 7, "last_uuid": "u", "updated": 999},
        "uuid-number": {"page_id": "p", "last_uuid": 7, "updated": 999},
    }

    assert state.prune(data, now=1000) == {}


def test_prune_keeps_entry_whose_last_uuid_is_null():
    # 페이지를 막 만들고 본문이 아직 빈 상태의 정상 항목이다. 이걸 버리면
    # 다음 발화가 캐시 미스 → find_page → clear_body 로 되돌아간다.
    data = {"fresh": {"page_id": "p", "last_uuid": None, "updated": 999}}

    assert set(state.prune(data, now=1000)) == {"fresh"}


def test_pruned_entries_are_safe_to_index_directly():
    # prune 의 계약: 통과한 항목은 sync 가 그대로 인덱싱해도 된다
    data = {
        "ok": {"page_id": "p", "last_uuid": None, "updated": 999},
        "bad": {"updated": 999},
    }

    for entry in state.prune(data, now=1000).values():
        assert entry["page_id"]
        entry["last_uuid"]  # KeyError 가 나면 계약 위반


# --- 첨부 블록 id ----------------------------------------------

def test_put_file_block_sets_id():
    data = state.put({}, "sess-1", "page-1", "uuid-1")

    data = state.put_file_block(data, "sess-1", "block-1")

    assert state.get(data, "sess-1")["file_block_id"] == "block-1"


def test_put_file_block_missing_session_is_noop():
    data = {}

    result = state.put_file_block(data, "sess-x", "block-1")

    assert result == {}


def test_put_preserves_file_block_id():
    """이 태스크에서 가장 중요한 테스트다.

    put_file_block 뒤에 기존 put() 을 불러도(매 배치마다 일어난다)
    file_block_id 가 지워지면 안 된다.
    """
    data = state.put({}, "sess-1", "page-1", "uuid-1")
    data = state.put_file_block(data, "sess-1", "block-1")

    data = state.put(data, "sess-1", "page-1", "uuid-2")

    entry = state.get(data, "sess-1")
    assert entry["file_block_id"] == "block-1"
    assert entry["last_uuid"] == "uuid-2"


def test_is_valid_accepts_entry_without_file_block_id():
    # 디스크에 이미 있는 기존 형식 항목에는 file_block_id 가 없다
    entry = {"page_id": "p", "last_uuid": "u", "updated": 999}

    assert state.is_valid(entry)


def test_prune_keeps_entry_without_file_block_id():
    data = {"legacy": {"page_id": "p", "last_uuid": "u", "updated": 999}}

    assert set(state.prune(data, now=1000)) == {"legacy"}


# --- 첨부 키 검증 -----------------------------------------

def _legacy_entry():
    """디스크에 실제로 있는 형식 — 세 키뿐이다."""
    return {"page_id": "p", "last_uuid": "u", "updated": time.time()}


def test_is_valid_accepts_legacy_entry_without_attachment_keys():
    """하위 호환은 하드 요구사항이다. 새 키의 존재를 요구하면 기존 세션이 전부
    무효가 되어 캐시 미스 → clear_body 전체 재작성으로 떨어진다."""
    assert state.is_valid(_legacy_entry())


def test_is_valid_rejects_broken_file_block_id():
    """깨진 file_block_id 는 '이미 첨부돼 있다'로 읽혀 갱신을 영원히 건너뛴다 —
    그러면 페이지의 마커가 없는 첨부를 가리킨다."""
    for bad in (123, "", None, [], {}):
        entry = _legacy_entry()
        entry["file_block_id"] = bad
        assert not state.is_valid(entry), repr(bad)


def test_is_valid_rejects_non_numeric_attachment_attempted():
    """숫자가 아니면 재업로드 간격 계산이 TypeError 를 낸다."""
    entry = _legacy_entry()
    entry["attachment_attempted"] = "방금"
    assert not state.is_valid(entry)


def test_is_valid_accepts_well_formed_attachment_keys():
    entry = _legacy_entry()
    entry.update({"file_block_id": "blk-1", "attachment_attempted": 1.0,
                  "attachment_stale": False})
    assert state.is_valid(entry)


def test_prune_drops_entry_with_broken_attachment_key():
    now = time.time()
    good = _legacy_entry()
    bad = _legacy_entry()
    bad["file_block_id"] = 42
    result = state.prune({"ok": good, "broken": bad}, now=now)
    assert "ok" in result and "broken" not in result


def test_put_file_block_clears_stale():
    cache = {"s": _legacy_entry()}
    state.mark_attachment_stale(cache, "s")
    assert cache["s"]["attachment_stale"] is True

    state.put_file_block(cache, "s", "blk-9")
    assert cache["s"]["file_block_id"] == "blk-9"
    assert cache["s"]["attachment_stale"] is False


def test_mark_attachment_attempt_stamps_regardless_of_outcome():
    """시도 시각은 성공/실패를 가리지 않는다 — 성공 시각만 남기면 실패가
    반복되는 동안 간격이 늘 지나 있어 매 턴 재업로드하게 된다."""
    cache = {"s": _legacy_entry()}
    state.mark_attachment_attempt(cache, "s", when=99.0)
    assert cache["s"]["attachment_attempted"] == 99.0


def test_mark_attachment_attempt_on_missing_session_is_noop():
    cache = {}
    state.mark_attachment_attempt(cache, "없는세션")
    assert cache == {}


def test_mark_attachment_stale_on_missing_session_is_noop():
    cache = {}
    state.mark_attachment_stale(cache, "없는세션")
    assert cache == {}


def test_is_valid_rejects_non_bool_attachment_stale():
    """숫자나 문자열이 들어오면 bool() 로 참이 되어 의무가 영원히 안 풀린다."""
    for bad in ("yes", 1, 0, []):
        entry = _legacy_entry()
        entry["attachment_stale"] = bad
        assert not state.is_valid(entry), repr(bad)
