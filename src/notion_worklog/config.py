"""경로 상수, 설정·토큰 로딩, 에러 로깅."""
import json
import os
from datetime import datetime
from pathlib import Path

# 설정·토큰·캐시·로그가 모두 이 디렉터리에 산다. 테스트나 여러 프로필을 쓰려면
# 환경변수로 옮긴다.
HOME = Path(os.environ.get("NOTION_WORKLOG_HOME") or (Path.home() / ".notion-worklog"))
CONFIG_PATH = HOME / "config.json"
TOKEN_PATH = HOME / "token"
STATE_PATH = HOME / "state.json"
ERROR_LOG = HOME / "error.log"

# 날짜·제목에 쓰는 시간대. 기본은 이 컴퓨터의 로컬 시간대다.
TZ = datetime.now().astimezone().tzinfo

NOTION_VERSION = "2022-06-28"
# (연결, 읽기) 타임아웃. requests 는 튜플을 이 순서로 해석한다.
#
# 읽기 5초는 너무 빡빡했다 — 평범한 append 가 ReadTimeout 으로 죽었다. Notion
# 응답은 평소 1초 안쪽이지만 꼬리가 길다. 읽기 타임아웃이 나도 요청이 반영됐을
# 수 있다는 점은 sync._landed_blocks 가 다룬다.
NOTION_TIMEOUT = (5, 15)


def load_config(path=None):
    """config.json 을 dict 로 읽는다. 없거나 깨졌으면 빈 dict."""
    path = Path(path) if path is not None else CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_token(path=None):
    """Notion 토큰. 환경변수 NOTION_TOKEN 이 파일보다 우선한다. 없으면 빈 문자열."""
    env = os.environ.get("NOTION_TOKEN", "").strip()
    if env:
        return env
    path = Path(path) if path is not None else TOKEN_PATH
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_private(path, text):
    """소유자만 읽고 쓸 수 있는 파일로 쓴다 (토큰이 든 파일이므로)."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(str(path), 0o600)


def save_config(data, path=None):
    _write_private(path or CONFIG_PATH, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def save_token(token, path=None):
    _write_private(path or TOKEN_PATH, token.strip() + "\n")


def log_error(text):
    """error.log 에 한 줄 남긴다.

    이 함수는 훅의 마지막 방어선이므로 **절대 예외를 내지 않는다.**
    문자열이 아닌 인자(예외 객체 등)를 받아도 죽지 않도록 강제 변환하고,
    로깅 자체의 실패는 종류를 가리지 않고 삼킨다.
    """
    try:
        stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        Path(ERROR_LOG).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write("[{}] {}\n".format(stamp, str(text).strip()))
    except Exception:
        pass
