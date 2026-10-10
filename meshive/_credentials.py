"""Local credentials storage (for `meshive login`).

Stores api_key (+ optional base_url) in `~/.meshive/credentials.json`.
It holds a secret, so the directory is created 0700 and the file 0600.
The location can be changed with MESHIVE_CONFIG_DIR (test isolation / multiple configs).

Resolution order (in _config): explicit argument > environment variable > this file > default.
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
    """Saved credentials. An empty dict if the file is missing or corrupt (silent fallback)."""
    try:
        with open(credentials_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save(api_key: str, base_url: str | None = None) -> Path:
    """Save credentials. Directory 0700 / file 0600. Returns the saved path."""
    directory = config_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = credentials_path()
    data: dict[str, str] = {"api_key": api_key}
    if base_url:
        data["base_url"] = base_url
    # Write to a temp file and swap it in with os.replace — a crash mid-write can't corrupt the file,
    # and a symlink planted at the final path can't leak the key elsewhere (rename replaces the link itself).
    # 0600 from O_CREAT on — the plaintext key is never exposed with wider permissions, even briefly.
    # O_NOFOLLOW — a symlink planted at the temp path isn't followed either.
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
    """Delete the credentials file. True if it was deleted, False if it didn't exist."""
    try:
        credentials_path().unlink()
        return True
    except FileNotFoundError:
        return False
