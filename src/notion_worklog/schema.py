"""setup 단계: 사용자가 준 URL 로 데이터베이스를 찾고 필요한 열을 갖춘다."""
import re
from urllib.parse import urlsplit

from .text import LANGS, TEXT

# 역할 → Notion 속성 형식. title 은 데이터베이스마다 반드시 하나 있으므로 만들지 않는다.
ROLE_TYPES = (
    ("date", "date"),
    ("project", "select"),
    ("session", "rich_text"),
    ("agent", "select"),
)

_ID = re.compile(r"[0-9a-f]{32}")


class SeveralDatabases(LookupError):
    """페이지 안에 테이블이 여러 개라 하나를 골라야 한다."""

    def __init__(self, candidates):
        super().__init__("이 페이지에는 테이블이 여러 개 있습니다")
        self.candidates = candidates


def parse_id(text):
    """Notion URL 이나 id 문자열에서 id 를 꺼내 대시 형식으로 돌려준다.

    URL 의 ?v= 뒤는 보기(view) id 라 버린다. 경로에 id 가 여럿이면 마지막 것이
    대상이다 (제목-id 형식의 슬러그).
    """
    raw = (text or "").strip()
    path = urlsplit(raw).path if "://" in raw else raw
    found = _ID.findall(path.replace("-", "").lower())
    if not found:
        raise ValueError("Notion URL 이나 id 를 알아볼 수 없습니다: {!r}".format(text))
    hexid = found[-1]
    return "-".join((hexid[:8], hexid[8:12], hexid[12:16], hexid[16:20], hexid[20:]))


def _title(database):
    return "".join(part.get("plain_text", "") for part in database.get("title") or [])


def resolve_database(api, any_id):
    """id 가 데이터베이스면 그것을, 인라인 테이블을 담은 페이지면 그 안의 테이블을 돌려준다.

    인라인 테이블은 URL 에 자기 id 가 나오지 않고 담고 있는 페이지 id 가 나온다.
    """
    try:
        return api.get_database(any_id)
    except Exception:
        pass

    try:
        children = api.list_children(any_id)
    except Exception:
        raise LookupError(
            "테이블을 찾을 수 없습니다. Notion 에서 그 테이블(또는 담고 있는 페이지)의 "
            "⋯ 메뉴 → 연결(Connections) 에 이 통합을 추가했는지 확인하세요."
        )

    candidates = [
        (block["id"], (block.get("child_database") or {}).get("title", ""))
        for block in children
        if block.get("type") == "child_database"
    ]
    if not candidates:
        raise LookupError("이 페이지 안에서 테이블을 찾지 못했습니다. 테이블 URL 을 주세요.")
    if len(candidates) > 1:
        raise SeveralDatabases(candidates)
    return api.get_database(candidates[0][0])


def ensure_columns(api, database, lang):
    """필요한 열을 갖추고 (역할 → 열 이름, 새로 만든 열 이름 목록) 을 돌려준다.

    이미 있는 열은 이름이 어느 언어든 형식이 맞으면 재사용하고, 없는 열만
    lang 의 이름으로 만든다. 같은 이름의 다른 형식 열이 있으면 덮어쓰지 않고
    멈춘다 — 사용자의 데이터를 망가뜨리지 않기 위해서다.
    """
    properties = {name: spec.get("type") for name, spec in database["properties"].items()}
    columns = {"title": next(name for name, kind in properties.items() if kind == "title")}
    to_create = {}

    for role, kind in ROLE_TYPES:
        aliases = [TEXT[other]["columns"][role] for other in LANGS]
        reuse = next((name for name in aliases if properties.get(name) == kind), None)
        if reuse:
            columns[role] = reuse
            continue
        name = TEXT[lang]["columns"][role]
        if name in properties:
            raise ValueError(
                "'{}' 열이 이미 있지만 형식이 {} 가 아니라 {} 입니다. 열 이름을 바꾸거나 "
                "다른 테이블을 쓰세요.".format(name, kind, properties[name])
            )
        columns[role] = name
        to_create[name] = {kind: {}}

    if to_create:
        api.add_properties(database["id"], to_create)
    return columns, list(to_create)
