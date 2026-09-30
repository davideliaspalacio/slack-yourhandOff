from handoff_agent import db, secretos


def test_the_database_value_wins_over_the_environment(conn, monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-del-entorno")
    db.execute("insert into secretos (key, value) values ('slack_user_token', 'xoxp-del-panel')")
    assert secretos.slack_user_token() == "xoxp-del-panel"


def test_the_environment_is_the_fallback_when_there_is_no_row(conn, monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-del-entorno")
    assert secretos.slack_user_token() == "xoxp-del-entorno"


def test_none_when_neither_source_has_a_token(conn):
    assert secretos.slack_user_token() is None


def test_the_cookie_database_value_wins_over_the_environment(conn, monkeypatch):
    monkeypatch.setenv("SLACK_D_COOKIE", "xoxd-del-entorno")
    db.execute("insert into secretos (key, value) values ('slack_d_cookie', 'xoxd-del-panel')")
    assert secretos.slack_d_cookie() == "xoxd-del-panel"


def test_the_cookie_environment_is_the_fallback_when_there_is_no_row(conn, monkeypatch):
    monkeypatch.setenv("SLACK_D_COOKIE", "xoxd-del-entorno")
    assert secretos.slack_d_cookie() == "xoxd-del-entorno"


def test_no_cookie_when_neither_source_has_one(conn):
    assert secretos.slack_d_cookie() is None


def _guardar_canales_en_la_base(canales):
    import json

    db.execute(
        "update config set value = %s::jsonb where key = 'slack_channel_ids'",
        (json.dumps(canales),),
    )


def test_the_database_channels_win_over_the_environment(restore_config, monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "CENV1111,CENV2222")
    _guardar_canales_en_la_base(["CPANEL111", "CPANEL222"])
    assert secretos.slack_channel_ids() == ("CPANEL111", "CPANEL222")


def test_the_environment_channels_are_the_fallback(restore_config, monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "CENV1111, CENV2222")
    # La fila existe pero vacía (como la deja la migración): gana el entorno.
    assert secretos.slack_channel_ids() == ("CENV1111", "CENV2222")
    db.execute("delete from config where key = 'slack_channel_ids'")
    assert secretos.slack_channel_ids() == ("CENV1111", "CENV2222")


def test_a_malformed_database_value_falls_back_to_the_environment(restore_config, monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "CENV1111")
    db.execute("update config set value = '\"C0ABC123\"'::jsonb where key = 'slack_channel_ids'")
    assert secretos.slack_channel_ids() == ("CENV1111",)


def test_no_channels_when_neither_source_has_any(restore_config):
    assert secretos.slack_channel_ids() == ()
