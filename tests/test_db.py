from handoff_agent import db


def test_fetch_one_returns_dict(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id, full_name) values ('U1', 'Ada')")
    row = db.fetch_one(
        "select slack_user_id, full_name from prospects where slack_user_id = %s", ("U1",)
    )
    assert row == {"slack_user_id": "U1", "full_name": "Ada"}


def test_fetch_one_returns_none_when_no_match(conn):
    assert db.fetch_one("select 1 as n from prospects where slack_user_id = %s", ("nope",)) is None


def test_fetch_all_returns_every_row(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U1'), ('U2')")
    rows = db.fetch_all("select slack_user_id from prospects order by slack_user_id")
    assert [r["slack_user_id"] for r in rows] == ["U1", "U2"]


def test_execute_returns_affected_row_count(conn):
    with conn.cursor() as cur:
        cur.execute("insert into prospects (slack_user_id) values ('U1'), ('U2')")
    assert db.execute("update prospects set state = 'investigado'") == 2
