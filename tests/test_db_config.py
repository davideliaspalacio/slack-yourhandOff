from handoff_agent import db, db_config


def test_value_reads_the_row(conn):
    assert db_config.value("sms_por_dia", 99) == 3


def test_value_falls_back_when_the_row_is_missing(conn):
    assert db_config.value("no_existe", 7) == 7


def test_value_falls_back_when_the_type_is_wrong(conn, caplog):
    """psycopg decodifica jsonb a Python: true llegaría como 1 y una cadena
    reventaría la comparación. Solo vale un valor del tipo del defecto."""
    db.execute("update config set value = '\"tres\"'::jsonb where key = 'sms_por_dia'")
    assert db_config.value("sms_por_dia", 3) == 3
    assert "sms_por_dia" in caplog.text
