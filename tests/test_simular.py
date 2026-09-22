"""Cubre solo lo que no necesita base de datos ni red: el id determinista, la
forma del perfil que da OfflineReader, el payload firmado, y que argparse
entiende cada subcomando. Los flujos completos (mensaje -> research -> tarjeta,
botón contra el receptor real, etc.) ya los prueba el resto de la suite --
aquí no se repiten.
"""

from __future__ import annotations

import json
import time

import pytest

from handoff_agent import db
from handoff_agent.slack_client import SlackUserNotFound, UserProfile
from handoff_agent.tools import prospects
from handoff_agent.web import slack_signature
from scripts import simular

# -- fake_user_id --


def test_fake_user_id_is_deterministic():
    first = simular.fake_user_id("Ada Ruiz", "Acme")
    second = simular.fake_user_id("Ada Ruiz", "Acme")
    assert first == second


def test_fake_user_id_has_the_documented_prefix_and_length():
    user_id = simular.fake_user_id("Ada Ruiz", "Acme")
    assert user_id.startswith("USIM")
    assert len(user_id) == len("USIM") + 8


def test_fake_user_id_differs_for_different_people():
    assert simular.fake_user_id("Ada Ruiz", "Acme") != simular.fake_user_id("Leo Gil", "Beta")


def test_fake_user_id_does_not_confuse_a_split_boundary():
    """ "AB" + "C" y "A" + "BC" no pueden caer en el mismo id solo por
    concatenar nombre y empresa sin separador."""
    assert simular.fake_user_id("AB", "C") != simular.fake_user_id("A", "BC")


# -- OfflineReader --


def test_offline_reader_never_imports_slack_sdk():
    import ast
    import sys

    assert "slack_sdk" not in vars(simular)
    # El módulo puede acabar cargado igualmente porque handoff_agent.slack_client
    # lo importa (research_runner necesita SlackAuthFailed/SlackUnavailable de
    # ahí); lo que este test fija es que simular.py no lo hace por su cuenta.
    # Se mira el árbol de sentencias `import`, no una búsqueda de texto: el
    # propio docstring del módulo menciona "slack_sdk" en prosa.
    source = sys.modules[simular.__name__].__file__
    with open(source, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "slack_sdk" not in imported


def test_offline_reader_profile_from_an_explicit_override():
    reader = simular.OfflineReader()
    reader.set_profile("USIM1", full_name="Ada Ruiz", company_name="Acme", email="ada@acme.com")
    profile = reader.user_profile("USIM1")
    assert isinstance(profile, UserProfile)
    assert profile.real_name == "Ada Ruiz"
    assert profile.title == "CEO @ Acme"
    assert profile.email == "ada@acme.com"
    assert profile.is_bot is False
    assert profile.deleted is False


def test_offline_reader_profile_without_a_company_has_no_title():
    reader = simular.OfflineReader()
    reader.set_profile("USIM1", full_name="Ada Ruiz", company_name=None, email=None)
    profile = reader.user_profile("USIM1")
    assert profile.title == ""
    assert profile.email == ""


def test_offline_reader_profile_falls_back_to_the_stored_prospect(conn):
    person = prospects.upsert_prospect("USIM2", full_name="Leo Gil")
    db.execute("update prospects set company_name = %s where id = %s", ("Beta", person["id"]))

    reader = simular.OfflineReader()
    profile = reader.user_profile("USIM2")
    assert profile.real_name == "Leo Gil"
    assert profile.title == "CEO @ Beta"


def test_offline_reader_raises_slack_user_not_found_for_an_unknown_person(conn):
    reader = simular.OfflineReader()
    try:
        reader.user_profile("USIMNADIE")
    except SlackUserNotFound:
        return
    raise AssertionError("se esperaba SlackUserNotFound")


def test_offline_reader_permalink_is_a_plausible_slack_url():
    reader = simular.OfflineReader()
    link = reader.permalink("CSIMULADO", "1700000000.000100")
    assert link.startswith("https://")
    assert "CSIMULADO" in link
    assert "1700000000000100" in link


def test_offline_reader_history_and_members_use_what_was_set():
    reader = simular.OfflineReader()
    reader.add_message("C1", {"ts": "100.0", "user": "U1", "text": "hola"})
    reader.set_members("C1", "U1", "U2")
    assert reader.owner_id() == "USIMOWNER"
    assert reader.members("C1") == {"U1", "U2"}
    assert [m["ts"] for m in reader.history("C1", oldest="50.0")] == ["100.0"]
    assert reader.history("C1", oldest="500.0") == []


# -- payload firmado --


def test_the_signed_payload_verifies():
    secret = "s3cr3t"
    payload = simular.build_block_actions_payload("contactado", "pid-1", "CHANDOFF", "1.1")
    body = ("payload=" + json.dumps(payload)).encode()
    timestamp = str(int(time.time()))
    signature = simular.sign(secret, timestamp, body)
    assert slack_signature.verify(secret, timestamp, signature, body)


def test_a_wrong_secret_fails_verification():
    payload = simular.build_block_actions_payload("descartar", "pid-1", "CHANDOFF", "1.1")
    body = ("payload=" + json.dumps(payload)).encode()
    timestamp = str(int(time.time()))
    signature = simular.sign("otro-secreto", timestamp, body)
    assert not slack_signature.verify("s3cr3t", timestamp, signature, body)


def test_the_unsigned_marker_used_by_sin_firma_is_rejected():
    """El valor que cmd_boton manda cuando se pide --sin-firma no puede colar
    como una firma válida."""
    payload = simular.build_block_actions_payload("contactado", "pid-1", "CHANDOFF", "1.1")
    body = ("payload=" + json.dumps(payload)).encode()
    timestamp = str(int(time.time()))
    assert not slack_signature.verify("s3cr3t", timestamp, "v0=firma-invalida", body)


def test_the_payload_carries_the_right_action_and_value():
    payload = simular.build_block_actions_payload("investigar_mas", "abc-123", "CHANDOFF", "9.9")
    assert payload["type"] == "block_actions"
    assert payload["channel"] == {"id": "CHANDOFF"}
    assert payload["actions"] == [{"action_id": "investigar_mas", "value": "abc-123"}]
    assert payload["message"]["ts"] == "9.9"
    # El usuario simulado tiene que parecer un id real de Slack (SLACK_ID_RE en
    # web/app.py) para que la nota de la tarjeta use la mención viva.
    assert payload["user"]["id"].startswith("U")
    assert payload["user"]["id"].isupper()


# -- argparse: solo el parseo, nunca la ejecución --


def test_mensaje_parses_positionals_and_options():
    args = simular.build_parser().parse_args(
        ["mensaje", "Ada Ruiz", "Acme", "hola", "--email", "ada@acme.com", "--canal", "C99"]
    )
    assert args.command == "mensaje"
    assert (args.full_name, args.company, args.message) == ("Ada Ruiz", "Acme", "hola")
    assert args.email == "ada@acme.com"
    assert args.web is None
    assert args.canal == "C99"
    assert args.local is False
    assert args.func is simular.cmd_mensaje


def test_mensaje_defaults_channel_and_has_no_email_or_web():
    args = simular.build_parser().parse_args(["mensaje", "Ada Ruiz", "Acme", "hola"])
    assert args.canal == simular.DEFAULT_CHANNEL
    assert args.email is None
    assert args.web is None


def test_mensaje_accepts_local():
    args = simular.build_parser().parse_args(
        ["mensaje", "Ada Ruiz", "Acme", "hola", "--web", "acme.com", "--local"]
    )
    assert args.web == "acme.com"
    assert args.local is True


def test_cola_defaults_to_twenty():
    args = simular.build_parser().parse_args(["cola"])
    assert args.command == "cola"
    assert args.max == 20
    assert args.func is simular.cmd_cola


def test_cola_accepts_max():
    args = simular.build_parser().parse_args(["cola", "--max", "5"])
    assert args.max == 5


def test_boton_parses_action_and_prospect_id():
    args = simular.build_parser().parse_args(["boton", "contactado", "pid-1"])
    assert args.command == "boton"
    assert args.accion == "contactado"
    assert args.prospect_id == "pid-1"
    assert args.url == "http://127.0.0.1:8000"
    assert args.sin_firma is False
    assert args.func is simular.cmd_boton


def test_boton_rejects_an_unknown_action():
    with pytest.raises(SystemExit):
        simular.build_parser().parse_args(["boton", "algo_raro", "pid-1"])


def test_boton_accepts_url_and_sin_firma():
    args = simular.build_parser().parse_args(
        ["boton", "descartar", "pid-1", "--url", "http://otro:9000", "--sin-firma"]
    )
    assert args.url == "http://otro:9000"
    assert args.sin_firma is True


def test_sms_parses_the_prospect_id():
    args = simular.build_parser().parse_args(["sms", "pid-1"])
    assert args.command == "sms"
    assert args.prospect_id == "pid-1"
    assert args.func is simular.cmd_sms


def test_digest_defaults_to_no_day_and_not_ver():
    args = simular.build_parser().parse_args(["digest"])
    assert args.command == "digest"
    assert args.dia is None
    assert args.ver is False
    assert args.func is simular.cmd_digest


def test_digest_accepts_dia_and_ver():
    args = simular.build_parser().parse_args(["digest", "--dia", "2026-09-20", "--ver"])
    assert args.dia == "2026-09-20"
    assert args.ver is True


def test_estado_parses_the_prospect_id():
    args = simular.build_parser().parse_args(["estado", "pid-1"])
    assert args.command == "estado"
    assert args.prospect_id == "pid-1"
    assert args.func is simular.cmd_estado


def test_limpiar_defaults_to_asking_for_confirmation():
    args = simular.build_parser().parse_args(["limpiar"])
    assert args.command == "limpiar"
    assert args.si is False
    assert args.func is simular.cmd_limpiar


def test_limpiar_accepts_si():
    args = simular.build_parser().parse_args(["limpiar", "--si"])
    assert args.si is True


def test_a_missing_command_is_a_usage_error():
    with pytest.raises(SystemExit):
        simular.build_parser().parse_args([])


# -- validación de dominio (usada por --web) --


def test_validate_domain_accepts_a_bare_domain():
    simular._validate_domain("acme.com")  # no debe levantar


def test_validate_domain_rejects_a_url():
    with pytest.raises(SystemExit):
        simular._validate_domain("https://acme.com")


# -- puerta de DATABASE_URL --


def test_gate_refuses_a_non_cloud_url_without_local(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/handoff")
    args = simular.build_parser().parse_args(["cola"])
    with pytest.raises(SystemExit):
        simular._gate(args)


def test_gate_accepts_a_non_cloud_url_with_local(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/handoff")
    args = simular.build_parser().parse_args(["cola", "--local"])
    simular._gate(args)  # no debe levantar


def test_gate_warns_loudly_for_a_cloud_url(monkeypatch, capsys):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres:pw@db.siouhwfdkhzchnqlcott.supabase.com:5432/postgres",
    )
    args = simular.build_parser().parse_args(["cola"])
    simular._gate(args)
    out = capsys.readouterr().out
    assert "Supabase Cloud" in out
    assert "db.siouhwfdkhzchnqlcott.supabase.com" in out


def test_gate_requires_database_url_at_all(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    args = simular.build_parser().parse_args(["cola"])
    with pytest.raises(SystemExit):
        simular._gate(args)
