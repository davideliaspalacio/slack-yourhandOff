"""A reader with the same surface as FoundersClubReader, for the ingest tests."""

from handoff_agent.slack_client import SlackUnavailable, UserProfile


class FakeReader:
    def __init__(self, owner: str = "UOWNER"):
        self.owner = owner
        self.messages: dict[str, list[dict]] = {}
        self.channel_members: dict[str, set[str]] = {}
        self.profiles: dict[str, UserProfile] = {}
        self.auth_error: Exception | None = None
        self.unavailable_channels: set[str] = set()
        self.history_calls: list[tuple[str, str]] = []

    # --- la misma superficie que FoundersClubReader ---
    def owner_id(self) -> str:
        if self.auth_error:
            raise self.auth_error
        return self.owner

    def history(self, channel: str, oldest: str) -> list[dict]:
        self.history_calls.append((channel, oldest))
        if channel in self.unavailable_channels:
            raise SlackUnavailable(f"{channel}: internal_error")
        return [m for m in self.messages.get(channel, []) if float(m["ts"]) > float(oldest)]

    def members(self, channel: str) -> set[str]:
        return set(self.channel_members.get(channel, set()))

    def user_profile(self, user_id: str) -> UserProfile:
        if self.auth_error:
            raise self.auth_error
        return self.profiles[user_id]

    # --- ayudas para los tests ---
    def post(self, channel: str, user: str, text: str, ts: str, **extra) -> None:
        self.messages.setdefault(channel, []).append(
            {"user": user, "text": text, "ts": ts, **extra}
        )

    def set_members(self, channel: str, *user_ids: str) -> None:
        self.channel_members[channel] = set(user_ids)

    def add_profile(self, user_id, real_name, title="", email="", is_bot=False, deleted=False):
        self.profiles[user_id] = UserProfile(user_id, real_name, title, email, is_bot, deleted)
