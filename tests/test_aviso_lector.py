"""Aviso en Slack cuando caduca la credencial del lector."""

from datetime import UTC, datetime, timedelta

import pytest

from handoff_agent import db
from handoff_agent.delivery import aviso_lector


@pytest.fixture
def posted(monkeypatch):
    enviados = []

    def post_card(blocks, text):
        enviados.append({"blocks": blocks, "text": text})
        return "CHANDOFF", "1.1"

    monkeypatch.setattr(aviso_lector.slack_writer, "post_card", post_card)
    return enviados


def avisos():
    return db.fetch_all("select * from agent_actions where action = 'aviso_credencial_lector'")


def test_the_first_failure_posts_a_short_english_notice(conn, posted, monkeypatch):
    monkeypatch.setenv("PANEL_URL", "https://panel.example.com")
    assert aviso_lector.avisar_credencial_caducada() is True
    (aviso,) = posted
    assert aviso["text"] == (
        "⚠️ I can no longer read Slack — the reader credential expired. "
        "Paste a fresh one in the panel → https://panel.example.com/ajustes"
    )
    assert aviso["blocks"][0]["text"]["text"] == aviso["text"]
    assert len(avisos()) == 1


def test_without_panel_url_there_is_no_link(conn, posted, monkeypatch):
    monkeypatch.delenv("PANEL_URL", raising=False)
    aviso_lector.avisar_credencial_caducada()
    assert posted[0]["text"].endswith("Paste a fresh one in the panel.")
    assert "http" not in posted[0]["text"]


def test_it_is_not_repeated_inside_the_window(conn, posted):
    assert aviso_lector.avisar_credencial_caducada() is True
    soon = datetime.now(UTC) + timedelta(hours=5, minutes=59)
    assert aviso_lector.avisar_credencial_caducada(now=soon) is False
    assert aviso_lector.avisar_credencial_caducada() is False
    assert len(posted) == 1


def test_it_is_posted_again_once_the_window_has_passed(conn, posted):
    aviso_lector.avisar_credencial_caducada()
    later = datetime.now(UTC) + timedelta(hours=6, minutes=1)
    assert aviso_lector.avisar_credencial_caducada(now=later) is True
    assert len(posted) == 2


def test_the_window_comes_from_config(conn, posted, restore_config):
    db.execute("update config set value = '1'::jsonb where key = 'aviso_token_horas'")
    aviso_lector.avisar_credencial_caducada()
    assert aviso_lector.avisar_credencial_caducada(
        now=datetime.now(UTC) + timedelta(hours=1, minutes=1)
    )
    assert len(posted) == 2


def test_a_failure_posting_is_recorded_and_never_raised(conn, monkeypatch):
    def boom(blocks, text):
        raise RuntimeError("channel_not_found")

    monkeypatch.setattr(aviso_lector.slack_writer, "post_card", boom)
    assert aviso_lector.avisar_credencial_caducada() is False
    (fallo,) = db.fetch_all(
        "select payload from agent_actions where action = 'aviso_credencial_fallido'"
    )
    assert "channel_not_found" in fallo["payload"]["motivo"]
    # Y no se reintenta en caliente: el intento ya cuenta para la ventana.
    assert aviso_lector.avisar_credencial_caducada() is False


def test_without_a_bot_configured_it_fails_quietly(conn):
    # conftest quita HANDOFF_SLACK_BOT_TOKEN: post_card real rechaza la escritura.
    assert aviso_lector.avisar_credencial_caducada() is False
    assert len(db.fetch_all("select 1 from agent_actions where action like 'aviso_%%'")) == 2


def test_if_the_attempt_cannot_be_recorded_nothing_is_posted(conn, posted, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(aviso_lector.ledger, "record_action", boom)
    assert aviso_lector.avisar_credencial_caducada() is False
    assert posted == []


def test_a_broken_database_never_raises(conn, posted, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(aviso_lector.db, "fetch_one", boom)
    assert aviso_lector.avisar_credencial_caducada() is False
