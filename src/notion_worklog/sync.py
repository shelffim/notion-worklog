"""한 세션의 트랜스크립트를 Notion 페이지 하나로 동기화한다.

에이전트(claude/codex)별 차이는 agents/ 어댑터가 흡수하고, 여기서는 에이전트와
무관한 흐름 — 페이지 찾기/만들기, 워터마크 이후 증분 append, 첨부 갱신, 잠금 —
만 다룬다. 훅 진입점(무음, 항상 exit 0)은 cli.py 에 있다.
"""
import os
import re
import time
import traceback
from datetime import datetime

from . import config, state, transcript
from .agents import AGENTS, claude
from .notion import (DEFAULT_COLUMNS, REFUSED_MARKER, Notion, batches, build_blocks,
                     build_props, same_block)
from .text import text


# 잠금이 오래 남아 있으면 SIGKILL 로 죽은 프로세스가 흘린 것이다. 훅 timeout(120초)
# 보다 길게 잡아, 살아 있는 훅의 잠금이 이 나이에 도달할 수 없게 한다.
LOCK_STALE_SECONDS = 180
# 진 쪽이 즉시 포기하면 안 된다. SessionEnd 는 Stop 이 놓친 마지막 턴을 줍자고
# 존재하므로, 이긴 쪽이 끝나기를 잠깐 기다렸다가 이어받아야 그 역할을 한다.
LOCK_WAIT_SECONDS = 4.0
LOCK_POLL_SECONDS = 0.2

# Cloudflare WAF 가 <script 가 든 페이지 본문을 거부하는 문제의 우회로. 세션
# 페이지에는 항상 이 이름으로 세션 전체 원문(마크다운)이 첨부된다.
# attach_file 에 이 이름을 명시적으로 넘기므로 업로드 파일명과 화면에 보이는
# 첨부 이름이 항상 같이 움직인다.
ATTACHMENT_NAME = "session.md"

# 첨부가 밀렸을 때(attachment_stale) 재업로드를 다시 시도하는 최소 간격.
#
# 필요한 이유: 거부된 블록이 있으면 원문을 반드시 올려야 하지만, 업로드가
# 계속 실패하는 상황(용량 초과, 지속적 장애)에서는 매 어시스턴트 턴마다
# 수백 KB 를 다시 올리게 된다. 훅은 턴마다 불리므로 이건 그냥 낭비다.
# 120초는 잠금 회수 기준(LOCK_STALE_SECONDS)과 같은 눈금이고, 사람이 한
# 턴을 주고받는 시간보다 길어 연속 턴에서 재시도가 겹치지 않는다.
# SessionEnd 는 이 간격을 무시한다 — 마지막 기회이므로 무조건 시도한다.
ATTACHMENT_RETRY_SECONDS = 120

# 이 크기를 넘는 문서는 업로드를 시도하지 않고 건너뛴다.
#
# upload_file 은 단일 파트 경로만 구현돼 있어 큰 파일은 결정적으로 실패한다.
# 그런데 훅 예산은 120초이고 async 라 초과하면 SIGKILL 이다 — finally 가
# 통째로 건너뛰어져 잠금 파일이 남고, 그 뒤 LOCK_STALE_SECONDS 동안 매 발화가
# LOCK_WAIT_SECONDS 를 허비하고 조용히 물러난다. 즉 대용량 트랜스크립트는
# 첨부에 실패하는 데서 끝나지 않고 **블록 동기화까지 2분씩 멈춰 세운다.**
# 실패할 걸 아는 업로드는 시도하지 않는 편이 훨씬 싸다.
# 20MB 는 Notion 단일 파트 상한(20MB)에 맞춘 값이다. 실측한 이 세션의
# session.md 가 156KB 였으니 정상 사용과는 두 자릿수 배 차이가 있다.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


def lock_path(session_id):
    """세션 잠금 파일 경로. 세션ID 를 파일명으로 쓰므로 경로 문자를 제거한다."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))[:64] or "unknown"
    return config.HOME / ".lock-{}".format(safe)


def _drop_if_stale(path, stale):
    """죽은 프로세스가 남긴 잠금을 나이로 회수한다.

    훅은 async(timeout 120) 로 돌아 SIGKILL 될 수 있고 그러면 finally 가 아예
    실행되지 않는다. 나이로 회수하지 않으면 그 세션은 영구히 기록이 멈춘다.
    """
    try:
        age = time.time() - os.stat(str(path)).st_mtime
    except OSError:
        return
    if age <= stale:
        return
    try:
        os.unlink(str(path))
    except OSError:
        pass


def acquire_lock(session_id, wait=LOCK_WAIT_SECONDS, poll=LOCK_POLL_SECONDS,
                 stale=LOCK_STALE_SECONDS):
    """세션당 한 프로세스만 돌게 하는 O_EXCL 잠금. 얻으면 경로, 못 얻으면 None.

    Stop 과 SessionEnd 가 모두 등록돼 있어 마지막 Stop 이 아직 날아가는 중에
    사용자가 세션을 끝내면 두 프로세스가 동시에 캐시 미스 → find_page(없음) →
    create_page 를 돌아 같은 세션에 행이 두 개 생긴다. find_page → create_page
    가 원자적이지 않으므로 바깥에서 직렬화한다.
    """
    path = lock_path(session_id)
    deadline = time.monotonic() + wait
    while True:
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # 경합. 상대가 죽어서 남긴 잠금이면 회수하고 다음 바퀴에 다시 시도한다.
            _drop_if_stale(path, stale)
        else:
            try:
                os.write(handle, str(os.getpid()).encode("ascii"))
            except OSError:
                pass
            finally:
                os.close(handle)
            return path

        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def release_lock(path):
    """잠금 해제. 실패해도 나이로 회수되므로 조용히 넘어간다."""
    try:
        os.unlink(str(path))
    except OSError:
        pass


def _no_save(cache):
    """save 기본값. 아무것도 하지 않는다."""


def sync(payload, api, cache, save=_no_save, agent=None, columns=None, lang="ko"):
    """한 번의 동기화. 갱신된 캐시를 돌려준다.

    agent 는 agents/ 의 어댑터 모듈(기본 claude), columns 는 역할 → 열 이름,
    lang 은 본문 머리글·마커·대체 제목의 언어다.

    save 는 "여기까지의 캐시를 지금 영구화하라"는 콜백이다. state.put 이 cache 를
    제자리에서 고치는 것을 전달 수단으로 삼지 않는다 — 워터마크가 유실되면 다음
    발화가 clear_body 전체 재작성(요청 100+)으로 떨어지므로, 어느 시점에 무엇이
    저장되는지가 코드에 그대로 보여야 한다. 계약은 콜백이다.
    """
    agent = agent or claude
    columns = columns or DEFAULT_COLUMNS
    labels = (text(lang)["user"], "🤖 {}".format(agent.SHORT))

    session_id = payload.get("session_id")
    if not session_id:
        return cache

    # Codex 는 트랜스크립트가 없는 세션에서 transcript_path 를 null 로 보낸다
    entries = agent.read_entries(payload.get("transcript_path") or "")
    if not entries:
        return cache

    when = agent.started_at(entries) or datetime.now(config.TZ)
    project = agent.project_name(entries, payload.get("cwd") or "")
    title = agent.title(entries, project, when, lang)

    cached = state.get(cache, session_id)
    props = build_props(title, project, when, session_id, agent.NAME, columns)
    # 제목은 대화에서 온다 — 거기 <script 가 있으면 이 요청이 그 세션의 첫 API
    # 호출부터 WAF 에 막힌다. 그때 통째로 갈아끼울, 대화가 한 글자도 안 섞인
    # 제목을 미리 만들어 넘긴다 (notion._retry_props 참고).
    fallback_props = build_props(
        transcript.fallback_title(project, when, lang), project, when, session_id,
        agent.NAME, columns,
    )

    if cached:
        # state.prune 이 세 키를 모두 검증하므로 여기서는 그대로 인덱싱한다.
        page_id = cached["page_id"]
        last_uuid = cached["last_uuid"]
        api.update_props(page_id, props, fallback_props=fallback_props)
    else:
        page_id = api.find_page(session_id, columns["session"])
        last_uuid = None
        if page_id:
            # 캐시를 잃었고 워터마크를 모른다. 본문을 비우고 전부 다시 쓴다.
            api.clear_body(page_id)
            # 본문이 빈 지금이 page_id 를 워터마크 없이 남길 수 있는 안전한 순간이다.
            # 여기서 저장하지 않으면 뒤에서 실패했을 때 다음 발화가 또 find_page →
            # clear_body(블록당 DELETE 1회) 로 떨어져 영원히 수렴하지 않는다.
            state.put(cache, session_id, page_id, None)
            save(cache)
            api.update_props(page_id, props, fallback_props=fallback_props)
        else:
            page_id = api.create_page(props, fallback_props=fallback_props)
            # 만든 직후 저장한다. 여기서 잃으면 다음 발화가 find_page → clear_body
            # 경로로 되돌아간다.
            state.put(cache, session_id, page_id, None)
            save(cache)

    if last_uuid is not None and not any(
        m["uuid"] == last_uuid for m in agent.conversation(entries)
    ):
        # conversation() 이 빈 리스트를 돌려주므로 이 세션은 이후 아무것도 기록하지
        # 못한다. 훅은 무음이라 error.log 가 유일한 흔적이다.
        config.log_error(
            "워터마크 uuid 가 트랜스크립트에 없어 기록이 멈춘다: session={} uuid={}".format(
                session_id, last_uuid
            )
        )

    messages = agent.conversation(entries, after_uuid=last_uuid)

    skip = 0
    if cached and cached.get("append_pending") is True and messages:
        skip = _landed_blocks(api, page_id, entries, messages, session_id, agent, labels)

    # 배치 하나가 성공할 때마다 워터마크를 즉시 저장한다. run() 의 finally 에 미루면
    # async 훅이 timeout 으로 SIGKILL 될 때 finally 가 통째로 건너뛰어진다.
    for blocks, batch_uuid in batches(messages, skip=skip, labels=labels):
        # 보내기 전에 "결과를 모르는 append" 를 디스크에 남긴다. 타임아웃이나
        # SIGKILL 로 끝나도 다음 발화가 페이지를 대조하고 나서 이어 붙인다.
        state.mark_append_pending(cache, session_id)
        save(cache)
        api.append_blocks(page_id, blocks)
        last_uuid = batch_uuid
        state.put(cache, session_id, page_id, last_uuid)
        # save 실패는 삼키지 않는다. 기록하지 못한 진행을 계속 이어붙이면
        # 다음 발화가 또 처음부터 다시 쓰게 되어 같은 사고가 재현된다.
        save(cache)

    # 새 메시지가 없어도 updated 를 갱신해 활성 세션이 TTL 로 사라지지 않게 한다.
    updated = state.put(cache, session_id, page_id, last_uuid)
    save(cache)

    # 여기서부터는 블록 워터마크 저장이 이미 끝난 뒤다. 첨부가 실패해도 그
    # 진행을 되돌리면 안 되므로, 이 구간 전체를 별도 함수로 감싸 예외를 삼킨다.
    _sync_attachment(api, cache, save, session_id, page_id, entries, title,
                     project, when, payload.get("hook_event_name"), cached,
                     agent=agent, lang=lang)

    return updated


def _landed_blocks(api, page_id, entries, messages, session_id, agent=claude,
                   labels=None):
    """결과를 모른 채 끝난 append 가 실제로 페이지에 올린 블록 수.

    페이지 본문은 워터마크까지의 블록(거부된 블록은 마커로 1:1 대체) 뒤에 그
    append 가 올린 블록이 이어진 모양이어야 한다. 첨부(file 블록)는 중간에 끼므로
    빼고 센다. 이어진 부분이 보낼 블록의 앞부분과 정확히 같을 때만 그만큼
    건너뛴다. 어긋나면(사람이 페이지를 손봤다 등) 0 을 돌려준다 — 중복은
    유실보다 낫다.
    """
    pending = build_blocks(messages, labels)
    before = len(build_blocks(agent.conversation(entries), labels)) - len(pending)
    body = [b for b in api.list_children(page_id) if b.get("type") != "file"]
    landed = body[before:]
    marker = getattr(api, "marker", REFUSED_MARKER)

    if len(landed) > len(pending) or not all(
        same_block(on_page, built, marker) for on_page, built in zip(landed, pending)
    ):
        config.log_error(
            "session={} 미확정 append 대조 실패 — 본문 {}블록, 예상 {}블록 뒤를 "
            "알 수 없어 그대로 다시 보낸다 (중복될 수 있다)".format(
                session_id, len(body), before
            )
        )
        return 0
    return len(landed)


def _sync_attachment(api, cache, save, session_id, page_id, entries, title,
                     project, when, hook_event_name, previous_entry, now=None,
                     agent=claude, lang="ko"):
    """session.md 첨부를 만들거나 최신본으로 교체한다.

    사용자가 선택한 정책은 "항상 첨부" — 페이지가 만들어질 때 붙이고,
    SessionEnd 에서 최종본으로 갱신한다. 매 어시스턴트 턴마다 전체 원문을
    다시 올리는 것은 낭비이고 페이싱만 잡아먹으므로 아래 조건에서만 올린다.
    """
    now = time.time() if now is None else now
    previous_entry = previous_entry if isinstance(previous_entry, dict) else {}
    old_block_id = previous_entry.get("file_block_id")
    is_session_end = hook_event_name == "SessionEnd"
    # api 가 테스트 더블이면 이 속성들이 없을 수 있다. 이 단계는 실패해도
    # sync() 를 죽이면 안 되는 부수 단계이므로 읽는 시점부터 방어한다.
    refused = getattr(api, "refused", 0)
    title_refused = getattr(api, "title_refused", 0)

    if refused or title_refused:
        # 훅은 무음이라 error.log 가 유일한 흔적이다.
        config.log_error(
            "session={} 에서 Cloudflare WAF 가 블록 {}개, 제목 {}회를 대체했다 — "
            "원문은 session.md 에서 확인할 것".format(
                session_id, refused, title_refused
            )
        )

    # 제목 거부는 첨부 갱신을 유발하지 않는다.
    #
    # 제목은 매 발화 트랜스크립트에서 다시 계산되므로, 한 번 거부되는 제목은
    # **매 턴 거부된다.** 이걸 "새 거부"로 치면 그런 세션은 평생 매 턴 전체
    # 원문을 다시 올린다. 그럴 필요가 없는 이유는, 원래 제목이 session.md 의
    # 첫 줄에 들어가는데 그 첫 업로드는 "아직 첨부가 없다" 조건으로 어차피
    # 일어나기 때문이다. 한 번 올라가면 제목은 더 이상 변하지 않는다.
    fresh_refusal = refused > 0

    if fresh_refusal:
        # 의무를 **디스크에** 남긴다. refused 는 인스턴스 변수라 이번 업로드가
        # 실패하면 다음 발화에서는 0 이 되어 의무가 사라진다 — 그러면 마커가
        # 가리키는 session.md 에 정작 그 원문이 없게 된다.
        state.mark_attachment_stale(cache, session_id)
        try:
            save(cache)
        except Exception:
            # 저장에 실패해도 아래 업로드는 시도한다. 성공하면 어차피 의무가
            # 해제되므로, 여기서 물러나면 이번 턴의 원문까지 잃는다.
            config.log_error(
                "session={} 첨부 의무 저장 실패:\n{}".format(
                    session_id, traceback.format_exc(limit=3)
                )
            )

    stale = bool(previous_entry.get("attachment_stale")) or fresh_refusal
    attempted = previous_entry.get("attachment_attempted")
    if not isinstance(attempted, (int, float)):
        attempted = 0

    if is_session_end:
        pass                       # 최종본 — 마지막 기회이므로 무조건 올린다
    elif fresh_refusal:
        # 방금 만든 마커가 "원문은 session.md 에 있다"고 가리킨다. 여기에 간격을
        # 걸면 그 마커가 간격만큼 거짓말을 한다 — 그래서 즉시 올린다.
        pass
    elif not old_block_id or stale:
        # 올려야 할 이유는 있지만 새로 생긴 건 아니다(첫 업로드가 아직 성공하지
        # 못했거나, 지난 발화의 의무가 넘어왔다). 계속 실패하는 업로드가 매 턴
        # 수백 KB 를 다시 올리지 않도록 여기에만 간격을 건다.
        if now - attempted < ATTACHMENT_RETRY_SECONDS:
            return
    else:
        return                     # 올릴 이유가 없다

    # 시도 시각을 먼저 남긴다. 성공/실패를 가리지 않으므로, 실패가 반복돼도
    # 간격이 실제로 걸린다 (성공 시각만 남기면 실패 중에는 간격이 늘 지나 있다).
    state.mark_attachment_attempt(cache, session_id, when=now)
    try:
        save(cache)
    except Exception:
        config.log_error(
            "session={} 첨부 시도 시각 저장 실패:\n{}".format(
                session_id, traceback.format_exc(limit=3)
            )
        )

    try:
        doc = transcript.document(agent.conversation(entries), title, project, when,
                                  session_id, agent.NAME, agent.SHORT, lang)
        payload = doc.encode("utf-8")
        if len(payload) > MAX_ATTACHMENT_BYTES:
            # 실패할 걸 아는 업로드는 시도하지 않는다 — 이유는 상수 주석 참고.
            config.log_error(
                "session={} session.md 가 {}바이트로 상한({})을 넘어 첨부를 "
                "건너뛴다".format(session_id, len(payload), MAX_ATTACHMENT_BYTES)
            )
            return

        # 순서가 요구사항이다: 새 첨부를 먼저 붙이고 캐시에 저장한 뒤에야
        # 옛것을 지운다. 반대로 하면 중간에 실패했을 때 페이지에 첨부가
        # 하나도 없는 상태가 남는다.
        file_upload_id = api.upload_file(ATTACHMENT_NAME, payload)
        new_id = api.attach_file(page_id, file_upload_id, name=ATTACHMENT_NAME)
    except Exception:
        # 블록 워터마크는 이미 저장이 끝났다. 첨부 실패가 그 진행을 되돌리게
        # 두면 안 되므로 여기서 삼키고 로그만 남긴다. attachment_stale 은
        # 위에서 이미 디스크에 남았으므로 다음 발화가 이어받는다.
        config.log_error(
            "session={} session.md 첨부 실패:\n{}".format(
                session_id, traceback.format_exc(limit=3)
            )
        )
        return

    state.put_file_block(cache, session_id, new_id)
    try:
        save(cache)
    except Exception:
        # 새 id 를 디스크에 못 남겼다. 여기서 옛 블록까지 지우면 다음 발화는
        # 옛 id 로 이미 없는 블록을 지우려 하고, 방금 붙인 새 첨부는 아무도
        # 모르는 고아가 된다. 지우지 않고 두면 최악이 "첨부가 두 개 보인다"
        # 이고 다음 발화가 옛 id 로 정상 정리한다.
        config.log_error(
            "session={} 첨부 id({}) 저장 실패 — 이전 첨부를 지우지 않고 "
            "다음 발화에 맡긴다:\n{}".format(
                session_id, new_id, traceback.format_exc(limit=3)
            )
        )
        return

    if old_block_id:
        # 옛 첨부는 새 첨부가 저장된 다음에만 지운다 — 삭제가 실패해도 새 id 는
        # 이미 저장돼 있으므로 최악의 결과가 "첨부가 두 개 보인다"이고, 이건
        # "원문이 사라진다"보다 훨씬 낫다. 이 실패는 위의 "첨부 실패"와 실체가
        # 다르므로(원문은 이미 안전하다) 로그도 따로 남긴다 — 아니면 error.log
        # 만 보는 운영자가 원문이 없어졌다고 오판한다.
        try:
            api.delete_block(old_block_id)
        except Exception:
            config.log_error(
                "session={} 이전 session.md 블록({}) 삭제 실패 — 새 첨부는 "
                "이미 저장됐으니 첨부가 두 개 보일 수 있다:\n{}".format(
                    session_id, old_block_id, traceback.format_exc(limit=3)
                )
            )


def run(payload, agent_key="claude"):
    settings = config.load_config()
    token = config.load_token()
    db_id = settings.get("database_id")
    if not token or not db_id:
        raise RuntimeError("설정이 없습니다 — `notion-worklog setup` 을 먼저 실행하세요")
    agent = AGENTS[agent_key]
    lang = settings.get("lang") or "ko"
    columns = settings.get("columns") or None

    session_id = payload.get("session_id")
    if not session_id:
        return

    lock = acquire_lock(session_id)
    if lock is None:
        # 기다려도 못 얻었다. 같은 세션의 다른 훅이 같은 트랜스크립트를 기록 중이고
        # 남은 턴은 다음 발화가 가져간다. 조용히 물러난다.
        return

    try:
        cache = state.prune(state.load(config.STATE_PATH))

        def save(current):
            """이 세션 항목만 최신 파일에 병합해 되쓴다.

            통째로 되쓰면 sync 가 네트워크에 머무는 몇 초 사이에 다른 Claude Code
            창이 쓴 항목을 덮어쓴다. 저장 직전에 다시 읽어 이 세션 항목만 얹으면
            덮어쓸 수 있는 창이 마이크로초로 줄어든다.
            """
            entry = state.get(current, session_id)
            latest = state.prune(state.load(config.STATE_PATH))
            if entry is not None:
                latest[session_id] = entry
            state.save(config.STATE_PATH, latest)

        api = Notion(token, db_id, marker=text(lang)["marker"])
        sync(payload, api, cache, save=save, agent=agent, columns=columns, lang=lang)
    finally:
        release_lock(lock)
