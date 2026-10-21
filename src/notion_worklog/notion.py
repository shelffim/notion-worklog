"""Notion REST API 래퍼. requests 외 의존 없음."""
import json
import time

import requests

from .config import NOTION_TIMEOUT, NOTION_VERSION
from .text import TEXT

API = "https://api.notion.com/v1"

# 열 이름 기본값(한국어). 실제 실행에서는 setup 이 대상 테이블에 맞춰 고른 이름
# (config.json 의 columns)을 넘긴다.
DEFAULT_COLUMNS = {
    "title": "작업",
    "date": "날짜",
    "project": "프로젝트",
    "session": "세션ID",
}

# 본문 머리글 기본값. 실제 실행에서는 에이전트와 언어에 맞춰 넘긴다.
DEFAULT_LABELS = ("🧑 나", "🤖 Claude")

MAX_BLOCKS_PER_REQUEST = 100
MAX_TEXT_PER_BLOCK = 1900  # Notion 상한 2000 에 여유를 둔다

# append_blocks 가 받는 403 은 Notion 이 아니라 앞단 Cloudflare 의 것이다
# (본문이 JSON 에러가 아니라 `<title>Attention Required! | Cloudflare</title>` HTML).
#
# **이 한도는 WAF 대책이 아니다.** 원인을 찾는 데 세 번 틀렸고 그 중 두 번이
# 크기 가설이었다 — 고정 바이트 한도라고 봤다가(36KB 통과/66KB 실패), 문턱이
# 움직이는 걸 보고 "적응형·평판 기반"이라고 봤다(8.9KB 통과/13KB 실패).
# 둘 다 틀렸다. 실제 원인은 요청 본문에 `<script` 가 들어 있는지 하나뿐이고,
# 큰 배치가 더 자주 막힌 건 그 블록을 포함할 확률이 높았기 때문이다.
# 자세한 실측 규칙은 DESIGN.md §5 에 있다.
#
# 그런데도 이 예산을 남겨두는 진짜 이유는 둘이다.
#   (1) 배치가 작아야 _append_chunk 의 반쪽 쪼개기가 문제 블록을 몇 번 만에
#       분리해 낸다. 100블록짜리 배치에서 나쁜 블록 하나를 찾으려면 더 깊이
#       내려가야 하고 그만큼 요청이 늘어난다.
#   (2) Notion 자체 한도(~190KB 에서 진짜 JSON 413 "Request body too large.")
#       에 여유 있게 못 미치게 한다. 이건 실재하는 별개의 한도다.
MAX_BYTES_PER_REQUEST = 8 * 1024

# Notion 은 통합당 초당 약 3요청까지 허용하고, 넘으면 429 가 아니라 403 이 오기도
# 한다. 실제로 clear_body 의 DELETE 100연발이 403 을 유발했다 (2026-09-03).
# 페이싱의 근거는 이 Notion 자체 레이트리밋이지 Cloudflare 가 아니다.
#
# 1.5초였던 것을 0.5초로 낮춘다. 그 값은 "때릴수록 문턱이 좁아진다"는 (반증된)
# 적응형 가설이 근거의 절반이었고, 남은 근거인 Notion 한도에는 0.5초(초당 2회)
# 로도 여유가 있다. 낮춰야 하는 이유는 벽시계다: 거부 블록 m 개짜리 배치는
# (3m-1) 요청이 필요한데, 1.5초 페이싱이면 m=12 에 52초라 훅 예산(async,
# settings.json 의 timeout)을 넘겨 SIGKILL 된다. 그러면 블록 일부가 올라간 채
# 워터마크가 저장되지 않아 다음 발화가 같은 배치를 다시 올린다 — 2026-09-04
# 카운터 대신 시계가 당기는 같은 모양의 사고다.
MIN_REQUEST_INTERVAL = 0.5

# 차단을 만나 청크를 쪼개기 전에 잠깐 쉰다. 원인이 크기가 아니라 내용으로
# 밝혀진 지금, 이 백오프에 확인된 메커니즘은 없다 — 같은 블록은 얼마를 쉬든
# 똑같이 막힌다. 그래도 남겨두는 건 값이 싸고(차단당 1.5초, 정상 경로에는
# 전혀 영향 없음) 연타로 평판을 깎을 이유도 없기 때문이다. 지우고 싶어지면
# 지워도 되는 코드다. 페이싱과 같은 이유로 0.5초까지 낮춘다 — 확인된 효과가
# 없는 대기가 훅 예산을 잡아먹는 것이 실제 손해다.
CLOUDFLARE_BACKOFF_SECONDS = 0.5

# 시도 횟수 상한. 상대가 끝까지 막아도 append_blocks 가 반드시 종료하게 한다.
#
# 이 값이 배치 크기와 무관한 고정값이면 정상 동작에서 터진다. 쪼개기의 최악
# 비용은 블록 n 개에 대해 정확히 3n-1 요청이다 (완전 이진 분할의 노드 2n-1 개
# + 거부된 잎마다 마커 PATCH 1회). 고정 40 이면 블록 14개(메시지 7개)부터
# 초과한다 — 프런트엔드 세션이 흔히 넘는 규모다. 그리고 상한에 닿아 예외가
# 나면 그 배치의 일부 블록은 **이미 페이지에 올라간 뒤**라서, 호출자가
# 워터마크를 저장하지 못하고 다음 발화가 같은 배치를 통째로 다시 올린다 —
# 턴마다 블록이 중복되며 영원히 수렴하지 않는다 (실제로 재현됐다).
#
# 그래서 예산은 입력에 비례해 잡는다. 쪼개기는 잎(블록 1개)에서 원리상 끝나므로
# 3n 예산은 정상 동작을 절대 자르지 않고, 그러면서도 구현이 예상과 다르게 돌 때의
# 폭주는 여전히 막는다. MAX_APPEND_ATTEMPTS 는 작은 배치를 위한 **하한**이다.
MAX_APPEND_ATTEMPTS = 40

# 블록 1개짜리 요청까지 Cloudflare 에 막히면(바로 위 주석의 "바닥") 더 쪼갤
# 곳이 없다. 예전엔 여기서 그대로 raise 했는데, 그러면 sync 가 중단되고
# 워터마크가 저장되지 않아 그 세션은 영원히 수렴하지 않았다(실제로 발생했던
# 사고). 그렇다고 필터가 문자열을 못 알아보게 원문을 변형하는 건 하지 않기로
# 사용자와 합의했다(제로폭 문자, 문자열 쪼개기, 엔티티 인코딩 다 대상 밖).
# 그래서 내용은 그대로 두고, 이 블록을 마커로 통째로 대체해 한 번만 더
# 보낸다. 세션 전체 원문은 session.md 로 페이지에 첨부되므로 마커는
# "어디서 원문을 찾을 수 있는지"만 알려주면 된다.
REFUSED_MARKER = TEXT["ko"]["marker"]


class Notion:
    def __init__(self, token, db_id, session=None, timeout=NOTION_TIMEOUT,
                 pace=None, sleep=None, clock=None, marker=REFUSED_MARKER):
        self.db_id = db_id
        # 거부된 블록 자리에 넣는 문구. 언어별로 다르다.
        self.marker = marker
        self.timeout = timeout
        # pace 를 인자로 받되 기본값은 호출 시점의 모듈 상수를 읽는다.
        # def 기본값으로 박아두면 테스트가 상수를 바꿔도 반영되지 않는다.
        self.pace = MIN_REQUEST_INTERVAL if pace is None else pace
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._last_request = None
        self._append_attempts = 0
        self._append_budget = MAX_APPEND_ATTEMPTS
        # 제목이 WAF 에 걸려 대체 제목으로 바뀐 횟수. 첨부 갱신을 유발한다.
        self.title_refused = 0
        self.http = session if session is not None else requests.Session()
        self.http.headers.update({
            "Authorization": "Bearer {}".format(token),
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        # 마커로 대체된 블록 수. sync 가 이 값을 읽어 error.log 에 남기고
        # session.md 첨부 갱신을 유발한다.
        self.refused = 0

    # --- 요청 간격 조절 -------------------------------------------------
    def _wait_turn(self):
        """직전 요청과 최소 self.pace 초를 벌린다. 첫 요청은 기다리지 않는다."""
        if self.pace <= 0:
            return
        if self._last_request is not None:
            # 이미 흘러간 시간(네트워크 왕복 등)은 간격에 포함시킨다.
            gap = self.pace - (self._clock() - self._last_request)
            if gap > 0:
                self._sleep(gap)
        self._last_request = self._clock()

    def _post(self, url, json=None):
        self._wait_turn()
        return self.http.post(url, json=json, timeout=self.timeout)

    def _patch(self, url, json=None):
        self._wait_turn()
        return self.http.patch(url, json=json, timeout=self.timeout)

    def _get(self, url, params=None):
        self._wait_turn()
        return self.http.get(url, params=params, timeout=self.timeout)

    def _delete(self, url):
        self._wait_turn()
        return self.http.delete(url, timeout=self.timeout)

    # --- API ------------------------------------------------------------
    def find_page(self, session_id, prop_name):
        """세션ID 속성으로 기존 행을 찾는다. 없으면 None."""
        response = self._post(
            "{}/databases/{}/query".format(API, self.db_id),
            json={
                "filter": {"property": prop_name, "rich_text": {"equals": session_id}},
                "page_size": 1,
            },
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        return results[0]["id"] if results else None

    def create_page(self, props, fallback_props=None):
        """행을 만든다. 제목이 WAF 에 걸리면 fallback_props 로 한 번 재시도한다."""
        response = self._post(
            "{}/pages".format(API),
            json={"parent": {"database_id": self.db_id}, "properties": props},
        )
        response = self._retry_props(
            response, fallback_props,
            lambda retry: self._post(
                "{}/pages".format(API),
                json={"parent": {"database_id": self.db_id}, "properties": retry},
            ),
        )
        response.raise_for_status()
        return response.json()["id"]

    def update_props(self, page_id, props, fallback_props=None):
        """속성을 갱신한다. 제목이 WAF 에 걸리면 fallback_props 로 한 번 재시도한다."""
        response = self._patch(
            "{}/pages/{}".format(API, page_id),
            json={"properties": props},
        )
        response = self._retry_props(
            response, fallback_props,
            lambda retry: self._patch(
                "{}/pages/{}".format(API, page_id),
                json={"properties": retry},
            ),
        )
        response.raise_for_status()

    def _retry_props(self, response, fallback_props, send):
        """속성 요청이 Cloudflare 에 막혔으면 대체 속성으로 한 번만 다시 보낸다.

        제목은 트랜스크립트에서 온다 — aiTitle 이 없으면 첫 사용자 프롬프트의
        첫 줄이다. 그 줄에 `<script` 가 들어 있으면(프런트엔드 세션에서 가장
        흔한 문자열이다) 이 요청이 그 세션의 **첫 API 호출**부터 403 을 받는다.
        캐시가 있는 경로에서는 update_props 가 append 보다 먼저 돌므로, 막히면
        그 세션은 영원히 아무것도 기록되지 않는다 — 마커 장치는 블록 append
        경로에만 있어서 여기까지 닿지도 못한다.

        그래서 REFUSED_MARKER 와 같은 수를 쓴다: 원문을 변형해 필터를
        통과시키는 게 아니라, 대화에서 온 제목을 **통째로 버리고** 대화가 한
        글자도 섞이지 않은 제목(transcript.fallback_title)으로 갈아끼운다.
        원문은 첨부 session.md 에 그대로 남는다.
        """
        if fallback_props is None or not _is_cloudflare_block(response):
            return response
        self._sleep(CLOUDFLARE_BACKOFF_SECONDS)
        self.title_refused += 1
        return send(fallback_props)

    def append_blocks(self, page_id, blocks):
        """한 요청에 100개까지만 보낼 수 있으므로 잘라서 여러 번 보낸다.

        정상 경로에서는 batches() 가 이미 메시지 단위로 100개 이하 & 8KB 이하로
        만들어 주므로 여기서 쪼개지는 일이 없다. 한 메시지가 홀로 한도를 넘는
        병적인 경우(초장문 응답)를 위한 안전망이 블록 100개 단위 분할이고,
        그렇게 나눈 청크가 Cloudflare 에 막히면 _append_chunk 가 **블록 수 기준**
        으로 반씩 쪼개 문제 블록을 분리해 낸다 (바이트가 아니다 — 크기는 원인이
        아니므로 쪼개기의 목적도 요청을 줄이는 게 아니라 나쁜 블록 하나를
        이웃과 떼어놓는 것이다).
        """
        self._append_attempts = 0
        # 예산을 입력에 비례해 잡는 이유는 MAX_APPEND_ATTEMPTS 주석에 있다.
        # 최악 비용이 3n-1 이므로 3n 이면 정상 동작을 자르지 않는다.
        self._append_budget = max(MAX_APPEND_ATTEMPTS, 3 * len(blocks))
        for start in range(0, len(blocks), MAX_BLOCKS_PER_REQUEST):
            chunk = blocks[start:start + MAX_BLOCKS_PER_REQUEST]
            self._append_chunk(page_id, chunk)

    def _spend_attempt(self, page_id):
        """요청 한 번을 예산에서 깎는다. 바닥나면 예외."""
        self._append_attempts += 1
        if self._append_attempts > self._append_budget:
            raise RuntimeError(
                "Cloudflare 차단이 {}회 재시도 뒤에도 풀리지 않았다 (page={})".format(
                    self._append_budget, page_id
                )
            )

    def _append_chunk(self, page_id, chunk):
        """chunk 하나를 보낸다. Cloudflare 차단이면 반으로 쪼개 재귀적으로 재시도한다.

        진짜 Notion 403(JSON 에러, 예: 권한 부족)은 재시도해 봤자 결과가 같으므로
        raise_for_status() 로 즉시 올린다 — 재시도는 Cloudflare 의 봇 차단(HTML
        403)에만 쓴다. 쪼개기는 블록 1개(바닥)까지 내려가며, 거기서도 Cloudflare
        에 막히면 그 블록을 REFUSED_MARKER 로 대체해 한 번 더 보낸다 — 마커까지
        막히면 그건 오탐이 아니라 다른 문제이므로 그대로 올린다.

        마커 PATCH 도 예산에서 깎는다. 예산은 그것까지 계산에 넣은 3n 이므로
        정상 동작에서 바닥나지 않고, 안 깎으면 거부 블록 하나당 요청 2회를
        내면서 1회로 청구해 상한이 실제 요청 수를 못 묶는다.
        """
        if not chunk:
            return

        self._spend_attempt(page_id)

        response = self._patch(
            "{}/blocks/{}/children".format(API, page_id),
            json={"children": chunk},
        )

        if not _is_cloudflare_block(response):
            response.raise_for_status()
            return

        if len(chunk) <= 1:
            # 블록 1개짜리 요청까지 막히면 쪼개서 해결할 문제가 아니다. 원문을
            # 바꾸지 않고 마커로 통째로 대체해 한 번만 더 보낸다 — sync 를
            # 중단시키지 않으려는 것뿐이지 필터 우회가 아니다.
            self._spend_attempt(page_id)
            marker_response = self._patch(
                "{}/blocks/{}/children".format(API, page_id),
                json={"children": [_paragraph(self.marker)]},
            )
            # 마커까지 Cloudflare 에 막힌다면(혹은 다른 이유로 실패한다면) 그건
            # 오탐 하나가 아니라 다른 문제이므로 예외를 그대로 내보낸다.
            marker_response.raise_for_status()
            self.refused += 1
            return

        self._sleep(CLOUDFLARE_BACKOFF_SECONDS)
        mid = len(chunk) // 2
        self._append_chunk(page_id, chunk[:mid])
        self._append_chunk(page_id, chunk[mid:])

    def clear_body(self, page_id):
        """페이지 본문 블록을 전부 지운다. 캐시 유실 복구 경로에서만 쓴다."""
        while True:
            response = self._get(
                "{}/blocks/{}/children".format(API, page_id),
                params={"page_size": 100},
            )
            response.raise_for_status()
            data = response.json()
            children = data.get("results") or []
            for block in children:
                # 삭제 실패를 삼키면 루프가 "다 지웠다"고 믿고 끝나 본문이 남는다.
                # 실패하면 예외를 내보내 worklog 가 error.log 에 남기고,
                # 다음 Stop 에서 (캐시가 여전히 비어 있으므로) 처음부터 다시 시도한다.
                deleted = self._delete("{}/blocks/{}".format(API, block["id"]))
                deleted.raise_for_status()
            if not data.get("has_more") or not children:
                return

    def list_children(self, page_id):
        """페이지 본문 블록을 전부 돌려준다. 결과를 모르는 append 를 대조할 때만 쓴다."""
        blocks = []
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            response = self._get(
                "{}/blocks/{}/children".format(API, page_id), params=params,
            )
            response.raise_for_status()
            data = response.json()
            blocks.extend(data.get("results") or [])
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return blocks

    def me(self):
        """토큰 확인용. 토큰이 틀리면 예외."""
        response = self._get("{}/users/me".format(API))
        response.raise_for_status()
        return response.json()

    def get_database(self, database_id):
        """데이터베이스 하나를 읽는다. setup 이 열 구성을 확인할 때 쓴다."""
        response = self._get("{}/databases/{}".format(API, database_id))
        response.raise_for_status()
        return response.json()

    def add_properties(self, database_id, properties):
        """데이터베이스에 열을 더한다. 이미 있는 열은 건드리지 않는다."""
        response = self._patch(
            "{}/databases/{}".format(API, database_id),
            json={"properties": properties},
        )
        response.raise_for_status()
        return response.json()

    def upload_file(self, filename, data, content_type="text/markdown"):
        """파일을 Notion 에 올리고 file_upload id 를 돌려준다.

        data 는 bytes. 세션 전체 원문(session.md)을 첨부하는 데 쓴다 — 마커로
        대체된 블록의 원문도 여기엔 그대로 담긴다(page 본문과 달리 파일 업로드는
        Cloudflare 의 <script 오탐을 타지 않는다는 걸 실측으로 확인했다).
        """
        response = self._post(
            "{}/file_uploads".format(API),
            json={"filename": filename, "content_type": content_type},
        )
        response.raise_for_status()
        payload = response.json()

        # self.http.post 를 직접 쓴다: 이 요청만 files= 로 multipart 를 보내야
        # 하고, self.http.headers 에 박힌 Content-Type: application/json 을
        # 떨어뜨려야 한다(아래 참고) — 기존 _post 는 json= 고정이라 맞지 않는다.
        # 페이싱 규칙(_wait_turn)은 그대로 지킨다.
        self._wait_turn()
        upload_response = self.http.post(
            payload["upload_url"],
            files={"file": (filename, data, content_type)},
            # requests 는 값이 None 인 헤더를 보내지 않고 제거한다 — 세션에
            # 박힌 Content-Type: application/json 을 이렇게 떨어뜨리지 않으면
            # multipart 경계(boundary)가 깨진다. Authorization 과
            # Notion-Version 은 세션 헤더 그대로 실려 나간다.
            headers={"Content-Type": None},
            timeout=self.timeout,
        )
        upload_response.raise_for_status()
        return payload["id"]

    def attach_file(self, page_id, file_upload_id, name="session.md"):
        """업로드한 파일을 페이지에 file 블록으로 붙이고 그 블록 id 를 돌려준다."""
        response = self._patch(
            "{}/blocks/{}/children".format(API, page_id),
            json={"children": [{
                "object": "block", "type": "file",
                "file": {
                    "type": "file_upload",
                    "file_upload": {"id": file_upload_id},
                    "name": name,
                },
            }]},
        )
        response.raise_for_status()
        return response.json()["results"][0]["id"]

    def delete_block(self, block_id):
        """블록 하나를 지운다. 첨부 갱신 때 이전 file 블록을 치우는 데 쓴다."""
        response = self._delete("{}/blocks/{}".format(API, block_id))
        response.raise_for_status()


def _is_cloudflare_block(response):
    """403 인데 본문이 Notion 의 JSON 에러가 아니라 Cloudflare 의 봇 차단 HTML 인지.

    진짜 Notion 403(권한 부족 등)은 `{"object": "error", "code": ..., ...}` 형태의
    JSON 을 돌려준다. Cloudflare 가 앞단에서 막을 때는 `<title>Attention
    Required! | Cloudflare</title>` 를 담은 HTML 페이지를 돌려준다 — 본문이
    JSON 이 아니라는 사실 자체가 이미 신호지만, 오탐을 줄이려고 "Cloudflare"나
    `<!DOCTYPE html>` 문자열이 실제로 들어있는지까지 확인한다.
    """
    if getattr(response, "status_code", None) != 403:
        return False
    text = getattr(response, "text", "") or ""
    if not text:
        return False
    lowered = text.lower()
    return "cloudflare" in lowered or "<!doctype html>" in lowered


def _rich_text(content):
    return [{"type": "text", "text": {"content": content}}]


def _paragraph(content):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(content)}}


def _heading(content):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": _rich_text(content)}}


def _chunks(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def build_props(title, project, when, session_id, agent=None, columns=None):
    """속성 dict. columns 는 역할(title/date/project/session/agent) → 열 이름.

    agent 열은 columns 에 있고 agent 값이 있을 때만 채운다.
    """
    columns = columns or DEFAULT_COLUMNS
    props = {
        # transcript.TITLE_MAX 가 이미 100자로 자르므로 지금은 발동하지 않지만,
        # build_props 는 transcript 를 거치지 않는 호출자도 쓸 수 있는 공개 빌더라
        # 바로 아래 project 와 같은 이유로 Notion 상한 방어를 남겨둔다.
        columns["title"]: {"title": _rich_text(title[:MAX_TEXT_PER_BLOCK])},
        columns["project"]: {"select": {"name": project[:100]}},
        columns["date"]: {"date": {"start": when.isoformat()}},
        columns["session"]: {"rich_text": _rich_text(session_id)},
    }
    if agent and columns.get("agent"):
        props[columns["agent"]] = {"select": {"name": agent}}
    return props


def build_blocks(messages, labels=None):
    """[{"role","text","uuid"}] → Notion 블록 목록. labels 는 (사람, 에이전트) 머리글."""
    user_label, agent_label = labels or DEFAULT_LABELS
    blocks = []
    for message in messages:
        label = user_label if message["role"] == "user" else agent_label
        blocks.append(_heading(label))
        for chunk in _chunks(message["text"], MAX_TEXT_PER_BLOCK):
            blocks.append(_paragraph(chunk))
    return blocks


def _block_text(block):
    body = block.get(block.get("type")) or {}
    return "".join(
        (rt.get("text") or {}).get("content", "") for rt in body.get("rich_text") or []
    )


def same_block(on_page, built, marker=REFUSED_MARKER):
    """페이지에서 읽은 블록이 build_blocks 가 만든 블록과 같은지.

    마커로 대체된 블록은 무엇과도 같다고 본다 — 거부된 블록 자리에는 원래
    블록 대신 마커가 1:1 로 올라가기 때문이다.
    """
    text = _block_text(on_page)
    if on_page.get("type") == "paragraph" and text == marker:
        return True
    return on_page.get("type") == built["type"] and text == _block_text(built)


def _payload_bytes(blocks):
    r"""batches() 의 배치 하나가 실제 요청 바디로 나갈 때의 바이트 수.

    append_blocks 가 보내는 것과 똑같이 {"children": blocks} 를 json.dumps 로
    직렬화해 UTF-8 로 인코딩한 길이를 잰다 — 이게 실제로 wire 에 나가는 바이트
    수다. len(text) 로 문자 수를 재면 한글이 든 텍스트를 크게 과소평가해
    MAX_BYTES_PER_REQUEST 를 조용히 넘긴다 — requests 는 ensure_ascii=True 로
    직렬화하므로 한글 한 글자가 \uXXXX, 즉 6바이트로 나가기 때문이다.
    """
    return len(json.dumps({"children": blocks}).encode("utf-8"))


def batches(messages, limit=MAX_BLOCKS_PER_REQUEST, byte_limit=MAX_BYTES_PER_REQUEST,
            skip=0, labels=None):
    """메시지를 **통째로** 묶어 (블록목록, 마지막 uuid) 를 순서대로 내놓는다.

    배치 경계를 블록이 아니라 메시지에 두는 것이 핵심이다. build_blocks 는 메시지
    하나당 heading 1 + paragraph N 을 내므로 100번째 블록은 거의 항상 메시지 중간에
    떨어지고, 그 지점에는 저장할 uuid 가 없다. 워터마크를 배치마다 저장하려면
    배치가 항상 메시지 경계에서 끝나야 한다.

    배치는 블록 수(limit)와 바이트 수(byte_limit) 둘 중 **먼저 닿는 쪽**에서
    끊긴다. 블록 수 100 은 Notion 자체 한도다. 바이트 수 8KB 는 WAF 대책이
    아니라 (원인은 크기가 아니었다 — 상수 주석과 DESIGN.md §5 참고) 배치를
    작게 유지해 _append_chunk 의 쪼개기가 문제 블록을 빨리 분리하게 하고,
    Notion 자체의 ~190KB 한도에 여유를 두기 위한 값이다. 메시지가 작을 땐
    블록 수가 먼저 닿고, 메시지가 (특히 한글로) 길면 바이트 수가 먼저 닿는다 —
    어느 쪽이든 같은 방식으로 배치를 끊는다.

    한 메시지가 홀로 limit 이나 byte_limit 을 넘으면 쪼갤 곳이 없으므로 그
    메시지 하나로 배치를 만든다. 그 배치는 한도를 넘긴 채로 나가고,
    append_blocks 가 요청 단위(블록 100개씩, 그래도 Cloudflare 에 막히면
    블록 수 기준 반쪽씩)로 다시 나눠 보낸다 — 조용히 버리거나 무한 루프에
    빠지는 대신, 손해가 더 큰 걸 알면서도 일단 내보낸다.

    skip 은 이미 페이지에 올라간 앞쪽 블록 수다 (결과를 모르던 append 가 실제로는
    반영됐던 경우). 그 블록들은 빼고 보내되 워터마크는 그대로 넘긴다 — 전부
    빠져도 빈 배치를 내놓아야 호출자가 워터마크를 저장할 수 있다.
    """
    current = []
    last_uuid = None
    for message in messages:
        blocks = build_blocks([message], labels)
        if skip:
            dropped = min(skip, len(blocks))
            blocks = blocks[dropped:]
            skip -= dropped
        if current and (
            len(current) + len(blocks) > limit
            or _payload_bytes(current + blocks) > byte_limit
        ):
            yield current, last_uuid
            current = []
        current.extend(blocks)
        last_uuid = message["uuid"]
    if last_uuid is not None:
        yield current, last_uuid
