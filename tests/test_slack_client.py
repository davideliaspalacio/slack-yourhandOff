import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from handoff_agent import slack_client as sc

READ_METHODS = {"auth_test", "conversations_history", "conversations_members", "users_info"}


class FakeWebClient:
    """Doble del WebClient: respuestas en cola y registro de cada llamada."""

    def __init__(self, *, history_pages=(), member_pages=(), users=None, error=None):
        self.history_pages = list(history_pages)
        self.member_pages = list(member_pages)
        self.users = users or {}
        self.error = error
        self.calls = []

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.error:
            raise self.error

    def auth_test(self, **kwargs):
        self._record("auth_test", kwargs)
        return {"ok": True, "user_id": "UOWNER"}

    def conversations_history(self, **kwargs):
        self._record("conversations_history", kwargs)
        return self.history_pages.pop(0)

    def conversations_members(self, **kwargs):
        self._record("conversations_members", kwargs)
        return self.member_pages.pop(0)

    def users_info(self, **kwargs):
        self._record("users_info", kwargs)
        return {"ok": True, "user": self.users[kwargs["user"]]}


def test_the_reader_only_exposes_reads():
    """El agente nunca escribe en el Founders Club: la superficie queda fijada."""
    public = {name for name in dir(sc.FoundersClubReader) if not name.startswith("_")}
    assert public == {"owner_id", "history", "members", "user_profile"}


def test_history_follows_cursors_across_pages():
    fake = FakeWebClient(
        history_pages=[
            {
                "ok": True,
                "messages": [{"ts": "2"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "abc"},
            },
            {"ok": True, "messages": [{"ts": "1"}], "has_more": False},
        ]
    )
    reader = sc.FoundersClubReader(client=fake)
    assert [m["ts"] for m in reader.history("C1", oldest="0")] == ["2", "1"]
    assert fake.calls[1][1]["cursor"] == "abc"
    assert fake.calls[0][1]["oldest"] == "0"


def test_members_follows_cursors_and_returns_a_set():
    fake = FakeWebClient(
        member_pages=[
            {"ok": True, "members": ["U1", "U2"], "response_metadata": {"next_cursor": "x"}},
            {"ok": True, "members": ["U3"], "response_metadata": {"next_cursor": ""}},
        ]
    )
    assert sc.FoundersClubReader(client=fake).members("C1") == {"U1", "U2", "U3"}


def test_user_profile_maps_the_fields_we_use():
    fake = FakeWebClient(
        users={
            "U1": {
                "real_name": "Ada Ruiz",
                "is_bot": False,
                "deleted": False,
                "profile": {"title": "CEO @ Acme", "email": "ada@acme.com"},
            }
        }
    )
    profile = sc.FoundersClubReader(client=fake).user_profile("U1")
    assert profile == sc.UserProfile("U1", "Ada Ruiz", "CEO @ Acme", "ada@acme.com", False, False)


@pytest.mark.parametrize("error", ["invalid_auth", "token_revoked", "missing_scope"])
def test_auth_errors_need_a_human(error):
    fake = FakeWebClient(error=SlackApiError("nope", {"ok": False, "error": error}))
    with pytest.raises(sc.SlackAuthFailed, match=error):
        sc.FoundersClubReader(client=fake).owner_id()


def test_other_slack_errors_are_retryable():
    fake = FakeWebClient(error=SlackApiError("nope", {"ok": False, "error": "internal_error"}))
    with pytest.raises(sc.SlackUnavailable):
        sc.FoundersClubReader(client=fake).members("C1")


def test_a_missing_token_is_an_auth_failure():
    with pytest.raises(sc.SlackAuthFailed, match="SLACK_USER_TOKEN"):
        sc.FoundersClubReader(token=None)


def test_the_real_client_retries_on_rate_limits():
    reader = sc.FoundersClubReader(token="xoxp-test")
    assert any(isinstance(h, RateLimitErrorRetryHandler) for h in reader._client.retry_handlers)


def test_only_read_methods_ever_reach_slack():
    fake = FakeWebClient(
        history_pages=[{"ok": True, "messages": [], "has_more": False}],
        member_pages=[{"ok": True, "members": [], "response_metadata": {}}],
        users={"U1": {"real_name": "Ada", "profile": {}}},
    )
    reader = sc.FoundersClubReader(client=fake)
    reader.owner_id()
    reader.history("C1", oldest="0")
    reader.members("C1")
    reader.user_profile("U1")
    assert {name for name, _ in fake.calls} <= READ_METHODS
