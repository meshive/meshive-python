"""로컬 credentials 저장 (`meshive login` 용).

`~/.meshive/credentials.json` 에 api_key (+선택적 base_url) 를 저장한다.
비밀을 담으므로 디렉토리 0700 / 파일 0600 권한으로 생성한다.
경로는 MESHIVE_CONFIG_DIR 로 재정의 가능 (테스트 격리 / 다중 설정).

해석 우선순위(_config 에서): 명시 인자 > 환경변수 > 이 파일 > 기본값.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ENV_CONFIG_DIR = "MESHIVE_CONFIG_DIR"


def config_dir() -> Path:
    override = os.getenv(ENV_CONFIG_DIR)
    return Path(override) if override else Path.home() / ".meshive"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def load() -> dict:
    """저장된 credentials. 파일 없음/깨짐 시 빈 dict (조용히 폴백)."""
    try:
        with open(credentials_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save(api_key: str, base_url: str | None = None) -> Path:
    """credentials 저장. 디렉토리 0700 / 파일 0600. 저장 경로 반환."""
    directory = config_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = credentials_path()
    data: dict[str, str] = {"api_key": api_key}
    if base_url:
        data["base_url"] = base_url
    # 임시 파일에 쓰고 os.replace 로 원자 교체 — 쓰기 도중 크래시로 파일이 깨지지 않고,
    # 최종 경로에 심링크가 심어져 있어도 키가 다른 곳으로 새지 않는다(rename 은 링크 자체를 대체).
    # O_CREAT 시점부터 0600 — 평문 키가 잠깐이라도 넓은 권한으로 노출되지 않게.
    # O_NOFOLLOW — 임시 경로에 미리 심어둔 심링크도 추적하지 않는다.
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
    return path


def clear() -> bool:
    """credentials 파일 삭제. 삭제했으면 True, 원래 없었으면 False."""
    try:
        credentials_path().unlink()
        return True
    except FileNotFoundError:
        return False
