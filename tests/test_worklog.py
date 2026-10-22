import copy
import itertools
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import requests

from notion_worklog import config
from notion_worklog import notion
from notion_worklog import state
from notion_worklog.agents import claude as transcript
from notion_worklog import sync as worklog

FIXTURES = Path(__file__).parent / "fixtures"

# attach_file 이 돌려주는 블록 id 를 프로세스 전체에서 유일하게 만든다.
# RecordingApi 인스턴스별로 1부터 새로 세면 서로 다른 인스턴스(=서로 다른 sync
# 호출)가 우연히 같은 id 를 돌려줘 "새 id 로 교체됐다"는 assert 가 실제로는
# 아무것도 검증하지 못하는 사고를 만든다.
_block_ids = itertools.count(1)


class RecordingApi:
    def __init__(self, existing_page=None):
        self.existing_page = existing_page
        self.created = []
        self.fallbacks = []
        self.updated = []
        self.appended = []
        self.cleared = []
        self.uploaded = []
        self.attached = []
        self.deleted = []
        self.call_order = []
        self.refused = 0

    def find_page(self, session_id, prop_name):
        return self.existing_page

    def create_page(self, props, fallback_props=None):
        self.created.append(props)
        self.fallbacks.append(fallback_props)
        return "page-new"

    def update_props(self, page_id, props, fallback_props=None):
        self.updated.append((page_id, props))
        self.fallbacks.append(fallback_props)

    def append_blocks(self, page_id, blocks):
        self.appended.append((page_id, blocks))

    def clear_body(self, page_id):
        self.cleared.append(page_id)

    def upload_file(self, filename, data, content_type="text/markdown"):
        self.uploaded.append((filename, data, content_type))
        self.call_order.append("upload")
        return "upload-{}".format(next(_block_ids))

    def attach_file(self, page_id, file_upload_id, name="session.md"):
        block_id = "block-{}".format(next(_block_ids))
        self.attached.append((page_id, file_upload_id, name, block_id))
        self.call_order.append("attach:{}".format(block_id))
        return block_id

    def delete_block(self, block_id):
        self.deleted.append(block_id)
        self.call_order.append("delete:{}".format(block_id))


def payload(name="with_aititle.jsonl", session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": str(FIXTURES / name),
        "cwd": "/Users/me/Projects",
        "hook_event_name": "Stop",
    }


def test_creates_page_when_cache_and_search_both_miss():
    api = RecordingApi(existing_page=None)

    worklog.sync(payload(), api, {})

    assert len(api.created) == 1
    assert api.created[0]["작업"]["title"][0]["text"]["content"] == "Skill-creator 플러그인 설치"
    assert len(api.appended) == 1


def test_records_page_id_and_watermark_in_cache():
    api = RecordingApi(existing_page=None)

    cache = worklog.sync(payload(), api, {})

    entry = state.get(cache, "sess-1")
    assert entry["page_id"] == "page-new"
    assert entry["last_uuid"]


def test_cache_hit_updates_and_appends_only_new():
    api = RecordingApi()
    first = worklog.sync(payload(), api, {})
    api2 = RecordingApi()

    worklog.sync(payload(), api2, first)

    assert api2.created == []
    assert len(api2.updated) == 1
    # 워터마크 이후 새 메시지가 없으므로 본문은 붙이지 않는다
    assert api2.appended == []


def test_cache_miss_with_existing_page_clears_before_rewrite():
    api = RecordingApi(existing_page="page-old")

    worklog.sync(payload(), api, {})

    assert api.cleared == ["page-old"]
    assert api.created == []
    assert len(api.appended) == 1


def test_missing_transcript_does_nothing():
    api = RecordingApi()
    broken = payload()
    broken["transcript_path"] = "/없는/경로.jsonl"

    worklog.sync(broken, api, {})

    assert api.created == []
    assert api.appended == []


def test_sync_appends_only_new_turns_as_transcript_grows(tmp_path):
    """트랜스크립트가 자라는 동안 Stop 이 두 번 터지는 실제 시나리오."""
    path = tmp_path / "session.jsonl"

    def entry(uuid, kind, text):
        content = text if kind == "user" else [{"type": "text", "text": text}]
        return {
            "type": kind,
            "uuid": uuid,
            "timestamp": "2026-09-03T01:00:00.000Z",
            "cwd": "/Users/me/Projects",
            "message": {"content": content},
        }

    def write(entries):
        path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8",
        )

    first = [entry("u1", "user", "첫 요청"), entry("a1", "assistant", "첫 응답")]
    write(first)
    payload = {
        "session_id": "grow-1",
        "transcript_path": str(path),
        "cwd": "/Users/me/Projects",
        "hook_event_name": "Stop",
    }

    api1 = RecordingApi(existing_page=None)
    cache = worklog.sync(payload, api1, {})
    assert len(api1.appended) == 1
    first_block_count = len(api1.appended[0][1])

    # 두 번째 Stop: 턴이 하나 더 늘었다
    write(first + [entry("u2", "user", "둘째 요청"), entry("a2", "assistant", "둘째 응답")])

    api2 = RecordingApi()
    worklog.sync(payload, api2, cache)

    assert len(api2.appended) == 1
    blocks = api2.appended[0][1]
    texts = [rt["text"]["content"] for b in blocks for rt in b[b["type"]]["rich_text"]]
    assert "둘째 요청" in texts
    assert "둘째 응답" in texts
    assert "첫 요청" not in texts   # 이미 기록한 턴을 다시 붙이면 안 된다
    assert len(blocks) == first_block_count


# ---------------------------------------------------------------------------
# 2026-09-03 사고 재현: 큰 세션의 두 번째 배치가 403 으로 죽고, 워터마크를 저장하지
# 못한 채 예외가 sync() 를 빠져나가, 다음 발화가 다시 clear_body + 전체 재작성으로
# 떨어져 턴당 100회 넘는 요청을 영원히 반복했다.
# ---------------------------------------------------------------------------

def write_transcript(path, count, text="내용"):
    """메시지 count 개짜리 트랜스크립트. 메시지 하나당 블록 2개(heading+paragraph)."""
    lines = []
    for i in range(count):
        kind = "user" if i % 2 == 0 else "assistant"
        body = "{} {}".format(text, i)
        content = body if kind == "user" else [{"type": "text", "text": body}]
        lines.append(json.dumps({
            "type": kind,
            "uuid": "u{:03d}".format(i),
            "timestamp": "2026-09-03T01:00:00.000Z",
            "cwd": "/Users/me/Projects",
            "message": {"content": content},
        }, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def big_payload(path, session_id="big-1"):
    return {
        "session_id": session_id,
        "transcript_path": str(path),
        "cwd": "/Users/me/Projects",
        "hook_event_name": "Stop",
    }


class FlakyApi(RecordingApi):
    """N 번째 append 에서 터진다 — 사고의 403 자리."""

    def __init__(self, fail_on_append, existing_page=None):
        super().__init__(existing_page=existing_page)
        self.fail_on_append = fail_on_append

    def append_blocks(self, page_id, blocks):
        if len(self.appended) + 1 == self.fail_on_append:
            raise RuntimeError("HTTP 403")
        super().append_blocks(page_id, blocks)


def block_texts(blocks):
    return [rt["text"]["content"] for b in blocks for rt in b[b["type"]]["rich_text"]]


def test_partial_append_failure_keeps_progress_and_next_sync_resumes(tmp_path):
    """사고 전체에 대한 회귀 방어.

    (a) 두 번째 배치가 죽어도 첫 배치의 워터마크는 이미 저장돼 있고
    (b) 다음 발화는 캐시 히트로 나머지만 붙이며 clear_body 를 부르지 않는다.
    """
    path = tmp_path / "big.jsonl"
    write_transcript(path, 120)          # 240블록 → 배치 3개
    load = big_payload(path)

    everything = transcript.conversation(transcript.read_entries(path))
    assert len(everything) == 120

    saves = []

    def save(cache):
        saves.append(copy.deepcopy(cache))

    api1 = FlakyApi(fail_on_append=2)
    try:
        worklog.sync(load, api1, {}, save=save)
    except RuntimeError:
        pass
    else:
        raise AssertionError("두 번째 배치의 403 이 sync 밖으로 나오지 않았다")

    # 첫 배치만 나갔다
    assert len(api1.appended) == 1
    done = len(api1.appended[0][1]) // 2          # 메시지당 블록 2개
    assert 0 < done < 120

    # (a) 실패했는데도 진행 상황이 남아 있다
    persisted = state.get(saves[-1], "big-1")
    assert persisted["page_id"] == "page-new"
    assert persisted["last_uuid"] == everything[done - 1]["uuid"]

    # (b) 다음 발화: 캐시 히트로 나머지만 이어붙인다. append 가 실패한 뒤라
    # 페이지를 대조하므로, 첫 배치가 올라가 있는 페이지를 흉내낸다.
    api2 = PageApi(page=[b for _, blocks in api1.appended for b in blocks])
    worklog.sync(load, api2, state.prune(copy.deepcopy(saves[-1])), save=save)

    assert api2.cleared == []                      # 전체 재작성 경로로 떨어지지 않았다
    assert api2.created == []
    resumed = [b for _, blocks in api2.appended for b in blocks]
    assert len(resumed) == (120 - done) * 2        # 정확히 나머지만
    texts = block_texts(resumed)
    assert "내용 0" not in texts                    # 이미 기록한 턴은 다시 안 붙인다
    assert "내용 {}".format(done) in texts          # 끊긴 바로 다음 턴부터
    assert "내용 119" in texts

    # 그리고 결국 수렴한다 — 세 번째 발화는 아무것도 하지 않는다
    api3 = RecordingApi()
    worklog.sync(load, api3, state.prune(copy.deepcopy(saves[-1])), save=save)
    assert api3.appended == []


def test_every_batch_saves_its_own_watermark(tmp_path):
    path = tmp_path / "big.jsonl"
    write_transcript(path, 120)
    watermarks = []

    def save(cache):
        watermarks.append(state.get(cache, "big-1")["last_uuid"])

    api = RecordingApi(existing_page=None)
    worklog.sync(big_payload(path), api, {}, save=save)

    # 생성 직후 None 한 번, 그 뒤 배치마다 한 번씩
    assert watermarks[0] is None
    assert len(api.appended) >= 3
    assert len([w for w in watermarks if w is not None]) >= len(api.appended)
    assert watermarks[-1] == "u119"


def test_page_id_is_saved_before_the_first_append(tmp_path):
    """만든 페이지의 id 를 잃으면 다음 발화가 find_page → clear_body 로 되돌아간다."""
    api = RecordingApi(existing_page=None)
    seen = []

    def save(cache):
        seen.append((copy.deepcopy(cache), len(api.appended)))

    worklog.sync(payload(), api, {}, save=save)

    first_cache, appends_so_far = seen[0]
    assert appends_so_far == 0
    entry = state.get(first_cache, "sess-1")
    assert entry["page_id"] == "page-new"
    assert entry["last_uuid"] is None


def test_page_id_is_saved_right_after_clear_body(tmp_path):
    """본문이 비워진 직후가 워터마크 없이 page_id 를 남길 수 있는 안전한 순간이다."""
    api = RecordingApi(existing_page="page-old")
    seen = []

    def save(cache):
        seen.append((copy.deepcopy(cache), list(api.cleared), len(api.appended)))

    worklog.sync(payload(), api, {}, save=save)

    first_cache, cleared_so_far, appends_so_far = seen[0]
    assert cleared_so_far == ["page-old"]      # 비우기가 끝난 뒤
    assert appends_so_far == 0                 # 붙이기가 시작되기 전
    entry = state.get(first_cache, "sess-1")
    assert entry["page_id"] == "page-old"
    assert entry["last_uuid"] is None


def test_sync_aborts_instead_of_appending_progress_it_cannot_record(tmp_path):
    """save 가 죽으면 멈춘다. 기록 못 한 진행을 계속 붙이면 사고가 재현된다."""
    path = tmp_path / "big.jsonl"
    write_transcript(path, 120)
    api = RecordingApi(existing_page=None)

    def save(cache):
        # 첫 배치를 붙인 직후의 저장(= 그 배치의 워터마크)을 실패시킨다
        if len(api.appended) == 1:
            raise OSError("state.json 을 쓸 수 없다")

    try:
        worklog.sync(big_payload(path), api, {}, save=save)
    except OSError:
        pass
    else:
        raise AssertionError("save 실패를 삼켰다")

    assert len(api.appended) == 1


def test_sync_still_returns_the_cache_without_a_save_callback():
    # save 는 선택적이다 — 기본값은 아무것도 하지 않는다
    api = RecordingApi(existing_page=None)

    cache = worklog.sync(payload(), api, {})

    assert state.get(cache, "sess-1")["page_id"] == "page-new"


# ---------------------------------------------------------------------------
# 2026-10-12 사고 재현: append 가 Notion 에는 반영됐는데 응답이 읽기 타임아웃으로
# 끊겼다. 훅은 실패로 보고 워터마크를 저장하지 않았고, 다음 발화가 같은 배치를
# 다시 보내 메시지가 페이지에 두 번 실렸다 (실제 페이지에서 확인).
# ---------------------------------------------------------------------------

class PageApi(RecordingApi):
    """본문을 실제로 쌓아 두는 가짜.

    land_then_timeout 을 주면 append 가 블록을 그만큼 페이지에 반영한 **뒤**
    타임아웃을 낸다 — 요청은 도달했는데 응답만 잃은 상황이다.
    """

    def __init__(self, page=None, land_then_timeout=None):
        super().__init__()
        self.page = [] if page is None else list(page)
        self.land_then_timeout = land_then_timeout
        self.listed = 0

    def append_blocks(self, page_id, blocks):
        if self.land_then_timeout is not None:
            self.page.extend(blocks[:self.land_then_timeout])
            raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)")
        super().append_blocks(page_id, blocks)
        self.page.extend(blocks)

    def list_children(self, page_id):
        self.listed += 1
        return list(self.page)


def _timed_out_then_resynced(tmp_path, landed, page_between=None):
    """첫 발화는 블록 landed 개를 반영한 뒤 타임아웃, 다음 발화는 정상 응답."""
    path = tmp_path / "s.jsonl"
    write_transcript(path, 2)                      # 메시지 2개 = 블록 4개
    load = big_payload(path, session_id="t-1")
    saves = []

    def save(cache):
        saves.append(copy.deepcopy(cache))

    first = PageApi(land_then_timeout=landed)
    try:
        worklog.sync(load, first, {}, save=save)
    except requests.exceptions.ReadTimeout:
        pass
    else:
        raise AssertionError("타임아웃이 sync 밖으로 나오지 않았다")

    page = first.page if page_between is None else page_between(first.page)
    second = PageApi(page=page)
    worklog.sync(load, second, state.prune(copy.deepcopy(saves[-1])), save=save)

    expected = notion.build_blocks(transcript.conversation(transcript.read_entries(path)))
    return second, expected, load, saves


def test_append_that_landed_before_timing_out_is_not_sent_again(tmp_path):
    second, expected, load, saves = _timed_out_then_resynced(tmp_path, landed=4)

    assert second.page == expected                 # 메시지마다 한 번씩만 실렸다

    # 그리고 수렴한다 — 세 번째 발화는 본문을 다시 대조하지도, 붙이지도 않는다
    third = PageApi(page=second.page)
    worklog.sync(load, third, state.prune(copy.deepcopy(saves[-1])))
    assert third.listed == 0
    assert third.appended == []


def test_append_that_landed_halfway_sends_only_the_rest(tmp_path):
    # Cloudflare 반쪽 쪼개기 도중에 끊기면 메시지 중간에서 멈출 수 있다
    second, expected, _, _ = _timed_out_then_resynced(tmp_path, landed=3)

    assert second.page == expected


def test_marker_in_place_of_a_refused_block_still_counts_as_landed(tmp_path):
    # Cloudflare 가 거부한 블록 자리에는 원래 블록 대신 마커가 1:1 로 올라간다
    marker = notion._paragraph(notion.REFUSED_MARKER)

    def refuse_second(page):
        return [page[0], marker] + page[2:]

    second, _, _, _ = _timed_out_then_resynced(
        tmp_path, landed=4, page_between=refuse_second
    )

    assert second.appended == [("page-new", [])]   # 아무 블록도 다시 보내지 않았다


def test_page_that_does_not_match_is_resent_rather_than_skipped(tmp_path, monkeypatch):
    """대조가 어긋나면(사람이 페이지를 손봤다 등) 건너뛰지 않는다 — 중복이 유실보다 낫다."""
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    note = notion._paragraph("사람이 적은 메모")

    second, expected, _, _ = _timed_out_then_resynced(
        tmp_path, landed=0, page_between=lambda page: page + [note]
    )

    assert second.page == [note] + expected
    assert "t-1" in (tmp_path / "error.log").read_text(encoding="utf-8")


def test_stale_watermark_leaves_a_line_in_error_log(tmp_path, monkeypatch):
    """워터마크가 트랜스크립트에 없으면 그 세션은 영원히 아무것도 기록하지 못한다.

    훅은 무음이라 error.log 가 유일한 진단 수단이다.
    """
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi()
    cache = {"sess-1": {"page_id": "p1", "last_uuid": "없는-uuid", "updated": time.time()}}

    worklog.sync(payload(), api, cache)

    logged = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "없는-uuid" in logged
    assert "sess-1" in logged
    assert api.appended == []


def test_healthy_watermark_logs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)

    cache = worklog.sync(payload(), api, {})
    worklog.sync(payload(), RecordingApi(), cache)

    assert not (tmp_path / "error.log").exists()


# --- 세션 잠금 --------------------------------------------------

def configure(tmp_path, monkeypatch):
    """run() 이 읽을 설정·토큰을 tmp_path 에 만든다."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "TOKEN_PATH", tmp_path / "token")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    config.save_config({"database_id": "d"}, tmp_path / "config.json")
    config.save_token("t", tmp_path / "token")


def test_lock_is_exclusive_and_the_loser_waits_before_giving_up(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)

    first = worklog.acquire_lock("sess-A")
    assert first is not None

    started = time.monotonic()
    second = worklog.acquire_lock("sess-A", wait=0.5, poll=0.05)
    elapsed = time.monotonic() - started

    assert second is None
    # 즉시 포기하면 SessionEnd 가 마지막 턴을 주울 기회를 잃는다
    assert elapsed >= 0.5

    worklog.release_lock(first)
    third = worklog.acquire_lock("sess-A", wait=0)
    assert third is not None
    worklog.release_lock(third)
    assert list(tmp_path.glob(".lock-*")) == []


def test_lock_is_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)

    a = worklog.acquire_lock("sess-A", wait=0)
    b = worklog.acquire_lock("sess-B", wait=0)

    assert a is not None and b is not None and a != b


def test_stale_lock_from_a_killed_process_is_reclaimed(tmp_path, monkeypatch):
    """훅은 async(timeout 30) 라 SIGKILL 되면 finally 가 통째로 건너뛰어진다."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    orphan = worklog.acquire_lock("sess-A", wait=0)
    os.utime(str(orphan), (0, 0))          # 아주 오래된 잠금으로 위장

    recovered = worklog.acquire_lock("sess-A", wait=0.5, poll=0.05, stale=60)

    assert recovered is not None


def test_lock_path_stays_inside_the_hook_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)

    path = worklog.lock_path("../../etc/passwd")

    assert path.parent == tmp_path


def test_lock_is_released_even_when_the_run_fails(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(worklog, "Notion", lambda *a, **k: RecordingApi())
    monkeypatch.setattr(worklog, "sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("망함")))

    try:
        worklog.run(payload())
    except RuntimeError:
        pass
    else:
        raise AssertionError("예외가 삼켜졌다")

    assert list(tmp_path.glob(".lock-*")) == []


class SlowApi(RecordingApi):
    """네트워크에 머무는 동안 다른 훅이 끼어들 틈을 만든다."""

    def find_page(self, session_id, prop_name):
        time.sleep(0.25)
        return self.existing_page


def test_two_concurrent_runs_create_exactly_one_page(tmp_path, monkeypatch):
    """Stop 이 아직 날아가는 중에 SessionEnd 가 터지는 실제 상황.

    잠금이 없으면 두 프로세스가 모두 캐시 미스 → find_page(없음) → create_page 를
    돌아 같은 세션에 행이 두 개 생긴다. 중복 행은 업무일지의 최악의 오염이다.
    """
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "STATE_PATH", tmp_path / "state.json")

    api = SlowApi(existing_page=None)
    monkeypatch.setattr(worklog, "Notion", lambda *a, **k: api)

    errors = []

    def go():
        try:
            worklog.run(payload(session_id="race-1"))
        except Exception as exc:       # noqa: BLE001 - 스레드 밖으로 옮겨 확인한다
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert not any(t.is_alive() for t in threads)
    assert len(api.created) == 1
    assert len(state.load(tmp_path / "state.json")) == 1
    assert list(tmp_path.glob(".lock-*")) == []


# --- 다른 창의 항목 보존 ----------------------------------------

def test_save_merges_instead_of_clobbering_another_window(tmp_path, monkeypatch):
    """run() 이 캐시를 통째로 되쓰면 네트워크에 머무는 몇 초 사이에 다른 Claude Code
    창이 쓴 항목이 사라진다. 저장 직전에 다시 읽어 이 세션 항목만 얹어야 한다."""
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "HOME", tmp_path)
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(config, "STATE_PATH", state_path)

    class OtherWindowApi(RecordingApi):
        def create_page(self, props, fallback_props=None):
            # 이 순간 다른 창이 자기 세션을 기록한다
            state.save(state_path, state.put(
                state.load(state_path), "other-sess", "page-other", "u-other"))
            return super().create_page(props, fallback_props=fallback_props)

    api = OtherWindowApi(existing_page=None)
    monkeypatch.setattr(worklog, "Notion", lambda *a, **k: api)

    worklog.run(payload())

    final = state.load(state_path)
    assert set(final) == {"sess-1", "other-sess"}
    assert final["other-sess"]["page_id"] == "page-other"
    assert final["sess-1"]["page_id"] == "page-new"


# --- 기록하지 않는 세션 ---------------------------------------

@pytest.mark.parametrize("cwd", [
    "/tmp",
    "/tmp/hooktest/orig",
    "/var/tmp/proj",
    # 실제로 섞여 들어온 테스트 세션의 cwd 모양. macOS 만 /tmp 가 /private/tmp 의 링크다.
    pytest.param("/private/tmp/claude-501/x/scratchpad/e2e/proj",
                 marks=pytest.mark.skipif(sys.platform != "darwin", reason="macOS 전용 경로")),
])
def test_sessions_in_a_temp_dir_are_skipped(cwd, monkeypatch):
    monkeypatch.delenv(worklog.DISABLE_ENV, raising=False)

    assert worklog.should_skip(dict(payload(), cwd=cwd))


def test_sessions_under_the_users_temp_dir_are_skipped(monkeypatch):
    """macOS 의 $TMPDIR 은 /tmp 가 아니라 /var/folders/.../T 다."""
    monkeypatch.delenv(worklog.DISABLE_ENV, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", "/Users/me/fake-tmp")

    assert worklog.should_skip(dict(payload(), cwd="/Users/me/fake-tmp/proj"))


@pytest.mark.parametrize("cwd", ["/Users/me/Projects", "/tmpfoo/proj", "/Users/me/tmp/proj", "", None])
def test_sessions_elsewhere_are_recorded(cwd, monkeypatch):
    monkeypatch.delenv(worklog.DISABLE_ENV, raising=False)

    assert not worklog.should_skip(dict(payload(), cwd=cwd))


@pytest.mark.parametrize("value, skipped", [("1", True), ("true", True), ("0", False), ("", False)])
def test_disable_env_turns_recording_off(value, skipped, monkeypatch):
    monkeypatch.setenv(worklog.DISABLE_ENV, value)

    assert worklog.should_skip(payload()) is skipped


@pytest.mark.parametrize("env, cwd", [("1", "/Users/me/Projects"), ("", "/tmp/hooktest/orig")])
def test_run_skips_before_reading_the_setup(env, cwd, tmp_path, monkeypatch):
    """설정이 없어도 조용히 물러난다 — 거르기가 설정 확인보다 먼저다."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "TOKEN_PATH", tmp_path / "token")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setenv(worklog.DISABLE_ENV, env)
    monkeypatch.setattr(worklog, "Notion", lambda *a, **k: pytest.fail("Notion 을 부르면 안 된다"))

    worklog.run(dict(payload(), cwd=cwd))


# --- session.md 첨부 -------------------------------------------

class FailingUploadApi(RecordingApi):
    def __init__(self, existing_page=None):
        super().__init__(existing_page=existing_page)
        self.upload_attempts = 0

    def upload_file(self, filename, data, content_type="text/markdown"):
        # 실패 경로가 실제로 실행됐는지(=호출이 일어났는지) 테스트가 확인할
        # 수 있게 기록해 둔다. 이게 없으면 "호출 자체가 안 일어나 예외도 안
        # 났다"와 "호출됐고 잡혔다"를 테스트가 구분하지 못한다.
        self.upload_attempts += 1
        raise RuntimeError("업로드 실패")


class FailingDeleteApi(RecordingApi):
    def delete_block(self, block_id):
        raise RuntimeError("삭제 실패")


def end_payload(session_id="sess-1"):
    p = payload(session_id=session_id)
    p["hook_event_name"] = "SessionEnd"
    return p


def test_attaches_on_first_sync():
    api = RecordingApi(existing_page=None)

    cache = worklog.sync(payload(), api, {})

    assert len(api.uploaded) == 1
    assert api.uploaded[0][0] == worklog.ATTACHMENT_NAME
    assert len(api.attached) == 1
    entry = state.get(cache, "sess-1")
    assert entry["file_block_id"] == api.attached[0][3]


def test_attachment_persists_new_file_block_id_via_save():
    """state.put_file_block 뒤의 save(cache) 호출이 실제로 일어나는지 검증한다.

    cache 는 인자로 전달된 그 dict 그대로 반환되므로, 그 save(cache) 줄을
    지워도 반환값만 보는 assert 는 여전히 통과한다(실제로 149개 테스트가
    그 상태로도 다 통과했다) — file_block_id 는 이미 메모리 위 dict 에
    쓰여 있고, 그 쓰기 자체는 save 호출과 무관하기 때문이다. save 콜백이
    실제로 "받은" 스냅숏(=디스크에 쓰일 값)을 봐야 그 줄이 삭제됐을 때만
    이 테스트가 깨진다.
    """
    api = RecordingApi(existing_page=None)
    snapshots = []

    def save(cache):
        snapshots.append(copy.deepcopy(cache))

    worklog.sync(payload(), api, {}, save=save)

    persisted = state.get(snapshots[-1], "sess-1")
    assert persisted["file_block_id"] == api.attached[0][3]


def test_skips_attachment_when_unchanged():
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    api2 = RecordingApi()
    worklog.sync(payload(), api2, cache)

    assert api2.uploaded == []
    assert api2.attached == []


def test_refreshes_on_session_end():
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})
    old_id = api.attached[0][3]

    api2 = RecordingApi()
    cache2 = worklog.sync(end_payload(), api2, cache)

    assert len(api2.uploaded) == 1
    assert api2.deleted == [old_id]
    entry = state.get(cache2, "sess-1")
    assert entry["file_block_id"] == api2.attached[0][3]
    assert entry["file_block_id"] != old_id


def test_refreshes_when_blocks_refused(tmp_path, monkeypatch):
    # 실제 error.log 를 오염시키지 않는다 — "error.log 가 비어 있다"가
    # 문서화된 건강 점검 기준이다.
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    api2 = RecordingApi()
    api2.refused = 1
    worklog.sync(payload(), api2, cache)

    assert len(api2.uploaded) == 1
    assert len(api2.attached) == 1


def test_attach_new_before_delete_old():
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    api2 = RecordingApi()
    worklog.sync(end_payload(), api2, cache)

    attach_index = next(i for i, c in enumerate(api2.call_order) if c.startswith("attach:"))
    delete_index = next(i for i, c in enumerate(api2.call_order) if c.startswith("delete:"))
    assert attach_index < delete_index


def test_attachment_failure_does_not_raise(tmp_path, monkeypatch):
    # 실제 error.log 를 오염시키지 않는다 — "error.log 가 비어 있다"가
    # 문서화된 건강 점검 기준이다.
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    """브리프의 굵은 글씨 조건: 첨부가 실패해도 이미 저장된 블록 워터마크는
    "저장된 상태로" 남아야 한다.

    반환된 in-memory dict 만 보면 이 테스트는 구현이 첨부 단계를 아예 타지
    않아도(=옛 worklog.py, d6caf29) 우연히 통과한다 — cache 는 인자로 그대로
    돌아오므로 page_id/last_uuid 는 애초에 거기 있었다. 그래서 (a) 실패
    경로가 실제로 실행됐는지를 upload_attempts 로 확인하고, (b) "저장된"
    상태를 save 콜백이 받은 스냅숏으로 확인한다.
    """
    api = FailingUploadApi(existing_page=None)
    snapshots = []

    def save(cache):
        snapshots.append(copy.deepcopy(cache))

    worklog.sync(payload(), api, {}, save=save)

    assert api.upload_attempts == 1  # 실패 경로가 실제로 실행됐다

    persisted = state.get(snapshots[-1], "sess-1")
    assert persisted["page_id"] == "page-new"
    assert persisted["last_uuid"]
    assert "file_block_id" not in persisted


def test_attachment_failure_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = FailingUploadApi(existing_page=None)

    worklog.sync(payload(), api, {})

    logged = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "sess-1" in logged


def test_refused_count_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)
    api.refused = 2

    worklog.sync(payload(), api, {})

    logged = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "sess-1" in logged
    # 단순히 "2" 를 찾으면 로그 줄 맨 앞의 타임스탬프([2026-09-...])에 걸려
    # refused 값을 실제로 검증하지 못한다. 메시지 문구로 확인한다.
    assert "블록 2개" in logged


def test_delete_failure_keeps_new_id(tmp_path, monkeypatch):
    # 실제 error.log 를 오염시키지 않는다 — "error.log 가 비어 있다"가
    # 문서화된 건강 점검 기준이다.
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    api2 = FailingDeleteApi()
    cache2 = worklog.sync(end_payload(), api2, cache)

    entry = state.get(cache2, "sess-1")
    assert entry["file_block_id"] == api2.attached[0][3]


# --- 첨부 의무가 발화를 넘어 살아남는가 -------------------

def test_failed_upload_records_obligation_on_disk(tmp_path, monkeypatch):
    """업로드가 실패해도 '원문이 아직 안 올라갔다'가 디스크에 남아야 한다.

    Notion.refused 는 인스턴스 변수라 다음 발화의 새 인스턴스에서는 0 이다.
    의무가 메모리에만 있으면 다음 턴은 갱신을 건너뛰고, 페이지의 마커는
    그 원문이 없는 session.md 를 계속 가리킨다 — 마커가 거짓말을 한다.
    """
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    saved = []

    class FailingUpload(RecordingApi):
        def upload_file(self, filename, data, content_type="text/markdown"):
            raise RuntimeError("업로드 실패")

    api = FailingUpload(existing_page=None)
    api.refused = 1   # __init__ 이 0 으로 덮으므로 생성 뒤에 세운다
    cache = worklog.sync(payload(), api, {}, save=lambda c: saved.append(
        json.loads(json.dumps(c))))

    assert saved, "save 가 한 번도 불리지 않았다"
    assert saved[-1]["sess-1"]["attachment_stale"] is True
    assert cache["sess-1"]["attachment_stale"] is True


def test_carried_obligation_retries_after_the_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    """지난 발화에서 밀린 의무는, 간격이 지나면 거부가 없어도 다시 올린다."""
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    # 의무가 밀린 채로 넘어온 상태를 만든다
    cache["sess-1"]["attachment_stale"] = True
    cache["sess-1"]["attachment_attempted"] = time.time() - (
        worklog.ATTACHMENT_RETRY_SECONDS + 1)

    api2 = RecordingApi()          # refused == 0 이다
    worklog.sync(payload(), api2, cache)

    assert len(api2.uploaded) == 1


def test_carried_obligation_waits_out_the_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    """간격 안이면 재시도하지 않는다 — 계속 실패하는 업로드가 매 턴 수백 KB 를
    다시 올리는 것을 막는다."""
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    cache["sess-1"]["attachment_stale"] = True
    cache["sess-1"]["attachment_attempted"] = time.time()   # 방금 시도했다

    api2 = RecordingApi()
    worklog.sync(payload(), api2, cache)

    assert api2.uploaded == []


def test_fresh_refusal_uploads_immediately_ignoring_the_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    """갓 생긴 마커에는 간격을 걸지 않는다.

    걸면 마커가 가리키는 session.md 에 그 원문이 없는 상태가 간격만큼
    이어진다 — 마커가 거짓말을 하게 된다.
    """
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})
    assert cache["sess-1"]["attachment_attempted"]   # 방금 시도했다

    api2 = RecordingApi()
    api2.refused = 1               # 이번 턴에 새로 거부가 생겼다
    worklog.sync(payload(), api2, cache)

    assert len(api2.uploaded) == 1


def test_successful_upload_clears_the_obligation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})
    cache["sess-1"]["attachment_stale"] = True
    cache["sess-1"]["attachment_attempted"] = 0

    api2 = RecordingApi()
    result = worklog.sync(payload(), api2, cache)

    assert result["sess-1"]["attachment_stale"] is False


def test_title_refusal_does_not_retrigger_attachment(tmp_path, monkeypatch):
    """제목 거부는 갱신을 유발하지 않는다.

    제목은 매 발화 트랜스크립트에서 다시 계산되므로 한 번 거부되는 제목은
    **매 턴 거부된다.** 이걸 새 거부로 치면 그런 세션은 평생 매 턴 전체 원문을
    다시 올린다. 원래 제목은 이미 첫 업로드의 session.md 첫 줄에 들어 있고
    그 뒤로 변하지 않으므로 다시 올릴 이유가 없다.
    """
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    for _ in range(3):
        api2 = RecordingApi()
        api2.title_refused = 1
        worklog.sync(payload(), api2, cache)
        assert api2.uploaded == []

    logged = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "제목 1회" in logged        # 로그에는 남는다


# --- 크기 가드와 저장 실패 ---------------------------

def test_oversized_document_is_skipped_not_attempted(tmp_path, monkeypatch):
    """실패할 걸 아는 업로드는 시도하지 않는다.

    upload_file 은 단일 파트 경로뿐이라 큰 파일은 결정적으로 실패한다. 그런데
    훅은 async(30초)라 초과하면 SIGKILL 이고, 그러면 잠금이 남아 이후 2분간
    매 발화가 조용히 물러난다 — 첨부만 실패하는 게 아니라 블록 동기화까지
    멈춘다.
    """
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    monkeypatch.setattr(worklog, "MAX_ATTACHMENT_BYTES", 10)

    api = RecordingApi(existing_page=None)
    worklog.sync(payload(), api, {})

    assert api.uploaded == []
    logged = (tmp_path / "error.log").read_text(encoding="utf-8")
    assert "상한" in logged


def test_save_failure_after_attach_keeps_old_block(tmp_path, monkeypatch):
    """새 id 를 저장 못 했으면 옛 블록을 지우지 않는다.

    지우면 새 첨부는 아무도 모르는 고아가 되고 옛 첨부는 사라진다. 남겨두면
    최악이 '두 개 보인다'이고 다음 발화가 옛 id 로 정상 정리한다.
    """
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    api2 = RecordingApi()
    api2.refused = 1

    calls = {"n": 0}

    def flaky_save(c):
        calls["n"] += 1
        if calls["n"] > 2:          # 첨부 단계의 save 만 실패시킨다
            raise OSError("디스크 가득")

    worklog.sync(payload(), api2, cache, save=flaky_save)

    assert api2.deleted == []       # 옛 블록을 지우지 않았다


def test_put_does_not_wipe_the_carried_obligation(tmp_path, monkeypatch):
    """건너뛴 발화의 put() 이 밀린 의무를 지우면 안 된다.

    이건 file_block_id 가 매 배치마다 조용히 사라지던 버그와 같은 유형이다.
    의무가 지워지면 그 다음 발화는 밀린 원문이 있다는 사실 자체를 모르고,
    페이지의 마커는 영원히 없는 내용을 가리킨다.

    갱신을 **건너뛰는** 발화라야 이 버그가 드러난다 — 올리는 발화는 어차피
    put_file_block 이 의무를 해제하기 때문이다.
    """
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    cache["sess-1"]["attachment_stale"] = True
    cache["sess-1"]["attachment_attempted"] = time.time()   # 간격 안 — 건너뛴다

    api2 = RecordingApi()                                   # refused == 0
    result = worklog.sync(payload(), api2, cache)

    assert api2.uploaded == []                              # 정말 건너뛰었고
    assert result["sess-1"]["attachment_stale"] is True      # 의무는 살아남았다
    assert result["sess-1"]["attachment_attempted"]          # 시각도 살아남았다


# --- 대체 제목이 실제로 배선돼 있는가 ---------------------
# 아래 세 테스트가 없으면 worklog 의 fallback_props= 인자를 통째로 지워도
# 테스트가 전부 통과한다. 지우면 제목에 <script 가 든 세션이 다시 영구히
# 기록 불가가 된다 — 실환경에서 확인된 경로다.

def _title_of(props):
    return props["작업"]["title"][0]["text"]["content"]


def test_create_page_receives_a_conversation_free_fallback_title():
    api = RecordingApi(existing_page=None)
    worklog.sync(payload(), api, {})

    assert api.fallbacks and api.fallbacks[0] is not None
    fallback = _title_of(api.fallbacks[0])
    real = _title_of(api.created[0])
    assert fallback != real                      # 통째로 다른 제목이고
    assert fallback == "Projects 세션 " + fallback.split()[-1]   # 형식이 맞고
    assert "세션" in fallback


def test_cached_path_update_props_also_gets_the_fallback():
    """캐시 경로가 특히 중요하다 — update_props 가 append 보다 먼저 돌아서,
    여기서 막히면 그 세션은 아무것도 기록되지 않는다."""
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    api2 = RecordingApi()
    worklog.sync(payload(), api2, cache)

    assert api2.updated                           # 캐시 경로를 탔고
    assert api2.fallbacks and api2.fallbacks[0] is not None


def test_recovery_path_update_props_also_gets_the_fallback():
    """캐시 미스인데 기존 페이지를 찾은 경로."""
    api = RecordingApi(existing_page="page-found")
    worklog.sync(payload(), api, {})

    assert api.cleared                            # clear_body 경로를 탔고
    assert api.fallbacks and api.fallbacks[0] is not None


def test_fallback_title_carries_no_conversation_text():
    """대체 제목에 대화가 한 글자라도 섞이면 그것도 거부될 수 있다."""
    api = RecordingApi(existing_page=None)
    worklog.sync(payload(), api, {})

    fallback = _title_of(api.fallbacks[0])
    entries = transcript.read_entries(str(FIXTURES / "with_aititle.jsonl"))
    for message in transcript.conversation(entries):
        for word in message["text"].split():
            if len(word) > 3:
                assert word not in fallback


# --- 의무 저장 실패가 업로드를 막으면 안 된다 -------------

def test_obligation_save_failure_still_attempts_upload(tmp_path, monkeypatch):
    """의무를 디스크에 못 남겨도 업로드는 시도한다.

    여기서 물러나면 이번 턴에 만든 마커의 원문까지 잃는다 — 저장 실패는
    다음 턴의 문제지 이번 턴의 원문을 버릴 이유가 아니다.
    """
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")
    api = RecordingApi(existing_page=None)
    cache = worklog.sync(payload(), api, {})

    api2 = RecordingApi()
    api2.refused = 1
    calls = {"n": 0}

    def failing_save(c):
        calls["n"] += 1
        if calls["n"] > 2:          # 첨부 단계의 save 부터 실패시킨다
            raise OSError("디스크 가득")

    worklog.sync(payload(), api2, cache, save=failing_save)

    assert len(api2.uploaded) == 1   # 저장이 실패했어도 올렸다


def test_failed_upload_is_throttled_on_the_next_turn(tmp_path, monkeypatch):
    """계속 실패하는 업로드가 매 턴 전체 문서를 다시 올리면 안 된다.

    시도 시각이 아니라 **성공** 시각만 남기면, 실패가 이어지는 동안 간격이
    늘 지나 있어 간격이 막으려던 바로 그 상황에서 무력해진다.
    """
    monkeypatch.setattr(config, "ERROR_LOG", tmp_path / "error.log")

    class FailingUpload(RecordingApi):
        def upload_file(self, filename, data, content_type="text/markdown"):
            self.uploaded.append(filename)
            raise RuntimeError("업로드 실패")

    api = FailingUpload(existing_page=None)
    cache = worklog.sync(payload(), api, {})
    assert len(api.uploaded) == 1        # 첫 시도는 한다

    for _ in range(3):                   # 그 뒤 연속 턴은 간격에 막힌다
        api2 = FailingUpload()
        worklog.sync(payload(), api2, cache)
        assert api2.uploaded == []
