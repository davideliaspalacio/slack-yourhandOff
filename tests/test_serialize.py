import uuid
from datetime import UTC, datetime

from handoff_agent import mcp_server, serialize


def test_jsonable_turns_datetimes_and_uuids_into_strings():
    moment = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert serialize.jsonable({"at": moment, "id": ident, "n": 3}) == {
        "at": "2026-09-10T12:00:00+00:00",
        "id": "12345678-1234-5678-1234-567812345678",
        "n": 3,
    }
    assert serialize.jsonable(None) is None


def test_the_mcp_server_reuses_the_same_function():
    assert mcp_server.jsonable is serialize.jsonable
