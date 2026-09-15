import pytest

from handoff_agent.config import load_settings


def test_load_settings_reads_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = load_settings()
    assert settings.database_url.endswith("/postgres")
    assert settings.openai_model == "gpt-4.1"
    assert settings.http_timeout_seconds > 0


def test_load_settings_fails_loudly_when_database_url_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        load_settings()


def test_settings_load_without_an_openai_key(monkeypatch):
    """Las herramientas que no usan LLM tienen que funcionar mientras el resto
    de integraciones se conectan. Solo DATABASE_URL bloquea el arranque."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert load_settings().openai_api_key is None


def test_slack_channel_ids_parsing(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", " C1, C2,")
    settings = load_settings()
    assert settings.slack_channel_ids == ("C1", "C2")


def test_slack_channel_ids_defaults_to_empty_tuple(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("SLACK_CHANNEL_IDS", raising=False)
    settings = load_settings()
    assert settings.slack_channel_ids == ()


def test_slack_lookback_hours_defaults_to_1(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("SLACK_LOOKBACK_HOURS", raising=False)
    settings = load_settings()
    assert settings.slack_lookback_hours == 1.0


def test_slack_lookback_hours_parses_float_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SLACK_LOOKBACK_HOURS", "2.5")
    settings = load_settings()
    assert settings.slack_lookback_hours == 2.5


def test_slack_poll_seconds_defaults_to_3600(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("SLACK_POLL_SECONDS", raising=False)
    settings = load_settings()
    assert settings.slack_poll_seconds == 3600


def test_slack_poll_seconds_parses_int_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SLACK_POLL_SECONDS", "900")
    settings = load_settings()
    assert settings.slack_poll_seconds == 900


def test_slack_user_token_defaults_to_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    settings = load_settings()
    assert settings.slack_user_token is None


def test_slack_user_token_parses_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test-token")
    settings = load_settings()
    assert settings.slack_user_token == "xoxp-test-token"


def test_alert_webhook_url_defaults_to_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("HANDOFF_ALERT_WEBHOOK_URL", raising=False)
    settings = load_settings()
    assert settings.alert_webhook_url is None


def test_alert_webhook_url_parses_value(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("HANDOFF_ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/test")
    settings = load_settings()
    assert settings.alert_webhook_url == "https://hooks.slack.com/services/test"


def test_serper_api_key_defaults_to_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    settings = load_settings()
    assert settings.serper_api_key is None


def test_price_serper_per_query_defaults_to_0_001(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("PRICE_SERPER_PER_QUERY", raising=False)
    settings = load_settings()
    assert settings.price_serper_per_query == 0.001


def test_langfuse_base_url_is_read_from_langfuse_base_url(monkeypatch):
    """Es el nombre que muestra el panel de Langfuse. Si también está el antiguo
    LANGFUSE_HOST, manda el nuevo."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    monkeypatch.setenv("LANGFUSE_HOST", "https://otro.example.com")
    assert load_settings().langfuse_base_url == "https://us.cloud.langfuse.com"


def test_langfuse_base_url_falls_back_to_langfuse_host(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.setenv("LANGFUSE_HOST", "https://us.cloud.langfuse.com")
    assert load_settings().langfuse_base_url == "https://us.cloud.langfuse.com"


def test_langfuse_base_url_defaults_to_the_eu_cloud(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54332/postgres")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert load_settings().langfuse_base_url == "https://cloud.langfuse.com"
