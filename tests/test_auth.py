import importlib
import json
import os
import stat

import httpx
import pytest

from meshive import _config, _credentials

cli = importlib.import_module("meshive.cli.main")


@pytest.fixture(autouse=True)
def isolate_config_dir(monkeypatch, tmp_path):
    """credentials 를 임시 디렉토리에 격리 (실제 ~/.meshive 건드리지 않도록)."""
    cfg = tmp_path / "cfg"
    monkeypatch.setenv(_credentials.ENV_CONFIG_DIR, str(cfg))
    # base_url/api_key 환경변수가 테스트에 새지 않도록 제거.
    monkeypatch.delenv(_config.ENV_BASE_URL, raising=False)
    monkeypatch.delenv(_config.ENV_API_KEY, raising=False)
    return cfg


def _ok_me(request):
    return httpx.Response(200, json={"email": "u@x.com", "username": "u", "userRole": "user"})


def _patch_client(monkeypatch, handler):
    class C(cli.Meshive):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._client = httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli, "Meshive", C)
    return C


# --- credentials store -------------------------------------------------------

def test_save_load_roundtrip():
    _credentials.save("meshive_abc", "https://api.dev.meshive.ai")
    data = _credentials.load()
    assert data == {"api_key": "meshive_abc", "base_url": "https://api.dev.meshive.ai"}


def test_save_omits_base_url_when_none():
    _credentials.save("meshive_abc")
    assert _credentials.load() == {"api_key": "meshive_abc"}


def test_saved_file_is_0600():
    path = _credentials.save("meshive_abc")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_load_missing_returns_empty():
    assert _credentials.load() == {}


def test_clear():
    _credentials.save("meshive_abc")
    assert _credentials.clear() is True
    assert _credentials.clear() is False  # already gone


# --- resolution precedence ---------------------------------------------------

def test_credentials_used_as_fallback():
    _credentials.save("meshive_file", "https://api.dev.meshive.ai")
    assert _config.resolve_api_key() == "meshive_file"
    assert _config.resolve_base_url() == "https://api.dev.meshive.ai"


def test_env_overrides_credentials(monkeypatch):
    _credentials.save("meshive_file", "https://api.dev.meshive.ai")
    monkeypatch.setenv(_config.ENV_API_KEY, "meshive_env")
    monkeypatch.setenv(_config.ENV_BASE_URL, "https://env.meshive.ai")
    assert _config.resolve_api_key() == "meshive_env"
    assert _config.resolve_base_url() == "https://env.meshive.ai"


def test_explicit_overrides_all(monkeypatch):
    _credentials.save("meshive_file", "https://api.dev.meshive.ai")
    monkeypatch.setenv(_config.ENV_API_KEY, "meshive_env")
    assert _config.resolve_api_key("meshive_explicit") == "meshive_explicit"


# --- login / logout ----------------------------------------------------------

def test_login_with_flag_saves_and_verifies(monkeypatch, capsys):
    _patch_client(monkeypatch, _ok_me)
    assert cli.main(["login", "--api-key", "meshive_xyz"]) == 0
    out = capsys.readouterr().out
    assert "Logged in as u@x.com" in out
    assert _credentials.load()["api_key"] == "meshive_xyz"
    # prod 기본값이므로 base_url 은 저장하지 않음.
    assert "base_url" not in _credentials.load()


def test_login_dev_remembers_base_url(monkeypatch):
    _patch_client(monkeypatch, _ok_me)
    assert cli.main(["login", "--api-key", "meshive_xyz",
                     "--base-url", "https://api.dev.meshive.ai"]) == 0
    assert _credentials.load()["base_url"] == "https://api.dev.meshive.ai"


def test_login_prompts_when_no_flag(monkeypatch, capsys):
    _patch_client(monkeypatch, _ok_me)
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "meshive_prompted")
    assert cli.main(["login"]) == 0
    assert _credentials.load()["api_key"] == "meshive_prompted"


def test_login_invalid_key_not_saved(monkeypatch, capsys):
    def unauthorized(request):
        return httpx.Response(401, json={"detail": {"title": "Unauthorized", "message": "Invalid API key."}})

    _patch_client(monkeypatch, unauthorized)
    assert cli.main(["login", "--api-key", "meshive_bad"]) == 1
    assert "could not verify" in capsys.readouterr().err
    assert _credentials.load() == {}  # nothing persisted


def test_login_empty_key_errors(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: "   ")
    assert cli.main(["login"]) == 1
    assert "no API key" in capsys.readouterr().err


def test_logout_removes_credentials(capsys):
    _credentials.save("meshive_xyz")
    assert cli.main(["logout"]) == 0
    assert "Logged out" in capsys.readouterr().out
    assert _credentials.load() == {}


def test_logout_when_not_logged_in(capsys):
    assert cli.main(["logout"]) == 0
    assert "Not logged in" in capsys.readouterr().out


def test_me_uses_saved_credentials(monkeypatch, capsys):
    """login 후 --api-key 없이 me 가 동작 (파일 폴백)."""
    _patch_client(monkeypatch, _ok_me)
    cli.main(["login", "--api-key", "meshive_xyz"])
    capsys.readouterr()
    assert cli.main(["me"]) == 0
    assert "u@x.com" in capsys.readouterr().out
