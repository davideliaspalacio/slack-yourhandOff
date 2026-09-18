import hashlib
import hmac
import time

from handoff_agent.web import slack_signature

SECRET = "s3cr3t"


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_a_correct_signature_passes():
    ts = str(int(time.time()))
    body = b"payload=%7B%7D"
    assert slack_signature.verify(SECRET, ts, sign(SECRET, ts, body), body) is True


def test_a_forged_signature_fails():
    ts = str(int(time.time()))
    body = b"payload=%7B%7D"
    assert slack_signature.verify(SECRET, ts, "v0=deadbeef", body) is False


def test_an_old_request_is_refused():
    """Sin la ventana temporal, cualquiera podría repetir una petición robada."""
    old = str(int(time.time()) - 60 * 10)
    body = b"x"
    assert slack_signature.verify(SECRET, old, sign(SECRET, old, body), body) is False


def test_a_future_request_is_refused():
    """El reloj puede estar desincronizado en cualquier dirección."""
    future = str(int(time.time()) + 60 * 10)
    body = b"x"
    assert slack_signature.verify(SECRET, future, sign(SECRET, future, body), body) is False


def test_a_non_numeric_timestamp_is_refused():
    body = b"x"
    signature = sign(SECRET, "no-soy-un-numero", body)
    assert slack_signature.verify(SECRET, "no-soy-un-numero", signature, body) is False


def test_an_empty_secret_fails_closed():
    """Sin SLACK_SIGNING_SECRET configurado, nunca hay que aceptar nada."""
    ts = str(int(time.time()))
    body = b"x"
    assert slack_signature.verify("", ts, sign(SECRET, ts, body), body) is False
