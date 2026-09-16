from handoff_agent import db, db_config


def test_value_reads_the_row(conn):
    assert db_config.value("sms_por_dia", 99) == 3


def test_value_falls_back_when_the_row_is_missing(conn):
    assert db_config.value("no_existe", 7) == 7


def test_value_falls_back_when_the_type_is_wrong(conn, caplog):
    """Un valor de tipo distinto al del defecto no vale, sea cual sea ese tipo.

    config es tabla de sesión (no está en TABLES_TO_CLEAN): hay que devolver
    sms_por_dia a como lo deja la migración o el siguiente test la hereda rota.
    """
    db.execute("update config set value = '\"tres\"'::jsonb where key = 'sms_por_dia'")
    try:
        assert db_config.value("sms_por_dia", 3) == 3
        assert "sms_por_dia" in caplog.text
    finally:
        db.execute("update config set value = '3'::jsonb where key = 'sms_por_dia'")


def test_value_reads_a_real_stored_true(conn):
    db.execute("update config set value = 'true'::jsonb where key = 'kill_switch'")
    try:
        assert db_config.value("kill_switch", False) is True
    finally:
        db.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")


def test_value_reads_a_real_stored_false(conn):
    # kill_switch ya sale en false de la migración; lo dejamos explícito para
    # que el test no dependa de ese orden.
    db.execute("update config set value = 'false'::jsonb where key = 'kill_switch'")
    assert db_config.value("kill_switch", True) is False


def test_value_rejects_a_stored_true_when_the_default_is_an_int(conn, caplog):
    """bool es subclase de int en Python: sin este caso aparte, un true
    guardado colaría como 1 para un default entero."""
    db.execute("update config set value = 'true'::jsonb where key = 'sms_por_dia'")
    try:
        assert db_config.value("sms_por_dia", 3) == 3
        assert "sms_por_dia" in caplog.text
    finally:
        db.execute("update config set value = '3'::jsonb where key = 'sms_por_dia'")
