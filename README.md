# notion-worklog

Claude Code·Codex CLI 로 작업한 세션을 **Notion 테이블에 자동으로 기록**합니다.
세션 하나가 테이블의 행 하나가 되고, 페이지 본문에는 대화가 쌓이며, 세션 전체 원문이
`session.md` 로 첨부됩니다. 업무일지를 손으로 정리할 필요가 없어집니다.

- LLM 을 부르지 않습니다 — 토큰 비용 0, 훅은 에이전트 밖의 별도 프로세스로 돕니다.
- 작업을 방해하지 않습니다 — 훅은 아무것도 출력하지 않고 항상 정상 종료하며, 실패는
  `~/.notion-worklog/error.log` 에만 남깁니다.
- 새 메시지만 이어 붙이고, 네트워크가 끊겨도 다음 턴에 이어서 기록합니다.

| 지원 에이전트 | 쓰는 훅 |
|---|---|
| Claude Code | `Stop`, `SessionEnd` (`~/.claude/settings.json`) |
| Codex CLI | `Stop`, `SessionEnd` (`~/.codex/hooks.json`) |

## 설치

Python 3.9 이상과 [uv](https://docs.astral.sh/uv/) (또는 pipx) 가 필요합니다.

```bash
uv tool install git+https://github.com/shelffim/notion-worklog
# 또는: pipx install git+https://github.com/shelffim/notion-worklog
```

## 설정 (한 번)

1. **Notion 통합 만들기** — <https://www.notion.so/profile/integrations> → 새 통합 →
   유형 "내부(Internal)" → 생성 후 토큰(`ntn_...`)을 복사합니다.
2. **기록할 테이블에 통합 연결** — 테이블(또는 테이블을 담은 페이지) 오른쪽 위
   `⋯` → 연결(Connections) → 방금 만든 통합을 추가합니다. 빈 테이블이어도 됩니다.
3. **setup 실행**

   ```bash
   notion-worklog setup
   ```

   토큰, 테이블 URL, 언어(ko/en)를 묻습니다. 테이블에 필요한 열이 없으면 자동으로
   만들고, 설치된 에이전트(Claude Code / Codex)를 찾아 훅을 등록합니다.
   인라인 테이블은 그 테이블을 담은 페이지 URL 을 줘도 됩니다.

4. **Codex 를 쓴다면** — 다음에 `codex` 를 열었을 때 `/hooks` 에서 notion-worklog
   훅을 신뢰(trust)해 주세요. Codex 는 신뢰하지 않은 훅을 실행하지 않습니다.

비대화형으로도 쓸 수 있습니다:

```bash
NOTION_TOKEN=ntn_... notion-worklog setup --database "<테이블 URL>" --lang ko --agents claude,codex
```

## 기록되는 내용

| 열 (ko / en) | 형식 | 값 |
|---|---|---|
| 테이블의 제목 열 | Title | 세션 제목 (Claude Code 가 붙인 제목, 없으면 첫 프롬프트의 첫 줄) |
| 날짜 / Date | Date | 세션 시작 시각 |
| 프로젝트 / Project | Select | 작업 폴더 이름 |
| 세션ID / Session ID | Text | 에이전트의 세션 id |
| 에이전트 / Agent | Select | `Claude Code` 또는 `Codex` |

이미 있는 열은 이름이 한국어든 영어든 형식이 맞으면 그대로 씁니다. 같은 이름인데 형식이
다른 열이 있으면 덮어쓰지 않고 setup 이 멈춥니다.

페이지 본문에는 **사람의 프롬프트와 에이전트의 답변 텍스트**만 들어갑니다. 도구 호출과
그 결과, 슬래시 커맨드 찌꺼기, 에이전트가 끼워 넣는 시스템 맥락은 빠집니다.

> ⚠️ 대화에 붙여넣은 내용은 그대로 Notion 에 올라갑니다. 비밀번호·API 키 같은 값을
> 대화에 붙여넣지 마세요. 기록할 테이블의 공유 범위도 확인하세요.

## 기록하지 않는 세션

훅은 모든 세션에서 돌기 때문에, 에이전트가 훅을 시험하려고 띄운 `claude -p` 세션까지
기록될 수 있습니다. 그래서 다음 세션은 건너뜁니다.

- **임시 폴더에서 연 세션** — 작업 폴더가 `/tmp`, `/var/tmp`, 시스템 임시 폴더(`$TMPDIR`)
  아래인 세션. 테스트용 일회용 프로젝트는 거의 이런 곳에 만들어집니다.
- **`NOTION_WORKLOG_DISABLE=1` 로 띄운 세션** — 임시 폴더 밖에서 테스트할 때 씁니다.

  ```bash
  NOTION_WORKLOG_DISABLE=1 claude -p "..."
  ```

헤드리스 실행(`claude -p`, `codex exec`) 자체는 거르지 않습니다. 실제 프로젝트 폴더에서
도는 자동화 작업은 그대로 기록됩니다.

## 파일 위치

모두 `~/.notion-worklog/` 에 있습니다 (`NOTION_WORKLOG_HOME` 으로 옮길 수 있음).

| 파일 | 내용 |
|---|---|
| `config.json` | 테이블 id, 열 이름, 언어 |
| `token` | Notion 토큰 (소유자만 읽기·쓰기, 600). 환경변수 `NOTION_TOKEN` 이 우선합니다 |
| `state.json` | 세션 → 페이지, 마지막으로 기록한 메시지 |
| `error.log` | 실패 기록 — 문제가 생기면 여기부터 보세요 |

## 제거

```bash
notion-worklog uninstall        # 등록한 훅만 제거 (다른 훅·설정은 그대로)
uv tool uninstall notion-worklog
rm -rf ~/.notion-worklog        # 설정·토큰·기록 캐시까지 지우려면
```

## 문제 해결

- **아무것도 기록되지 않는다** → `~/.notion-worklog/error.log` 를 보세요.
  `object_not_found` 면 테이블에 통합이 연결되지 않은 것입니다 (설정 2단계).
- **Codex 만 기록되지 않는다** → `/hooks` 에서 훅을 신뢰했는지 확인하세요.
- **특정 세션만 기록되지 않는다** → 작업 폴더가 임시 폴더인지, `NOTION_WORKLOG_DISABLE`
  이 켜져 있지 않은지 확인하세요 ([기록하지 않는 세션](#기록하지-않는-세션)).
- **본문에 ⚠️ 마커가 있다** → Notion API 앞단 보안 필터가 그 메시지를 거부한 것입니다.
  원문은 첨부된 `session.md` 에 그대로 있습니다.

## 한계

- macOS 와 Linux 에서 검증했습니다. Windows 는 검증하지 않았습니다.
- 데이터 소스가 여러 개인 Notion 데이터베이스는 지원하지 않습니다 (API `2022-06-28` 사용).
- 요약은 만들지 않고 원문을 기록합니다.

동작 원리와 설계 결정은 [docs/design.md](docs/design.md) 에 있습니다.

## 개발

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

테스트는 네트워크를 쓰지 않습니다. 픽스처는 모두 합성 데이터입니다.

## License

MIT
