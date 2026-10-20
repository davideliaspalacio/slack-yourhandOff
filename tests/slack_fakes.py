"""A reader with the same surface as FoundersClubReader, for the ingest tests."""

from handoff_agent.slack_client import SlackUnavailable, SlackUserNotFound, UserProfile


class FakeReader:
    def __init__(self, owner: str = "UOWNER"):
        self.owner = owner
        self.messages: dict[str, list[dict]] = {}
        self.channel_members: dict[str, set[str]] = {}
        self.profiles: dict[str, UserProfile] = {}
        self.auth_error: Exception | None = None
        self.unavailable_channels: set[str] = set()
        self.history_calls: list[tuple[str, str]] = []
        self.channel_names: dict[str, str] = {}
        self.name_calls: list[str] = []
        self.team = "T0OWNER"
        # Las conversaciones directas, tal como las devuelve conversations.list.
        self.conversations: list[dict] = []
        self.last_message_calls: list[str] = []
        self.dms_error: Exception | None = None
        # Los canales del workspace, tal como los devuelve conversations.list, y el
        # registro de a quién se le pidió la lista de miembros: es la llamada más cara
        # que hay y el modo automático la hace a su propio ritmo.
        self.workspace: list[dict] = []
        self.member_calls: list[str] = []
        self.canales_error: Exception | None = None
        # Lo no leído de cada DM, tal como lo daría conversations.info, y a quién se
        # le preguntó (0031).
        self.estados: dict[str, dict] = {}
        self.estado_calls: list[str] = []

    # --- la misma superficie que FoundersClubReader ---
    def identity(self) -> tuple[str, str]:
        if self.auth_error:
            raise self.auth_error
        return self.owner, "Handoff"

    def owner_id(self) -> str:
        return self.identity()[0]

    def history(self, channel: str, oldest: str) -> list[dict]:
        self.history_calls.append((channel, oldest))
        if channel in self.unavailable_channels:
            raise SlackUnavailable(f"{channel}: internal_error")
        fresh = [m for m in self.messages.get(channel, []) if float(m["ts"]) > float(oldest)]
        # Como el Slack real: primero lo más nuevo. Devolverlos en orden de
        # inserción escondía que el watcher los guardaba de nuevo a viejo.
        return sorted(fresh, key=lambda m: float(m["ts"]), reverse=True)

    def channel_name(self, channel: str) -> str:
        self.name_calls.append(channel)
        if self.auth_error:
            raise self.auth_error
        if channel in self.unavailable_channels:
            raise SlackUnavailable(f"conversations_info: {channel}: internal_error")
        return self.channel_names.get(channel, "")

    def members(self, channel: str) -> set[str]:
        self.member_calls.append(channel)
        return set(self.channel_members.get(channel, set()))

    def canales(self, incluir_privados: bool = True) -> list[dict]:
        if self.auth_error:
            raise self.auth_error
        if self.canales_error:
            raise self.canales_error
        return list(self.workspace)

    def user_profile(self, user_id: str) -> UserProfile:
        if self.auth_error:
            raise self.auth_error
        if user_id not in self.profiles:
            # Como el Slack real: users.info con un id que no existe.
            raise SlackUserNotFound("users_info: user_not_found")
        return self.profiles[user_id]

    def permalink(self, channel: str, ts: str) -> str | None:
        return f"https://fake.slack.com/archives/{channel}/p{ts.replace('.', '')}"

    def team_id(self) -> str:
        if self.auth_error:
            raise self.auth_error
        return self.team

    def dms(self) -> list[dict]:
        if self.auth_error:
            raise self.auth_error
        if self.dms_error:
            raise self.dms_error
        return list(self.conversations)

    def last_message(self, channel: str) -> dict | None:
        """Como el real: un solo mensaje, el más nuevo."""
        self.last_message_calls.append(channel)
        if channel in self.unavailable_channels:
            raise SlackUnavailable(f"conversations_history: {channel}: internal_error")
        mensajes = self.messages.get(channel, [])
        return max(mensajes, key=lambda m: float(m["ts"])) if mensajes else None

    def dm_estado(self, dm_id: str) -> dict:
        self.estado_calls.append(dm_id)
        if self.auth_error:
            raise self.auth_error
        if dm_id in self.unavailable_channels:
            raise SlackUnavailable(f"conversations_info: {dm_id}: internal_error")
        return dict(self.estados.get(dm_id, {"sin_leer": 0, "leido_hasta": None}))

    # --- ayudas para los tests ---
    def post(self, channel: str, user: str, text: str, ts: str, **extra) -> None:
        self.messages.setdefault(channel, []).append(
            {"user": user, "text": text, "ts": ts, **extra}
        )

    def set_members(self, channel: str, *user_ids: str) -> None:
        self.channel_members[channel] = set(user_ids)

    def add_profile(self, user_id, real_name, title="", email="", is_bot=False, deleted=False):
        self.profiles[user_id] = UserProfile(user_id, real_name, title, email, is_bot, deleted)

    def add_dm(self, dm_id: str, user: str, **extra) -> None:
        """Un DM uno a uno en la lista de conversaciones del dueño. `extra`
        sirve para las variantes que el rastro tiene que descartar
        (is_mpim=True, is_user_deleted=True...)."""
        self.conversations.append({"id": dm_id, "is_im": True, "user": user, **extra})
