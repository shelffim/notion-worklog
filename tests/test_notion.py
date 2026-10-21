import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notion_worklog import config
from notion_worklog import notion
from notion_worklog.agents import claude as transcript

KST = timezone(timedelta(hours=9))
FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, payload, status=200, text=None):
        self._payload = payload
        self.status_code = status
        # 실제 requests.Response.text 대역. 기본은 payload 를 JSON 으로 직렬화한
        # 것 — 진짜 Notion 에러 응답의 모양(JSON 본문)을 흉내낸다. Cloudflare
        # 차단을 흉내낼 때는 호출자가 HTML 문자열을 명시적으로 넘긴다.
        self.text = json.dumps(payload) if text is None else text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP {}".format(self.status_code))


class FakeSession:
    """requests.Session 대역. 호출 기록을 남기고 미리 정한 응답을 돌려준다."""

    def __init__(self, responses=None):
        self.headers = {}
        self.calls = []
        self.payloads = []
        # upload_file 2단계(multipart)만 남기는 별도 기록. files/headers 는
        # json 바디가 없어 payloads 에는 (POST, url, None) 으로만 남고 그걸로는
        # multipart 인자가 실제로 넘어갔는지 확인할 수 없다.
        self.file_posts = []
        self.responses = responses or {}
        self._response_cursors = {}

    def _reply(self, method, url):
        self.calls.append((method, url))
        key = (method, url)
        response = self.responses.get(key) or FakeResponse({})

        # 리스트 응답을 지원: 첫 호출부터 순서대로 pop 하다가, 마지막 원소는 계속 돌려준다
        if isinstance(response, list):
            if key not in self._response_cursors:
                self._response_cursors[key] = 0
            cursor = self._response_cursors[key]
            result = response[min(cursor, len(response) - 1)]
            if cursor < len(response) - 1:
                self._response_cursors[key] += 1
            return result
        return response

    def post(self, url, json=None, timeout=None, files=None, headers=None):
        self.payloads.append(("POST", url, json))
        if files is not None or headers is not None:
            self.file_posts.append({"url": url, "files": files, "headers": headers})
        return self._reply("POST", url)

    def patch(self, url, json=None, timeout=None):
        self.payloads.append(("PATCH", url, json))
        return self._reply("PATCH", url)

    def get(self, url, params=None, timeout=None):
        return self._reply("GET", url)

    def delete(self, url, timeout=None):
        return self._reply("DELETE", url)


BASE = "https://api.notion.com/v1"
DB = "db-123"


def test_sets_auth_and_version_headers():
    fake = FakeSession()
    notion.Notion("tok-abc", DB, session=fake)

    assert fake.headers["Authorization"] == "Bearer tok-abc"
    assert fake.headers["Notion-Version"] == config.NOTION_VERSION


def test_find_page_returns_id_when_found():
    fake = FakeSession({
        ("POST", BASE + "/databases/db-123/query"):
            FakeResponse({"results": [{"id": "page-99"}]}),
    })
    api = notion.Notion("tok", DB, session=fake)

    assert api.find_page("sess-1", "세션ID") == "page-99"


def test_find_page_returns_none_when_empty():
    fake = FakeSession({
        ("POST", BASE + "/databases/db-123/query"): FakeResponse({"results": []}),
    })
    api = notion.Notion("tok", DB, session=fake)

    assert api.find_page("sess-1", "세션ID") is None


def test_create_page_returns_new_id():
    fake = FakeSession({("POST", BASE + "/pages"): FakeResponse({"id": "page-new"})})
    api = notion.Notion("tok", DB, session=fake)

    assert api.create_page({}) == "page-new"


def test_append_blocks_splits_at_100():
    fake = FakeSession()
    api = notion.Notion("tok", DB, session=fake)

    api.append_blocks("page-1", [{"n": i} for i in range(250)])

    patches = [c for c in fake.calls if c[0] == "PATCH"]
    assert len(patches) == 3


def test_append_blocks_does_nothing_when_empty():
    fake = FakeSession()
    api = notion.Notion("tok", DB, session=fake)

    api.append_blocks("page-1", [])

    assert fake.calls == []


def test_build_props_matches_real_schema():
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)
    props = notion.build_props("제목", "my-app", when, "sess-1")

    assert props["작업"]["title"][0]["text"]["content"] == "제목"
    assert props["프로젝트"]["select"]["name"] == "my-app"
    assert props["날짜"]["date"]["start"].startswith("2026-09-02T21:30")
    assert props["세션ID"]["rich_text"][0]["text"]["content"] == "sess-1"


def test_build_blocks_labels_each_role():
    blocks = notion.build_blocks([
        {"role": "user", "text": "안녕", "uuid": "u1"},
        {"role": "assistant", "text": "네", "uuid": "u2"},
    ])
    texts = [
        rt["text"]["content"]
        for b in blocks
        for rt in b[b["type"]]["rich_text"]
    ]

    assert "🧑 나" in texts
    assert "🤖 Claude" in texts
    assert "안녕" in texts
    assert "네" in texts


def test_build_blocks_splits_long_text():
    blocks = notion.build_blocks([{"role": "user", "text": "가" * 5000, "uuid": "u1"}])
    paragraphs = [b for b in blocks if b["type"] == "paragraph"]

    assert len(paragraphs) == 3
    assert all(
        len(p["paragraph"]["rich_text"][0]["text"]["content"]) <= notion.MAX_TEXT_PER_BLOCK
        for p in paragraphs
    )


def test_clear_body_deletes_every_child():
    fake = FakeSession({
        ("GET", BASE + "/blocks/page-1/children"):
            FakeResponse({"results": [{"id": "b1"}, {"id": "b2"}], "has_more": False}),
    })
    api = notion.Notion("tok", DB, session=fake)

    api.clear_body("page-1")

    deletes = [c for c in fake.calls if c[0] == "DELETE"]
    assert len(deletes) == 2


def test_find_page_sends_rich_text_equals_filter():
    fake = FakeSession({("POST", BASE + "/databases/db-123/query"): FakeResponse({"results": []})})
    api = notion.Notion("tok", DB, session=fake)

    api.find_page("sess-1", "세션ID")

    method, url, body = fake.payloads[0]
    assert body["filter"] == {"property": "세션ID", "rich_text": {"equals": "sess-1"}}
    assert body["page_size"] == 1


def test_create_page_sends_database_parent_and_properties():
    fake = FakeSession({("POST", BASE + "/pages"): FakeResponse({"id": "page-new"})})
    api = notion.Notion("tok", DB, session=fake)

    api.create_page({"작업": {"title": []}})

    method, url, body = fake.payloads[0]
    assert body["parent"] == {"database_id": DB}
    assert body["properties"] == {"작업": {"title": []}}


def test_update_props_sends_only_properties():
    fake = FakeSession()
    api = notion.Notion("tok", DB, session=fake)

    api.update_props("page-1", {"날짜": {"date": {"start": "2026-09-03T10:00:00+09:00"}}})

    method, url, body = fake.payloads[0]
    assert url.endswith("/pages/page-1")
    assert set(body) == {"properties"}


def test_append_blocks_sends_exact_chunk_sizes():
    fake = FakeSession()
    api = notion.Notion("tok", DB, session=fake)

    api.append_blocks("page-1", [{"n": i} for i in range(250)])

    sizes = [len(body["children"]) for _, _, body in fake.payloads]
    assert sizes == [100, 100, 50]
    # 잘린 조각이 원본 순서를 그대로 유지하는지 — 누락도 중복도 없어야 한다
    sent = [b["n"] for _, _, body in fake.payloads for b in body["children"]]
    assert sent == list(range(250))


def test_clear_body_follows_pagination():
    fake = FakeSession({
        ("GET", BASE + "/blocks/page-1/children"): [
            FakeResponse({"results": [{"id": "b1"}, {"id": "b2"}], "has_more": True}),
            FakeResponse({"results": [{"id": "b3"}], "has_more": False}),
        ],
    })
    api = notion.Notion("tok", DB, session=fake)

    api.clear_body("page-1")

    deletes = [c for c in fake.calls if c[0] == "DELETE"]
    assert len(deletes) == 3


class ParamsRecordingSession(FakeSession):
    """GET 의 쿼리 파라미터까지 남긴다. 다음 쪽 커서가 실제로 실리는지 보려면 필요하다."""

    def __init__(self, responses=None):
        super().__init__(responses)
        self.get_params = []

    def get(self, url, params=None, timeout=None):
        self.get_params.append(params)
        return super().get(url, params=params, timeout=timeout)


def test_list_children_follows_the_cursor_to_the_end():
    fake = ParamsRecordingSession({
        ("GET", BASE + "/blocks/page-1/children"): [
            FakeResponse({"results": [{"id": "b1"}, {"id": "b2"}],
                          "has_more": True, "next_cursor": "c-2"}),
            FakeResponse({"results": [{"id": "b3"}],
                          "has_more": False, "next_cursor": None}),
        ],
    })
    api = notion.Notion("tok", DB, session=fake)

    blocks = api.list_children("page-1")

    assert [b["id"] for b in blocks] == ["b1", "b2", "b3"]
    assert fake.get_params[1]["start_cursor"] == "c-2"


def test_clear_body_raises_when_a_delete_fails():
    fake = FakeSession({
        ("GET", BASE + "/blocks/page-1/children"):
            FakeResponse({"results": [{"id": "b1"}], "has_more": False}),
        ("DELETE", BASE + "/blocks/b1"): FakeResponse({}, status=409),
    })
    api = notion.Notion("tok", DB, session=fake)

    try:
        api.clear_body("page-1")
    except RuntimeError:
        return
    raise AssertionError("삭제 실패가 조용히 넘어갔다")


# --- 요청 간격 --------------------------------------------------

class FakeClock:
    """수동 시계. sleep 이 부른 만큼만 흐른다."""

    def __init__(self, step=0.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


def test_requests_are_paced_so_a_burst_cannot_exceed_the_rate_limit():
    # 100블록 3연발이 간격 없이 나가면 Notion 이 403 을 돌려준다 (2026-09-03 사고)
    fake = FakeSession()
    clock = FakeClock()
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    api = notion.Notion("tok", DB, session=fake, pace=0.35, sleep=sleep, clock=clock)
    api.append_blocks("page-1", [{"n": i} for i in range(250)])

    assert len(fake.calls) == 3
    # 첫 요청은 기다리지 않는다. 나머지는 매번 간격만큼 벌린다.
    assert len(slept) == 2
    assert all(abs(s - 0.35) < 1e-9 for s in slept)


def test_pacing_counts_time_that_already_passed():
    # 네트워크 왕복만으로 간격이 채워졌으면 더 자지 않는다
    fake = FakeSession()
    api = notion.Notion(
        "tok", DB, session=fake, pace=0.35,
        sleep=lambda s: (_ for _ in ()).throw(AssertionError("자면 안 된다")),
        clock=FakeClock(step=1.0),
    )

    api.append_blocks("page-1", [{"n": i} for i in range(250)])

    assert len(fake.calls) == 3


def test_pacing_also_covers_the_delete_burst_of_clear_body():
    # 사고의 실제 원인은 clear_body 의 DELETE 연발이었다
    fake = FakeSession({
        ("GET", BASE + "/blocks/page-1/children"):
            FakeResponse({"results": [{"id": "b1"}, {"id": "b2"}], "has_more": False}),
    })
    clock = FakeClock()
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    api = notion.Notion("tok", DB, session=fake, pace=0.35, sleep=sleep, clock=clock)
    api.clear_body("page-1")

    # GET + DELETE + DELETE = 3요청, 그 사이 간격 2번
    assert len(slept) == 2


def test_default_pace_comes_from_the_module_constant(monkeypatch):
    # 기본값을 def 인자에 박아두면 상수를 바꿔도 반영되지 않는다
    monkeypatch.setattr(notion, "MIN_REQUEST_INTERVAL", 0.42)

    api = notion.Notion("tok", DB, session=FakeSession())

    assert api.pace == 0.42


def test_production_pace_stays_under_three_requests_per_second(production_pace):
    assert production_pace >= 1.0 / 3.0


def test_no_retry_on_403():
    # 본문이 JSON 이 아닌(=Cloudflare 로 식별되지 않는) 403 은 재시도하지 않는다.
    # 진짜 권한 오류면 재시도해 봤자 호출 수만 태우고 결과는 같다.
    fake = FakeSession({
        ("PATCH", BASE + "/blocks/page-1/children"): FakeResponse({}, status=403),
    })
    api = notion.Notion("tok", DB, session=fake)

    try:
        api.append_blocks("page-1", [{"n": 1}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("403 이 조용히 넘어갔다")

    assert len(fake.calls) == 1


# --- Cloudflare 적응형 백오프 -----------------------------------
#
# 30KB 고정 예산은 잘못된 가정 위에 있었다: 실측을 반복하자 같은 페이지에서
# 문턱이 36KB 통과/66KB 실패에서 8.9KB 통과/13KB 실패로 좁아졌다 — Cloudflare
# 가 이 클라이언트를 "때릴수록 더 의심"하는 적응형·평판 기반 방어라는 뜻이다.
# 그래서 고정 바이트 예산만으로는 절대 못 맞히고, append_blocks 스스로 403 을
# 감지해 청크를 반으로 쪼개고 잠깐 쉬었다가 재시도해야 한다. 단, 진짜 Notion
# JSON 403(권한 부족 등)까지 재시도하면 안 된다 — 결과가 같은 호출을 태우기만
# 한다.
#
# 아래 테스트들은 이 픽스 이전 코드(403 이면 무조건 즉시 raise)에서는 전부
# 실패한다.

class CloudflareFakeSession:
    """블록 수가 threshold 를 넘는 PATCH 만 Cloudflare 봇 차단(HTML 403)으로
    막고, 그 이하는 성공시킨다. append_blocks 가 실제로 청크를 쪼개 재시도하는지
    확인하려면 FakeSession 처럼 URL 로 고정된 응답이 아니라 매 호출의 청크
    크기를 봐야 하므로 별도로 둔다."""

    CLOUDFLARE_HTML = "<!DOCTYPE html><title>Attention Required! | Cloudflare</title>"

    def __init__(self, threshold):
        self.headers = {}
        self.threshold = threshold
        self.attempts = 0
        self.delivered = []  # 실제로 200 을 받아 "전달된" 청크들, 순서대로

    def patch(self, url, json=None, timeout=None):
        self.attempts += 1
        chunk = json["children"]
        if len(chunk) > self.threshold:
            return FakeResponse({}, status=403, text=self.CLOUDFLARE_HTML)
        self.delivered.append(chunk)
        return FakeResponse({"object": "list"}, status=200)

    def post(self, url, json=None, timeout=None):
        raise NotImplementedError("이 테스트들은 append_blocks 만 사용한다")

    def get(self, url, params=None, timeout=None):
        raise NotImplementedError("이 테스트들은 append_blocks 만 사용한다")

    def delete(self, url, timeout=None):
        raise NotImplementedError("이 테스트들은 append_blocks 만 사용한다")


def test_cloudflare_block_splits_and_delivers_every_block_exactly_once_in_order():
    # threshold=8: 8블록을 넘는 청크는 Cloudflare 에 막힌다. append_blocks 가
    # 스스로 반으로 쪼개 재시도해서 결국 37블록 전부를 순서대로, 중복도 누락도
    # 없이 전달해야 한다.
    fake = CloudflareFakeSession(threshold=8)
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)
    blocks = [{"n": i} for i in range(37)]

    api.append_blocks("page-1", blocks)

    assert len(fake.delivered) > 1  # 실제로 쪼개졌는지
    assert all(len(chunk) <= 8 for chunk in fake.delivered)
    delivered = [b for chunk in fake.delivered for b in chunk]
    assert delivered == blocks


def test_genuine_notion_403_raises_without_retry():
    # 진짜 Notion 권한 오류는 JSON 바디(object: "error", code: ...)를 돌려준다.
    # Cloudflare HTML 이 아니므로 즉시 올라가야 하고, 재시도로 호출 수를
    # 태우면 안 된다.
    body = {
        "object": "error", "status": 403, "code": "restricted_resource",
        "message": "이 통합은 이 리소스에 대한 접근 권한이 없습니다.",
    }
    fake = FakeSession({
        ("PATCH", BASE + "/blocks/page-1/children"): FakeResponse(body, status=403),
    })
    slept = []
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: slept.append(s))

    try:
        api.append_blocks("page-1", [{"n": i} for i in range(5)])
    except RuntimeError:
        pass
    else:
        raise AssertionError("진짜 Notion 403 이 조용히 넘어갔다")

    assert len(fake.calls) == 1
    assert slept == []  # 재시도도, 그 앞의 백오프 sleep 도 없어야 한다


def test_single_block_cloudflare_block_raises_instead_of_looping():
    # 블록 1개짜리 청크까지 Cloudflare 에 막히면 더 쪼갤 곳이 없다 — 무한 루프
    # 대신 원문 대신 마커로 한 번 더 보내보고(Part A), 그마저 막히면 포기하고
    # 올려야 한다. CloudflareFakeSession(threshold=0) 은 크기와 무관하게 항상
    # 막으므로 마커 요청도 막혀 결국 raise 한다 — 시도 횟수는 원본 1회 +
    # 마커 1회 = 2회.
    fake = CloudflareFakeSession(threshold=0)  # 어떤 크기도 통과 못 함
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    try:
        api.append_blocks("page-1", [{"n": 1}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("블록 1개짜리 Cloudflare 차단이 조용히 넘어갔다")

    assert fake.attempts == 2  # 원본 시도 + 마커 대체 시도, 그 이상 재귀하지 않는다


class AlwaysBlockingSession(CloudflareFakeSession):
    """마커까지 포함해 **모든** PATCH 를 Cloudflare 차단으로 막는다."""

    def patch(self, url, json=None, timeout=None):
        self.attempts += 1
        return FakeResponse({}, status=403, text=self.CLOUDFLARE_HTML)


def test_retry_cap_terminates_when_even_markers_are_blocked():
    # 상한의 진짜 역할은 "상대가 끝까지 막아도 반드시 종료한다"이다. 마커까지
    # 막히면 _append_chunk 는 raise_for_status() 로 즉시 올리므로 예산에 닿기도
    # 전에 멈추지만, 어느 쪽이든 무한히 두드리지 않는다는 것이 요구사항이다.
    fake = AlwaysBlockingSession(threshold=0)
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    try:
        api.append_blocks("page-1", [{"n": i} for i in range(50)])
    except Exception:
        pass
    else:
        raise AssertionError("전부 막히는데도 예외 없이 끝났다")

    # 마커까지 막히면 첫 잎에서 raise 하므로 요청은 극소수여야 한다. 넉넉한
    # 상한(3n)만 확인하면 예산 검사를 통째로 없애도 이 테스트가 통과한다.
    assert fake.attempts <= 2 * 7   # 50블록을 잎까지 내려가는 깊이(약 6) + 마커


def test_budget_is_proportional_to_the_batch_size():
    """예산이 입력에 비례하는지 직접 확인한다. 고정값으로 되돌리면 여기서 걸린다."""
    api = notion.Notion("tok", DB, session=FakeSession(), sleep=lambda s: None)
    api.append_blocks("page-1", [{"n": i} for i in range(200)])
    assert api._append_budget == 3 * 200

    api.append_blocks("page-1", [{"n": 0}])          # 작은 배치에는 하한이 걸린다
    assert api._append_budget == notion.MAX_APPEND_ATTEMPTS


def test_full_refusal_batch_completes_without_blowing_the_budget():
    """거부 블록이 많은 배치가 예외 없이 끝나야 한다.

    이것이 실제로 재현됐던 사고의 회귀 테스트다. 예산이 배치 크기와
    무관한 고정 40 이던 시절, 블록 24개짜리 배치(메시지 12개가 각각 <script 를
    포함)는 41번째 시도에서 터졌다 — 그런데 그 시점엔 이미 19개 블록이 페이지에
    올라간 뒤였다. 호출자는 예외를 받아 워터마크를 저장하지 못하고, 다음 발화가
    같은 배치를 통째로 다시 올려 턴마다 블록이 중복되며 영원히 수렴하지 않았다.

    쪼개기의 최악 비용은 정확히 3n-1 이므로(완전 이진 분할 노드 2n-1 + 거부된
    잎마다 마커 1회), 예산 3n 이면 이 경우가 반드시 완주한다.
    """
    # threshold=0 이면 모든 원본 청크가 막히고, 마커만 통과한다.
    class MarkerOnlySession(CloudflareFakeSession):
        def patch(self, url, json=None, timeout=None):
            self.attempts += 1
            chunk = json["children"]
            is_marker = (
                len(chunk) == 1
                and chunk[0].get("paragraph", {}).get("rich_text", [{}])[0]
                .get("text", {}).get("content") == notion.REFUSED_MARKER
            )
            if not is_marker:
                return FakeResponse({}, status=403, text=self.CLOUDFLARE_HTML)
            self.delivered.append(chunk)
            return FakeResponse({"object": "list"}, status=200)

    n = 24
    fake = MarkerOnlySession(threshold=0)
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    api.append_blocks("page-1", [{"n": i} for i in range(n)])  # 예외가 나면 실패다

    assert api.refused == n  # 24개 전부 마커로 대체됐다
    assert fake.attempts <= 3 * n  # 그리고 예산 안에서 끝났다
    # 고정 40 예산이었다면 여기 닿기 전에 RuntimeError 로 터졌다.
    assert fake.attempts > notion.MAX_APPEND_ATTEMPTS


def test_marker_patch_is_charged_to_the_budget():
    # 마커 PATCH 를 예산에서 안 깎으면 거부 블록당 요청 2회를 내면서 1회로
    # 청구해, 상한이 실제 요청 수를 못 묶는다.
    fake = CloudflareFakeSession(threshold=0)
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)
    api._append_budget = 1  # 첫 원본 시도 하나만 허용한다

    try:
        api._append_chunk("page-1", [{"n": 0}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("마커 시도가 예산에서 깎이지 않았다")

    assert fake.attempts == 1  # 마커 요청은 나가지도 못했다


def test_cloudflare_retry_sleeps_before_each_retry_attempt():
    # 크기만 줄이고 곧바로 재시도하면 안 된다 — 백오프는 시간으로도 물러서야
    # 한다. 재시도할 때마다 CLOUDFLARE_BACKOFF_SECONDS 만큼 자야 한다.
    fake = CloudflareFakeSession(threshold=2)
    slept = []
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: slept.append(s))

    api.append_blocks("page-1", [{"n": i} for i in range(9)])  # 9 > 2, 반드시 쪼개짐

    assert slept  # 최소 한 번은 쉬었다
    assert all(abs(s - notion.CLOUDFLARE_BACKOFF_SECONDS) < 1e-9 for s in slept)


# --- 거부된 블록의 마커 대체 -----------------------------
#
# 블록 1개짜리 요청까지 Cloudflare 에 막히면(위 섹션의 "포기하고 올린다")
# 더 쪼갤 곳이 없어 sync 가 중단되고 워터마크가 저장되지 않는다 — 그 세션은
# 영원히 수렴하지 않는다. 대신 그 블록을 내용을 바꾸지 않고 눈에 보이는
# 마커로 한 번 대체해 다시 보낸다. 마커까지 막히면 그건 Cloudflare 오탐이
# 아니라 다른 문제이므로 그대로 raise 한다.

def test_single_block_refused_falls_back_to_marker():
    fake = FakeSession({
        ("PATCH", BASE + "/blocks/page-1/children"): [
            FakeResponse({}, status=403, text=CloudflareFakeSession.CLOUDFLARE_HTML),
            FakeResponse({"object": "list"}, status=200),
        ],
    })
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    api.append_blocks("page-1", [{"n": 1}])  # 예외 없이 끝나야 한다

    patches = [p for p in fake.payloads if p[0] == "PATCH"]
    assert len(patches) == 2  # 원본 시도 + 마커 대체 시도
    marker_body = patches[1][2]
    marker_text = marker_body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"]
    assert marker_text == notion.REFUSED_MARKER
    # 마커도 build_blocks 와 같은 paragraph 블록 모양이어야 한다
    assert patches[1][2]["children"][0]["object"] == "block"
    assert patches[1][2]["children"][0]["type"] == "paragraph"


def test_marker_fallback_increments_refused():
    fake = FakeSession({
        ("PATCH", BASE + "/blocks/page-1/children"): [
            FakeResponse({}, status=403, text=CloudflareFakeSession.CLOUDFLARE_HTML),
            FakeResponse({"object": "list"}, status=200),
        ],
    })
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    api.append_blocks("page-1", [{"n": 1}])

    assert api.refused == 1


def test_marker_also_blocked_raises():
    # 마커 요청 자체가 Cloudflare 에 막히면 그건 오탐이 아니라 다른 문제다 —
    # 조용히 삼키지 말고 예외를 올려야 한다.
    fake = FakeSession({
        ("PATCH", BASE + "/blocks/page-1/children"):
            FakeResponse({}, status=403, text=CloudflareFakeSession.CLOUDFLARE_HTML),
    })
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    try:
        api.append_blocks("page-1", [{"n": 1}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("마커까지 막혔는데 조용히 넘어갔다")

    patches = [p for p in fake.payloads if p[0] == "PATCH"]
    assert len(patches) == 2  # 원본 1회 + 마커 1회, 그 이상 재시도하지 않는다


def test_real_notion_403_still_raises():
    # 진짜 Notion JSON 403(권한 부족 등)은 마커 대체 없이 그대로 raise 해야
    # 한다 — _is_cloudflare_block 이 거짓이면 마커 분기 자체에 들어가지 않는다.
    body = {
        "object": "error", "status": 403, "code": "restricted_resource",
        "message": "이 통합은 이 리소스에 대한 접근 권한이 없습니다.",
    }
    fake = FakeSession({
        ("PATCH", BASE + "/blocks/page-1/children"): FakeResponse(body, status=403),
    })
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    try:
        api.append_blocks("page-1", [{"n": 1}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("진짜 Notion 403 이 마커로 대체됐다")

    patches = [p for p in fake.payloads if p[0] == "PATCH"]
    assert len(patches) == 1  # 마커 재시도 없이 1회만
    assert api.refused == 0


def test_refused_starts_at_zero():
    api = notion.Notion("tok", DB, session=FakeSession())

    assert api.refused == 0


# --- 파일 업로드/첨부/삭제 --------------------------------
#
# 세션 전체 원문을 session.md 로 페이지에 첨부하는 데 쓰인다. 실제 API 로
# 검증된 2단계 업로드(POST /file_uploads → POST upload_url multipart) +
# file 블록 첨부 + 블록 삭제(첨부 갱신 때 이전 파일 블록을 치우는 데 쓴다).

def test_upload_file_two_step():
    upload_url = "https://notion-uploads.example.com/up-1"
    fake = FakeSession({
        ("POST", BASE + "/file_uploads"):
            FakeResponse({"id": "upload-1", "upload_url": upload_url}),
        ("POST", upload_url): FakeResponse({}, status=200),
    })
    api = notion.Notion("tok", DB, session=fake)

    result = api.upload_file("session.md", b"hello", "text/markdown")

    assert result == "upload-1"
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 2
    assert posts[0][1] == BASE + "/file_uploads"
    assert posts[1][1] == upload_url
    # 1단계 요청 바디도 명세대로인지
    assert fake.payloads[0][2] == {"filename": "session.md", "content_type": "text/markdown"}


def test_upload_file_drops_json_content_type():
    upload_url = "https://notion-uploads.example.com/up-1"
    fake = FakeSession({
        ("POST", BASE + "/file_uploads"):
            FakeResponse({"id": "upload-1", "upload_url": upload_url}),
        ("POST", upload_url): FakeResponse({}, status=200),
    })
    api = notion.Notion("tok", DB, session=fake)

    api.upload_file("session.md", b"hello", "text/markdown")

    assert len(fake.file_posts) == 1
    call = fake.file_posts[0]
    assert call["url"] == upload_url
    # self.http.headers 에 박힌 Content-Type: application/json 을 이 요청에만
    # 떨어뜨려야 multipart 경계가 안 깨진다.
    assert call["headers"] == {"Content-Type": None}
    assert call["files"] == {"file": ("session.md", b"hello", "text/markdown")}


def test_upload_file_raises_on_failure():
    fake = FakeSession({
        ("POST", BASE + "/file_uploads"): FakeResponse({}, status=400),
    })
    api = notion.Notion("tok", DB, session=fake)

    try:
        api.upload_file("session.md", b"hello")
    except RuntimeError:
        pass
    else:
        raise AssertionError("1단계 실패가 조용히 넘어갔다")

    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 1  # 2단계로 가지 않았다
    assert fake.file_posts == []


def test_attach_file_returns_block_id():
    fake = FakeSession({
        ("PATCH", BASE + "/blocks/page-1/children"):
            FakeResponse({"results": [{"id": "block-99"}]}),
    })
    api = notion.Notion("tok", DB, session=fake)

    block_id = api.attach_file("page-1", "upload-1", name="session.md")

    assert block_id == "block-99"
    method, url, body = fake.payloads[0]
    assert url == BASE + "/blocks/page-1/children"
    assert body == {"children": [{
        "object": "block", "type": "file",
        "file": {
            "type": "file_upload",
            "file_upload": {"id": "upload-1"},
            "name": "session.md",
        },
    }]}


def test_attach_file_defaults_name_to_session_md():
    fake = FakeSession({
        ("PATCH", BASE + "/blocks/page-1/children"):
            FakeResponse({"results": [{"id": "block-1"}]}),
    })
    api = notion.Notion("tok", DB, session=fake)

    api.attach_file("page-1", "upload-1")

    body = fake.payloads[0][2]
    assert body["children"][0]["file"]["name"] == "session.md"


def test_delete_block():
    fake = FakeSession({
        ("DELETE", BASE + "/blocks/block-1"): FakeResponse({}, status=200),
    })
    api = notion.Notion("tok", DB, session=fake)

    api.delete_block("block-1")

    assert fake.calls == [("DELETE", BASE + "/blocks/block-1")]

    failing = FakeSession({
        ("DELETE", BASE + "/blocks/block-2"): FakeResponse({}, status=404),
    })
    api2 = notion.Notion("tok", DB, session=failing)
    try:
        api2.delete_block("block-2")
    except RuntimeError:
        pass
    else:
        raise AssertionError("삭제 실패가 조용히 넘어갔다")


# --- 메시지 단위 배치 -------------------------------------------

def messages(count, size=10):
    return [
        {"role": "user" if i % 2 == 0 else "assistant",
         "text": "메시지{}".format(i) * size,
         "uuid": "u{}".format(i)}
        for i in range(count)
    ]


def test_batches_never_exceed_the_block_limit():
    produced = list(notion.batches(messages(120)))

    assert len(produced) > 1
    assert all(len(blocks) <= notion.MAX_BLOCKS_PER_REQUEST for blocks, _ in produced)


def test_batches_lose_nothing_and_duplicate_nothing():
    sample = messages(120)
    rejoined = [b for blocks, _ in notion.batches(sample) for b in blocks]

    assert rejoined == notion.build_blocks(sample)


def test_batch_watermark_is_a_real_message_uuid_at_the_boundary():
    sample = messages(120)
    produced = list(notion.batches(sample))
    uuids = [uuid for _, uuid in produced]

    # 워터마크는 반드시 실제 메시지의 uuid 여야 한다. 블록 단위로 자르면
    # 경계가 메시지 중간에 떨어져 저장할 uuid 자체가 없다.
    assert all(uuid in {m["uuid"] for m in sample} for uuid in uuids)
    # 마지막 배치의 워터마크는 마지막 메시지
    assert uuids[-1] == sample[-1]["uuid"]
    # 각 배치는 자기 uuid 까지의 메시지를 정확히 담는다
    consumed = 0
    for blocks, uuid in produced:
        consumed += len(blocks)
        prefix = sample[:[m["uuid"] for m in sample].index(uuid) + 1]
        assert consumed == len(notion.build_blocks(prefix))


def test_batches_keep_an_oversized_message_whole():
    # 한 메시지가 홀로 100블록을 넘으면 쪼갤 지점이 없다. 그 메시지 하나로 배치를
    # 만들고, 요청 단위 분할은 append_blocks 에 맡긴다.
    huge = [{"role": "assistant", "text": "가" * (notion.MAX_TEXT_PER_BLOCK * 150),
             "uuid": "huge"}]
    produced = list(notion.batches(huge))

    assert len(produced) == 1
    blocks, uuid = produced[0]
    assert uuid == "huge"
    assert len(blocks) > notion.MAX_BLOCKS_PER_REQUEST

    fake = FakeSession()
    notion.Notion("tok", DB, session=fake).append_blocks("page-1", blocks)
    assert len(fake.calls) == 2


def test_batches_of_nothing_yields_nothing():
    assert list(notion.batches([])) == []


def test_batches_put_a_single_small_message_in_one_batch():
    produced = list(notion.batches(messages(1)))

    assert len(produced) == 1
    assert produced[0][1] == "u0"


def test_batches_respect_a_custom_limit():
    # 메시지당 2블록이므로 limit 5 면 메시지 2개(4블록)까지 들어간다
    produced = list(notion.batches(messages(6), limit=5))

    assert [len(blocks) for blocks, _ in produced] == [4, 4, 4]
    assert [uuid for _, uuid in produced] == ["u1", "u3", "u5"]


# --- 요청 바디 바이트 예산 ----------------------
#
# append_blocks 가 요청 바디를 크게 보내면 403 이 온다. 그런데 이건 Notion 이
# 아니라 Notion 앞단의 Cloudflare 가 봇 방어로 돌려주는 응답이다(본문이
# `<title>Attention Required! | Cloudflare</title>`). 실제 API 로 이분 탐색한
# 결과 36KB 는 통과, 66KB 는 Cloudflare 403 — 그 사이 어딘가가 진짜 한도다.
# batches() 는 그래서 블록수(100) 뿐 아니라 바이트수(30KB)로도 배치를 끊어야
# 한다. 아래 테스트들은 이 픽스 이전 코드(블록수만 보는 batches)에서는 전부
# 실패한다 — 특히 처음 세 개는 배치 하나가 100블록보다 한참 작은데도 30KB 를
# 넘겨 나가버린다.

def payload_bytes(blocks):
    """batches() 가 실제로 재는 것과 동일한 방식(요청 바디 그대로 직렬화)으로
    독립적으로 다시 잰다 — notion._payload_bytes 를 그대로 재사용하지 않고
    같은 정의를 테스트에서 한 번 더 적어, 구현이 이 정의에서 벗어나면 테스트가
    잡아내게 한다."""
    return len(json.dumps({"children": blocks}).encode("utf-8"))


def korean_message(i, chars, role=None):
    role = role or ("user" if i % 2 == 0 else "assistant")
    return {"role": role, "text": "가" * chars, "uuid": "kr{}".format(i)}


def test_batches_cut_by_byte_budget_far_below_the_block_limit():
    # 메시지당 2블록(500자 한글 문단 하나) x 20개 = 40블록. 100블록 한도에는
    # 한참 못 미치지만, 문단 하나가 이스케이프된 JSON 으로 약 3.2KB 라 9개
    # 남짓만 모여도 30KB 예산에 닿는다.
    sample = [korean_message(i, 500) for i in range(20)]

    produced = list(notion.batches(sample))

    assert len(produced) > 1
    assert all(len(blocks) < 50 for blocks, _ in produced)  # 100블록 한도의 절반도 안 됨
    assert all(payload_bytes(blocks) <= notion.MAX_BYTES_PER_REQUEST for blocks, _ in produced)
    # 배치 사이를 가른 원인이 진짜 바이트라는 것: 다음 메시지를 더하면 예산을 넘긴다
    rejoined_so_far = []
    messages_by_uuid = {m["uuid"]: m for m in sample}
    for blocks, uuid in produced[:-1]:
        idx = list(messages_by_uuid).index(uuid)
        next_message = sample[idx + 1]
        with_next = blocks + notion.build_blocks([next_message])
        assert payload_bytes(with_next) > notion.MAX_BYTES_PER_REQUEST


def test_every_batch_payload_stays_under_the_byte_budget_for_korean_text():
    # 실제 대화처럼 사용자 프롬프트는 짧고 어시스턴트 응답은 긴, 뒤섞인 길이의
    # 한글 메시지. 캐릭터 수가 아니라 바이트 수를 재지 않으면 이 중 다수의
    # 배치가 30KB 를 조용히 넘겨버린다(한글은 UTF-8 이스케이프로 글자당 6바이트).
    sentence = (
        "이것은 실제 대화처럼 보이도록 만든 한국어 문장입니다. "
        "세션 로그 기록 테스트를 위한 예시 본문이며 충분히 길게 반복됩니다. "
    )
    sample = []
    for i in range(40):
        role = "user" if i % 2 == 0 else "assistant"
        reps = 3 if role == "user" else 20  # 사용자는 짧게, 어시스턴트는 길게
        sample.append({"role": role, "text": sentence * reps, "uuid": "conv{}".format(i)})

    produced = list(notion.batches(sample))

    assert len(produced) > 1
    for blocks, _ in produced:
        assert payload_bytes(blocks) <= notion.MAX_BYTES_PER_REQUEST


def test_block_limit_still_cuts_batches_when_messages_are_byte_small():
    # 블록수 한도가 여전히 독립적으로 작동하는지 확인한다. 지금 기본 바이트
    # 예산(8KB)은 Cloudflare 의 적응형 문턱에 맞춘 값이라 메시지 두 개(빈 텍스트
    # 라도 heading+paragraph 오버헤드만으로) 정도만 모여도 닿아버린다 — 즉
    # "메시지가 byte-small 이면 block 100 에서만 끊긴다"는 이제 기본 예산에서는
    # 성립하지 않는다(그 자체가 이 픽스의 요점: 고정 바이트 여유가 없다).
    # 그래서 이 테스트는 byte_limit 을 넉넉히 열어 블록수 한도만 따로 검증한다.
    tiny = messages(300, size=1)

    produced = list(notion.batches(tiny, byte_limit=10**9))

    assert len(produced) > 1
    assert all(len(blocks) <= notion.MAX_BLOCKS_PER_REQUEST for blocks, _ in produced)
    # 마지막 배치를 뺀 나머지는 블록수 한도에 정확히 닿아야 한다 — 바이트가
    # 아니라 블록수가 자른 것이라는 증거
    for blocks, _ in produced[:-1]:
        assert len(blocks) == notion.MAX_BLOCKS_PER_REQUEST


def test_a_single_byte_oversized_message_is_still_emitted_and_never_dropped():
    # 문단 3개짜리(4블록, 100블록 한도에는 한참 못 미침) 메시지 하나가 그 자체로
    # 34.7KB 라 30KB 예산을 혼자 넘긴다. 쪼갤 곳이 없으니 그대로 자기 배치 하나로
    # 나가야 한다 — 조용히 버려지거나, 무한 루프에 빠지거나, 옆 메시지와 뭉쳐
    # 나가면 안 된다.
    def small(i):
        role = "user" if i % 2 == 0 else "assistant"
        return {"role": role, "text": "안녕하세요 {}".format(i), "uuid": "small{}".format(i)}

    big = {"role": "assistant", "text": "가" * (notion.MAX_TEXT_PER_BLOCK * 3), "uuid": "big"}
    sample = [small(0), small(1), big, small(2), small(3)]

    big_blocks = notion.build_blocks([big])
    assert len(big_blocks) < notion.MAX_BLOCKS_PER_REQUEST  # 블록수로는 안 걸림을 확인
    assert payload_bytes(big_blocks) > notion.MAX_BYTES_PER_REQUEST  # 바이트로는 걸림을 확인

    produced = list(notion.batches(sample))
    uuids = [uuid for _, uuid in produced]

    assert "big" in uuids
    big_batch = produced[uuids.index("big")][0]
    assert big_batch == big_blocks  # 다른 메시지와 뭉치지 않고 단독 배치

    # 유실도 중복도 없다: 모든 배치를 이어 붙이면 원본 전체와 정확히 같다
    rejoined = [b for blocks, _ in produced for b in blocks]
    assert rejoined == notion.build_blocks(sample)


def test_realistic_transcript_fixture_stays_under_the_byte_budget():
    # 실제 세션 픽스처(작아서 원래도 통과하지만, 회귀 방지용 스모크 테스트)
    entries = transcript.read_entries(str(FIXTURES / "with_aititle.jsonl"))
    sample = transcript.conversation(entries)
    assert sample  # 픽스처가 비어 있으면 이 테스트는 아무것도 검증하지 못한다

    for blocks, _ in notion.batches(sample):
        assert payload_bytes(blocks) <= notion.MAX_BYTES_PER_REQUEST


def test_synthetic_long_korean_transcript_stays_under_the_byte_budget():
    # 더 중요한 쪽: 실제 픽스처는 작아서 예산에 안 걸린다. 장문 한글 응답이
    # 섞인 합성 트랜스크립트로 실제 문제 상황을 재현해 확인한다.
    #
    # 기본 예산이 8KB 로 내려온 뒤로는 응답 하나(reps=35, 약 2만 바이트)가 이미
    # 혼자 예산을 넘긴다 — batches() 는 이런 메시지를 쪼갤 곳이 없어 그대로
    # 자기 배치 하나로 내보낸다(위 test_a_single_byte_oversized_message_...
    # 참고). 그래서 "모든 배치가 예산 이하"는 더 이상 불변식이 아니다: 예산을
    # 넘긴 배치가 있다면 그건 반드시 메시지 하나짜리여야 하고, append_blocks 가
    # Cloudflare 적응형 백오프로 마저 쪼갠다. 유실·중복이 없다는 것과, 예산을
    # 넘기는 배치는 오직 그 "쪼갤 곳 없음" 경우뿐이라는 것을 확인한다.
    sentence = (
        "오늘 작업한 내용을 정리하면 다음과 같습니다. 원인을 분석하고 수정한 뒤 "
        "테스트를 추가했습니다. 세부 사항은 아래와 같이 길게 설명됩니다. "
    )
    sample = []
    for i in range(60):
        role = "user" if i % 3 == 0 else "assistant"
        reps = 4 if role == "user" else 35
        sample.append({"role": role, "text": sentence * reps, "uuid": "long{}".format(i)})
    by_uuid = {m["uuid"]: m for m in sample}

    produced = list(notion.batches(sample))

    assert len(produced) > 3  # 정말로 여러 배치로 나뉘었는지
    for blocks, uuid in produced:
        if payload_bytes(blocks) > notion.MAX_BYTES_PER_REQUEST:
            assert blocks == notion.build_blocks([by_uuid[uuid]]), (
                "예산을 넘긴 배치는 쪼갤 곳 없는 메시지 하나짜리여야 한다"
            )
    # 유실도 중복도 없다
    rejoined = [b for blocks, _ in produced for b in blocks]
    assert rejoined == notion.build_blocks(sample)


# --- 제목 길이 방어 ---------------------------------------------

def test_build_props_truncates_an_overlong_title():
    # transcript.TITLE_MAX 가 100자로 자르므로 훅 경로에서는 발동하지 않지만,
    # build_props 는 공개 빌더라 Notion 상한 방어를 남겨둔다.
    when = datetime(2026, 9, 2, 21, 30, tzinfo=KST)
    props = notion.build_props("가" * 5000, "p", when, "s")

    content = props["작업"]["title"][0]["text"]["content"]
    assert len(content) == notion.MAX_TEXT_PER_BLOCK


# --- 제목이 WAF 에 걸리는 경로 -------------------------------------------
# 제목은 트랜스크립트에서 온다(aiTitle 없으면 첫 사용자 프롬프트의 첫 줄).
# 거기 <script 가 있으면 create_page/update_props 가 그 세션의 첫 API 호출부터
# 403 을 받는다. 캐시 경로에서는 update_props 가 append 보다 먼저 돌므로,
# 막히면 그 세션은 영원히 아무것도 기록되지 않는다.

CF_HTML = "<!DOCTYPE html><title>Attention Required! | Cloudflare</title>"
BLOCKED_PROPS = {"작업": {"title": [{"text": {"content": "<script> 왜 안 되죠?"}}]}}
SAFE_PROPS = {"작업": {"title": [{"text": {"content": "Projects 세션 14:30"}}]}}


def test_create_page_retries_with_fallback_title_when_title_is_refused():
    fake = FakeSession({("POST", "https://api.notion.com/v1/pages"): [
        FakeResponse({}, status=403, text=CF_HTML),
        FakeResponse({"id": "page-new"}, status=200),
    ]})
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    assert api.create_page(BLOCKED_PROPS, fallback_props=SAFE_PROPS) == "page-new"

    sent = [p for m, u, p in fake.payloads if m == "POST"]
    assert len(sent) == 2
    assert sent[0]["properties"] == BLOCKED_PROPS   # 원본을 먼저 시도하고
    assert sent[1]["properties"] == SAFE_PROPS      # 막히면 통째로 갈아끼운다
    assert api.title_refused == 1


def test_update_props_retries_with_fallback_title_when_title_is_refused():
    url = "https://api.notion.com/v1/pages/page-1"
    fake = FakeSession({("PATCH", url): [
        FakeResponse({}, status=403, text=CF_HTML),
        FakeResponse({}, status=200),
    ]})
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    api.update_props("page-1", BLOCKED_PROPS, fallback_props=SAFE_PROPS)

    sent = [p for m, u, p in fake.payloads if m == "PATCH"]
    assert len(sent) == 2
    assert sent[1]["properties"] == SAFE_PROPS
    assert api.title_refused == 1


def test_props_fallback_does_not_alter_the_original_title():
    """대체는 '통째로 갈아끼우기'다. 원본 제목을 변형해 통과시키는 게 아니다."""
    fake = FakeSession({("POST", "https://api.notion.com/v1/pages"): [
        FakeResponse({}, status=403, text=CF_HTML),
        FakeResponse({"id": "p"}, status=200),
    ]})
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)
    api.create_page(BLOCKED_PROPS, fallback_props=SAFE_PROPS)

    retry = [p for m, u, p in fake.payloads if m == "POST"][1]
    title = retry["properties"]["작업"]["title"][0]["text"]["content"]
    # 원본에서 파생된 흔적이 없어야 한다 — 제로폭 문자를 끼우거나 쪼갠 형태가
    # 아니라, 대화가 한 글자도 안 섞인 별개의 문자열이어야 한다.
    assert title == "Projects 세션 14:30"
    assert "script" not in title


def test_props_without_fallback_still_raises():
    """호출자가 대체를 안 주면 기존대로 예외. 조용히 삼키지 않는다."""
    fake = FakeSession({("POST", "https://api.notion.com/v1/pages"):
                        FakeResponse({}, status=403, text=CF_HTML)})
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    try:
        api.create_page(BLOCKED_PROPS)
    except Exception:
        pass
    else:
        raise AssertionError("대체 제목이 없는데도 예외가 나지 않았다")


def test_genuine_notion_403_on_props_is_not_retried():
    """진짜 Notion 403(JSON 에러)은 재시도해도 결과가 같으므로 즉시 올린다."""
    fake = FakeSession({("POST", "https://api.notion.com/v1/pages"):
                        FakeResponse({"object": "error", "code": "unauthorized"},
                                     status=403)})
    api = notion.Notion("tok", DB, session=fake, sleep=lambda s: None)

    try:
        api.create_page(BLOCKED_PROPS, fallback_props=SAFE_PROPS)
    except Exception:
        pass
    else:
        raise AssertionError("진짜 403 인데 예외가 나지 않았다")

    assert len([p for m, u, p in fake.payloads if m == "POST"]) == 1
    assert api.title_refused == 0
