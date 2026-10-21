"""에이전트별 트랜스크립트 어댑터.

어댑터는 모듈 하나이고 다음을 제공한다:
    NAME, SHORT                         표시 이름 (Notion 의 에이전트 열, 본문 머리글)
    read_entries(path)                  트랜스크립트 → 엔트리 리스트
    started_at(entries)                 세션 시작 시각 (aware datetime | None)
    project_name(entries, cwd)          프로젝트 이름
    title(entries, project, when, lang) 페이지 제목
    conversation(entries, after_uuid)   [{"role", "text", "uuid"}] — uuid 는 워터마크
"""
from . import claude, codex

AGENTS = {"claude": claude, "codex": codex}
