"""sessionId → (pageId, 마지막 기록 uuid) 캐시.

여러 Claude Code 창이 동시에 쓸 수 있으므로 임시 파일 + os.replace 로 원자 교체한다.
겹쳐서 한쪽이 유실돼도 캐시 미스로 떨어질 뿐이고, 세션ID 검색이 중복 행을 막는다.
"""
import json
import os
import tempfile
import time

TTL_SECONDS = 30 * 24 * 3600


def load(path):
    """캐시를 읽는다. 없거나 깨졌으면 빈 dict — 캐시는 언제든 버릴 수 있어야 한다."""
    # 캐시는 언제든 버릴 수 있어야 하므로 어떤 실패도 밖으로 내지 않는다.
    # (ValueError 만 잡으면 깊게 중첩된 JSON 의 RecursionError 가 새어 나간다)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save(path, data):
    """임시 파일에 쓴 뒤 원자적으로 교체한다."""
    directory = os.path.dirname(str(path)) or "."
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(data, session_id):
    entry = data.get(session_id)
    return entry if isinstance(entry, dict) else None


# put() 이 매 배치마다 엔트리를 새로 만들기 때문에, 첨부 관련 키는 명시적으로
# 옮겨 담지 않으면 배치 하나 저장할 때마다 지워진다. 실제로 file_block_id 가
# 그렇게 사라지는 버그가 있었다. 새 키를 추가할 때 여기 넣는 것을 잊으면 같은
# 사고가 반복되므로 목록을 한 곳에 모아 둔다.
#
# append_pending 은 일부러 넣지 않는다. 배치가 성공해 put() 이 새 워터마크를
# 쓰는 순간이 곧 "결과를 모르는 append 가 없다"가 되는 순간이기 때문이다.
CARRIED_KEYS = ("file_block_id", "attachment_stale", "attachment_attempted")


def put(data, session_id, page_id, last_uuid):
    existing = data.get(session_id)
    carried = {}
    if isinstance(existing, dict):
        carried = {k: existing[k] for k in CARRIED_KEYS if k in existing}

    entry = {
        "page_id": page_id,
        "last_uuid": last_uuid,
        "updated": time.time(),
    }
    entry.update(carried)
    data[session_id] = entry
    return data


def put_file_block(data, session_id, file_block_id):
    """첨부 블록 id 를 기록하고 밀린 첨부 의무를 해제한다.

    엔트리가 없으면 아무것도 안 한다.
    """
    entry = data.get(session_id)
    if not isinstance(entry, dict):
        return data
    entry["file_block_id"] = file_block_id
    # 업로드가 성공했으니 "원문이 아직 안 올라갔다"는 의무는 해제된다.
    entry["attachment_stale"] = False
    return data


def mark_attachment_attempt(data, session_id, when=None):
    """첨부 업로드를 **시도한** 시각을 남긴다. 성공/실패를 가리지 않는다.

    성공 시각만 남기면 재시도 간격이 마지막 **성공**부터 재어져, 업로드가 계속
    실패하는 동안에는 간격이 영원히 지나 있다 — 매 턴 전체 문서를 다시 올리게
    된다. 간격이 막으려던 바로 그 상황에서 무력해지므로 시도 시각을 남긴다.
    """
    entry = data.get(session_id)
    if not isinstance(entry, dict):
        return data
    entry["attachment_attempted"] = time.time() if when is None else when
    return data


def mark_attachment_stale(data, session_id):
    """첨부가 최신이 아님을 **디스크에** 남긴다.

    Notion.refused 는 인스턴스 변수라 다음 발화의 새 인스턴스에서는 0 이다.
    거부된 블록이 생겼는데 그 턴의 업로드가 실패하면, 다음 턴에는 의무가 사라져
    첨부 갱신을 건너뛴다 — 그러면 페이지의 마커는 "원문은 session.md 에 있다"고
    가리키는데 그 파일에는 그 메시지가 없다. 마커가 거짓말을 하게 되고 원문은
    Notion 어디에서도 복구할 수 없다. 그래서 의무를 캐시에 적어 살아남게 한다.
    """
    entry = data.get(session_id)
    if not isinstance(entry, dict):
        return data
    entry["attachment_stale"] = True
    return data


def mark_append_pending(data, session_id):
    """결과를 모르는 append 가 나간다는 사실을 **보내기 전에** 남긴다.

    읽기 타임아웃은 "요청이 반영되지 않았다"는 뜻이 아니다. Notion 이 블록을 이미
    붙였는데 응답만 늦었을 수 있다 (2026-10-12 실제 발생). 이 표시가 남아 있으면
    다음 발화가 페이지를 읽어 대조한 뒤에 이어 붙인다. 해제는 put() 이 한다.
    """
    entry = data.get(session_id)
    if not isinstance(entry, dict):
        return data
    entry["append_pending"] = True
    return data


def is_valid(entry):
    """sync 가 그대로 인덱싱해도 되는 온전한 항목인지.

    updated 만 보던 시절에는 page_id/last_uuid 가 잘려나간 항목이 prune 을 통과해
    sync 의 cached["page_id"] 에서 매 턴 KeyError 를 냈다. error.log 말고는 진단
    수단이 없는 훅에서 이런 조용한 반복 실패가 가장 비싸다. 세 키를 모두 검사해
    깨진 항목은 여기서 버리고, 버려진 세션은 캐시 미스로 떨어져 정상 복구된다.
    """
    if not isinstance(entry, dict):
        return False
    # last_uuid 는 None 이 정상값(본문이 빈 새 페이지)이므로 값이 아니라 키의
    # 존재를 본다. .get() 만으로는 "없음"과 "명시적 null"을 구분할 수 없다.
    if "last_uuid" not in entry:
        return False
    if not (entry["last_uuid"] is None or isinstance(entry["last_uuid"], str)):
        return False
    if not isinstance(entry.get("page_id"), str) or not entry["page_id"]:
        return False
    # updated 가 숫자가 아니면 뺄셈이 TypeError 를 낸다. 형식이 깨진 항목은
    # 버리는 것이 이 모듈의 계약이므로, 크래시가 아니라 제외로 처리한다.
    if not isinstance(entry.get("updated"), (int, float)):
        return False

    # 첨부 키들은 **있을 때만** 검사한다. 디스크에 이미 있는 엔트리는 세 키뿐이라
    # 존재를 요구하면 기존 세션이 전부 무효가 된다.
    #
    # 값이 깨졌을 때 버리는 이유: 깨진 file_block_id 는 "이미 첨부돼 있다"로 읽혀
    # 갱신을 영원히 건너뛰게 만들고, 그러면 페이지의 마커가 없는 첨부를 가리킨다.
    # 깨진 attachment_attempted 는 재업로드 간격 계산에서 TypeError 를 낸다.
    if "file_block_id" in entry:
        if not isinstance(entry["file_block_id"], str) or not entry["file_block_id"]:
            return False
    if "attachment_attempted" in entry:
        if not isinstance(entry["attachment_attempted"], (int, float)):
            return False
    if "attachment_stale" in entry:
        if not isinstance(entry["attachment_stale"], bool):
            return False
    return True


def prune(data, now=None):
    """TTL 지난 항목과 형식이 깨진 항목을 버린다."""
    now = time.time() if now is None else now
    return {
        key: value
        for key, value in data.items()
        if is_valid(value) and now - value["updated"] < TTL_SECONDS
    }
