import http.client
from urllib.error import URLError

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

from handoff_agent import slack_client as sc

# Escrita a mano a propósito, igual que las listas blancas del MCP: añadir un método
# obliga a venir aquí y comprobar que de verdad no escribe en el Founders Club.
READ_METHODS = {
    "auth_test",
    "conversations_history",
    "conversations_info",
    "conversations_list",
    "conversations_members",
    # Las respuestas de un hilo (0032): solo lista, no escribe ni marca como leído.
    "conversations_replies",
    "users_info",
    "chat_getPermalink",
}

# Métodos que no pueden entrar en la lista de arriba por mucho que parezcan
# inofensivos. `conversations.mark` no publica nada, pero marcaría como leídos
# los mensajes directos del dueño de la credencial: el agente le borraría sus
# notificaciones. El rastro de los DMs (ingest/dms.py) lee y no toca nada.
JAMAS = {
    "conversations_mark",
    "conversations_setTopic",
    "chat_postMessage",
    "chat_update",
    "chat_delete",
    "reactions_add",
    "conversations_join",
    "conversations_invite",
    "files_upload_v2",
}


class FakeWebClient:
    """Doble del WebClient: respuestas en cola y registro de cada llamada."""

    def __init__(
        self,
        *,
        history_pages=(),
        member_pages=(),
        dm_pages=(),
        users=None,
        error=None,
        permalink="https://handoff.slack.com/archives/C1/p1726000000000100",
        channel=None,
    ):
        self.history_pages = list(history_pages)
        self.member_pages = list(member_pages)
        self.dm_pages = list(dm_pages)
        self.users = users or {}
        self.error = error
        self.permalink_response = permalink
        self.channel = channel if channel is not None else {"id": "C1", "name": "founders-general"}
        self.calls = []

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.error:
            raise self.error

    def auth_test(self, **kwargs):
        self._record("auth_test", kwargs)
        return {"ok": True, "user_id": "UOWNER", "team": "Handoff", "team_id": "T0FOUNDERS"}

    def conversations_history(self, **kwargs):
        self._record("conversations_history", kwargs)
        return self.history_pages.pop(0)

    def conversations_list(self, **kwargs):
        self._record("conversations_list", kwargs)
        return self.dm_pages.pop(0)

    def conversations_mark(self, **kwargs):  # nunca debería llamarse
        self._record("conversations_mark", kwargs)
        return {"ok": True}

    def conversations_members(self, **kwargs):
        self._record("conversations_members", kwargs)
        return self.member_pages.pop(0)

    def users_info(self, **kwargs):
        self._record("users_info", kwargs)
        return {"ok": True, "user": self.users[kwargs["user"]]}

    def chat_getPermalink(self, **kwargs):
        self._record("chat_getPermalink", kwargs)
        return {"ok": True, "permalink": self.permalink_response}

    def conversations_info(self, **kwargs):
        self._record("conversations_info", kwargs)
        return {"ok": True, "channel": self.channel}

    def chat_postMessage(self, **kwargs):  # nunca debería llamarse
        self._record("chat_postMessage", kwargs)
        return {"ok": True}


def test_the_reader_only_exposes_reads():
    """El agente nunca escribe en el Founders Club: la superficie queda fijada."""
    public = {name for name in dir(sc.FoundersClubReader) if not name.startswith("_")}
    assert public == {
        "identity",
        "owner_id",
        "team_id",
        "history",
        "last_message",
        "dms",
        "canales",
        "members",
        "user_profile",
        "permalink",
        "channel_name",
        # Lo no leído de un DM (0031): conversations.info, la misma llamada que
        # channel_name. Preguntar no marca nada como leído.
        "dm_estado",
        # Las respuestas de un hilo (0032): conversations.replies, solo lista.
        "respuestas",
    }


def test_identity_returns_user_and_team_and_owner_id_still_works():
    reader = sc.FoundersClubReader(client=FakeWebClient())
    assert reader.identity() == ("UOWNER", "Handoff")
    assert reader.owner_id() == "UOWNER"


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


def test_a_missing_token_is_an_auth_failure(monkeypatch):
    monkeypatch.setattr(sc.secretos, "slack_user_token", lambda: None)
    with pytest.raises(sc.SlackAuthFailed, match="SLACK_USER_TOKEN"):
        sc.FoundersClubReader().owner_id()


def test_a_reader_without_token_resolves_it_at_call_time(monkeypatch):
    tokens = iter(["xoxp-primero", "xoxp-segundo"])
    monkeypatch.setattr(sc.secretos, "slack_user_token", lambda: next(tokens))
    reader = sc.FoundersClubReader()  # no falla al construirse sin token
    seen = []

    def fake_web_client(token, cookie=None):
        seen.append(token)
        return FakeWebClient()

    monkeypatch.setattr(sc, "_web_client", fake_web_client)
    assert reader.owner_id() == "UOWNER"
    assert reader.owner_id() == "UOWNER"
    assert seen == ["xoxp-primero", "xoxp-segundo"]


@pytest.mark.parametrize(
    "error",
    [
        URLError("dns"),
        TimeoutError("timed out"),
        http.client.RemoteDisconnected("closed"),
    ],
)
def test_transport_errors_are_retryable(error):
    fake = FakeWebClient(error=error)
    with pytest.raises(sc.SlackUnavailable):
        sc.FoundersClubReader(client=fake).members("C1")


def test_the_real_client_retries_on_rate_limits_and_connection_errors():
    reader = sc.FoundersClubReader(token="xoxp-test")
    handlers = reader._client.retry_handlers
    assert any(isinstance(h, RateLimitErrorRetryHandler) for h in handlers)
    # ConnectionErrorRetryHandler is included via default_retry_handlers()
    from slack_sdk.http_retry.builtin_handlers import ConnectionErrorRetryHandler

    assert any(isinstance(h, ConnectionErrorRetryHandler) for h in handlers)


def test_only_read_methods_ever_reach_slack():
    fake = FakeWebClient(
        history_pages=[{"ok": True, "messages": [], "has_more": False}, {"ok": True}],
        member_pages=[{"ok": True, "members": [], "response_metadata": {}}],
        users={"U1": {"real_name": "Ada", "profile": {}}},
        dm_pages=[{"ok": True, "channels": []}],
    )
    reader = sc.FoundersClubReader(client=fake)
    reader.owner_id()
    reader.history("C1", oldest="0")
    reader.members("C1")
    reader.user_profile("U1")
    reader.permalink("C1", "1726000000.000100")
    reader.channel_name("C1")
    reader.team_id()
    reader.dms()
    reader.last_message("D1")
    assert {name for name, _ in fake.calls} <= READ_METHODS
    # La lista del código es la misma que vigila este test, y `_call` la impone.
    assert set(sc.READ_METHODS) == READ_METHODS
    with pytest.raises(RuntimeError, match="no es de lectura"):
        reader._call("chat_postMessage", channel="C1", text="hola")
    assert "chat_postMessage" not in {name for name, _ in fake.calls}


def test_no_write_method_is_ever_whitelisted_or_called():
    """Ni uno de los de JAMAS, y `conversations.mark` el primero: leer el
    rastro de sus DMs no puede marcárselos como leídos."""
    assert not (READ_METHODS & JAMAS)
    assert not (set(sc.READ_METHODS) & JAMAS)
    fake = FakeWebClient(
        history_pages=[{"ok": True, "messages": [{"ts": "9", "user": "U1"}]}],
        member_pages=[{"ok": True, "members": [], "response_metadata": {}}],
        users={"U1": {"real_name": "Ada", "profile": {}}},
        dm_pages=[{"ok": True, "channels": [{"id": "D1", "user": "U1", "is_im": True}]}],
    )
    reader = sc.FoundersClubReader(client=fake)
    # Todo el camino del rastro de los DMs, de punta a punta.
    reader.owner_id()
    reader.team_id()
    for conversacion in reader.dms():
        reader.last_message(conversacion["id"])
        reader.user_profile(conversacion["user"])
    llamados = {name for name, _ in fake.calls}
    assert not (llamados & JAMAS)
    for metodo in sorted(JAMAS):
        with pytest.raises(RuntimeError, match="no es de lectura"):
            reader._call(metodo, channel="D1")
    assert not ({name for name, _ in fake.calls} & JAMAS)


def test_the_dm_list_asks_only_for_one_to_one_conversations_and_follows_cursors():
    fake = FakeWebClient(
        dm_pages=[
            {
                "ok": True,
                "channels": [{"id": "D1", "user": "U1", "is_im": True}],
                "response_metadata": {"next_cursor": "siguiente"},
            },
            {"ok": True, "channels": [{"id": "D2", "user": "U2", "is_im": True}]},
        ]
    )
    reader = sc.FoundersClubReader(client=fake)
    assert [c["id"] for c in reader.dms()] == ["D1", "D2"]
    assert fake.calls[0][1]["types"] == "im"
    assert fake.calls[1][1]["cursor"] == "siguiente"


def test_the_last_message_is_a_single_message_and_nothing_else():
    fake = FakeWebClient(
        history_pages=[{"ok": True, "messages": [{"ts": "9", "user": "U1", "text": "hola"}]}]
    )
    reader = sc.FoundersClubReader(client=fake)
    assert reader.last_message("D1")["ts"] == "9"
    # limit=1: ni paginación ni historial entero de una conversación privada.
    assert fake.calls[0][1] == {"channel": "D1", "limit": 1}
    assert len(fake.calls) == 1


def test_an_empty_dm_has_no_last_message():
    fake = FakeWebClient(history_pages=[{"ok": True, "messages": []}])
    reader = sc.FoundersClubReader(client=fake)
    assert reader.last_message("D1") is None


def test_team_id_comes_from_auth_test():
    fake = FakeWebClient()
    reader = sc.FoundersClubReader(client=fake)
    assert reader.team_id() == "T0FOUNDERS"
    # identity() sigue devolviendo lo de siempre: el nombre, no el código.
    assert reader.identity() == ("UOWNER", "Handoff")


def test_permalink_asks_slack_for_the_link(monkeypatch):
    calls = []

    class FakeWeb:
        def chat_getPermalink(self, **kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "permalink": "https://handoff.slack.com/archives/C1/p1726000000000100",
            }

    reader = sc.FoundersClubReader(token="xoxp-test", client=FakeWeb())
    assert reader.permalink("C1", "1726000000.000100").endswith("p1726000000000100")
    assert calls[0] == {"channel": "C1", "message_ts": "1726000000.000100"}


def test_permalink_returns_none_when_slack_says_no(monkeypatch):
    """Una tarjeta sin enlace sigue siendo útil; una excepción aquí tumbaría
    la entrega entera."""

    class FakeWeb:
        def chat_getPermalink(self, **kwargs):
            raise sc.SlackApiError("message_not_found", response={"error": "message_not_found"})

    reader = sc.FoundersClubReader(token="xoxp-test", client=FakeWeb())
    assert reader.permalink("C1", "1.0") is None


def test_an_unknown_user_is_its_own_error_but_still_slack_unavailable():
    """user_not_found no se arregla reintentando: el runner lo distingue, y
    quien ya capturaba SlackUnavailable sigue funcionando igual."""
    fake = FakeWebClient(error=SlackApiError("nope", {"ok": False, "error": "user_not_found"}))
    with pytest.raises(sc.SlackUserNotFound) as caught:
        sc.FoundersClubReader(client=fake).user_profile("UGONE")
    assert isinstance(caught.value, sc.SlackUnavailable)


def _capturar_web_client(monkeypatch, token, cookie):
    """Resuelve el cliente real del lector con esas credenciales y lo devuelve."""
    monkeypatch.setattr(sc.secretos, "slack_user_token", lambda: token)
    monkeypatch.setattr(sc.secretos, "slack_d_cookie", lambda: cookie)
    return sc.FoundersClubReader()._resolve_client()


def test_a_session_token_sends_the_d_cookie_header(monkeypatch):
    client = _capturar_web_client(monkeypatch, "xoxc-1234567890-abcdef", "xoxd-galleta")
    assert client.token == "xoxc-1234567890-abcdef"
    assert client.headers["Cookie"] == "d=xoxd-galleta"


def test_a_session_token_without_the_cookie_is_an_auth_failure(monkeypatch):
    monkeypatch.setattr(sc.secretos, "slack_user_token", lambda: "xoxc-1234567890-abcdef")
    monkeypatch.setattr(sc.secretos, "slack_d_cookie", lambda: None)
    with pytest.raises(sc.SlackAuthFailed, match="cookie d"):
        sc.FoundersClubReader().owner_id()


def test_an_app_token_sends_no_cookie_even_if_one_is_stored(monkeypatch):
    client = _capturar_web_client(monkeypatch, "xoxp-1234567890-abcdef", "xoxd-sobra")
    assert "Cookie" not in client.headers


def test_a_refreshed_cookie_rebuilds_the_client(monkeypatch):
    cookies = iter(["xoxd-vieja", "xoxd-nueva"])
    monkeypatch.setattr(sc.secretos, "slack_user_token", lambda: "xoxc-1234567890-abcdef")
    monkeypatch.setattr(sc.secretos, "slack_d_cookie", lambda: next(cookies))
    reader = sc.FoundersClubReader()
    assert reader._resolve_client().headers["Cookie"] == "d=xoxd-vieja"
    assert reader._resolve_client().headers["Cookie"] == "d=xoxd-nueva"


def test_an_invalid_auth_alert_says_the_credential_may_have_expired():
    fake = FakeWebClient(error=SlackApiError("x", {"ok": False, "error": "invalid_auth"}))
    with pytest.raises(sc.SlackAuthFailed, match="caducado.*Settings del panel"):
        sc.FoundersClubReader(client=fake).owner_id()


def test_channel_name_asks_conversations_info():
    fake = FakeWebClient(channel={"id": "C1", "name": "founders-general"})
    reader = sc.FoundersClubReader(client=fake)
    assert reader.channel_name("C1") == "founders-general"
    assert fake.calls == [("conversations_info", {"channel": "C1"})]


@pytest.mark.parametrize("respuesta", [{"id": "C1"}, {}])
def test_a_channel_without_a_name_comes_back_empty(respuesta):
    reader = sc.FoundersClubReader(client=FakeWebClient(channel=respuesta))
    assert reader.channel_name("C1") == ""


def test_a_failing_channel_name_raises_like_any_other_read():
    error = SlackApiError("x", response={"ok": False, "error": "channel_not_found"})
    reader = sc.FoundersClubReader(client=FakeWebClient(error=error))
    with pytest.raises(sc.SlackUnavailable, match="channel_not_found"):
        reader.channel_name("C1")
