"""Config precedence (flag > env > default) + auth headers + URL quoting."""

from localai_steer.config import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_S,
    resolve_config,
)


def test_defaults_when_nothing_set() -> None:
    cfg = resolve_config(agent="A", env={})
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.api_key is None
    assert cfg.timeout_s == DEFAULT_TIMEOUT_S
    assert cfg.verbose is False


def test_env_used_when_flags_absent() -> None:
    env = {"LOCALAI_BASE_URL": "http://env:1", "LOCALAI_API_KEY": "envkey"}
    cfg = resolve_config(agent="A", env=env)
    assert cfg.base_url == "http://env:1"
    assert cfg.api_key == "envkey"


def test_flags_beat_env() -> None:
    env = {"LOCALAI_BASE_URL": "http://env:1", "LOCALAI_API_KEY": "envkey"}
    cfg = resolve_config(agent="A", base_url="http://flag:2", key="flagkey", env=env)
    assert cfg.base_url == "http://flag:2"
    assert cfg.api_key == "flagkey"


def test_timeout_flag() -> None:
    cfg = resolve_config(agent="A", timeout_s=12.5, env={})
    assert cfg.timeout_s == 12.5


def test_auth_headers_present_when_key_set() -> None:
    cfg = resolve_config(agent="A", key="sekret", env={})
    assert cfg.auth_headers == {"Authorization": "Bearer sekret"}


def test_auth_headers_absent_without_key() -> None:
    cfg = resolve_config(agent="A", env={})
    assert cfg.auth_headers == {}


