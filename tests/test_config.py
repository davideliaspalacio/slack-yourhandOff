import pytest

from handoff_agent.config import load_settings


def test_load_settings_reads_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = load_settings()
    assert settings.database_url.endswith("/postgres")
    assert settings.openai_model == "gpt-4.1"
    assert settings.http_timeout_seconds > 0


def test_load_settings_fails_loudly_when_required_var_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        load_settings()
