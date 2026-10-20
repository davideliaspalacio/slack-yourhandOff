"""El "What's new" después de cada despliegue: sale una vez, con todo lo pendiente, y
nunca se repite ni se pierde por un fallo de Slack."""

import json

import pytest

from handoff_agent import db
from handoff_agent.delivery import novedades
from handoff_agent.delivery.novedades import Novedad

UNA = Novedad(
    "2026-10-20-hilos", "Reading thread replies", ("It reads replies.", "Off by default.")
)
OTRA = Novedad("2026-10-21-otra", "Something else", ("One line.",))


@pytest.fixture(autouse=True)
def slack(monkeypatch):
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    publicados = []

    def post_card(blocks, text):
        publicados.append({"json": json.dumps(blocks, ensure_ascii=False), "text": text})
        return "CHANDOFF", f"1700000000.{len(publicados):06d}"

    monkeypatch.setattr(novedades.slack_writer, "post_card", post_card)
    return publicados


def con(monkeypatch, *lista):
    monkeypatch.setattr(novedades, "NOVEDADES", tuple(lista))


def test_with_nothing_to_announce_nothing_is_posted(conn, slack, monkeypatch):
    con(monkeypatch)
    assert novedades.publicar_pendientes() == 0
    assert slack == []


def test_pending_entries_go_out_in_one_message(conn, slack, monkeypatch):
    con(monkeypatch, UNA, OTRA)
    assert novedades.publicar_pendientes() == 2
    assert len(slack) == 1
    assert "What's new" in slack[0]["json"]
    assert "Reading thread replies" in slack[0]["json"]
    assert "Something else" in slack[0]["json"]
    assert "• It reads replies." in slack[0]["json"]


def test_an_entry_is_announced_only_once(conn, slack, monkeypatch):
    """Reiniciar el worker, o desplegar sin entradas nuevas, no vuelve a publicar."""
    con(monkeypatch, UNA)
    novedades.publicar_pendientes()
    assert novedades.publicar_pendientes() == 0
    assert len(slack) == 1


def test_only_the_new_entry_goes_out_next_time(conn, slack, monkeypatch):
    con(monkeypatch, UNA)
    novedades.publicar_pendientes()
    con(monkeypatch, UNA, OTRA)
    assert novedades.publicar_pendientes() == 1
    assert "Something else" in slack[1]["json"]
    assert "Reading thread replies" not in slack[1]["json"]


def test_a_slack_failure_leaves_it_pending_for_the_next_round(conn, slack, monkeypatch):
    con(monkeypatch, UNA)

    def boom(blocks, text):
        raise RuntimeError("slack caído")

    monkeypatch.setattr(novedades.slack_writer, "post_card", boom)
    assert novedades.publicar_pendientes() == 0
    assert (
        db.fetch_one(
            "select count(*) as n from agent_actions where action = %s", (novedades.ACCION,)
        )["n"]
        == 0
    )


def test_the_kill_switch_holds_it_back(conn, slack, monkeypatch, restore_config):
    con(monkeypatch, UNA)
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    assert novedades.publicar_pendientes() == 0
    assert slack == []


def test_without_the_bot_configured_nothing_is_posted(conn, slack, monkeypatch):
    con(monkeypatch, UNA)
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN", raising=False)
    assert novedades.publicar_pendientes() == 0


def test_it_never_raises(conn, monkeypatch):
    def boom():
        raise RuntimeError("base caída")

    monkeypatch.setattr(novedades, "_publicar", boom)
    assert novedades.publicar_pendientes() == 0


def test_ids_are_unique():
    """Un id repetido haría que una entrada nueva se diera por anunciada."""
    ids = [n.id for n in novedades.NOVEDADES]
    assert len(ids) == len(set(ids))


def test_every_entry_has_a_title_and_lines():
    for n in novedades.NOVEDADES:
        assert n.titulo.strip() and n.lineas and all(linea.strip() for linea in n.lineas)
