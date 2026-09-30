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
