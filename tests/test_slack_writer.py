import pytest

from handoff_agent.delivery import slack_writer


class FakeBot:
    def __init__(self):
        self.posted, self.updated = [], []

    def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "channel": kwargs["channel"], "ts": "1726000001.000200"}

    def chat_update(self, **kwargs):
        self.updated.append(kwargs)
        return {"ok": True}


@pytest.fixture
def bot(monkeypatch):
    fake = FakeBot()
    monkeypatch.setenv("HANDOFF_SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("HANDOFF_SLACK_CHANNEL_ID", "CHANDOFF")
    monkeypatch.setattr(slack_writer, "_client", lambda: fake)
    return fake


def test_posting_goes_to_the_handoff_channel(bot):
    channel, ts = slack_writer.post_card([{"type": "divider"}], "señal alta")
    assert (channel, ts) == ("CHANDOFF", "1726000001.000200")
    assert bot.posted[0]["channel"] == "CHANDOFF"
    assert bot.posted[0]["text"] == "señal alta"


def test_it_refuses_to_write_in_a_founders_club_channel(bot, monkeypatch):
    """La invariante del proyecto: el agente nunca escribe allí. Un canal mal
    configurado no puede convertirse en un mensaje publicado."""
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "CHANDOFF,C999")
    with pytest.raises(slack_writer.SlackWriteRefused):
        slack_writer.post_card([{"type": "divider"}], "señal")
    assert bot.posted == []


def test_without_a_bot_token_it_refuses_instead_of_guessing(bot, monkeypatch):
    monkeypatch.delenv("HANDOFF_SLACK_BOT_TOKEN")
    with pytest.raises(slack_writer.SlackWriteRefused):
        slack_writer.post_card([{"type": "divider"}], "señal")


def test_updating_replaces_the_card_in_place(bot):
    slack_writer.update_card("CHANDOFF", "1.0", [{"type": "divider"}], "actualizada")
    assert bot.updated[0]["ts"] == "1.0"


def test_updating_refuses_to_write_in_a_founders_club_channel(bot, monkeypatch):
    """La invariante del proyecto también protege actualización: un canal
    vigilado no puede ser actualizado, sea cual sea su fuente."""
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "CLOBBERED,C999")
    with pytest.raises(slack_writer.SlackWriteRefused):
        slack_writer.update_card("CLOBBERED", "1.0", [{"type": "divider"}], "test")
    assert bot.updated == []


def test_update_normalises_the_channel_by_stripping_and_case(bot, monkeypatch):
    """El canal puede llegar con espacios o en minúsculas de una fuente ajena,
    y la comparación debe seguir siendo segura. Se escapullen dos canales
    vigilados: uno con espacios, otro en minúsculas."""
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "CLOBBERED,c999")
    with pytest.raises(slack_writer.SlackWriteRefused):
        slack_writer.update_card("  CLOBBERED  ", "1.0", [{"type": "divider"}], "test")
    assert bot.updated == []
    with pytest.raises(slack_writer.SlackWriteRefused):
        slack_writer.update_card("clobbered", "1.0", [{"type": "divider"}], "test")
    assert bot.updated == []
