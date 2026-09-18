"""Verificación de la firma de Slack.

Sin esto, cualquiera que conozca la URL puede descartar prospectos. La ventana
de 5 minutos es lo que impide repetir una petición interceptada, y la firma se
calcula sobre los bytes crudos del cuerpo: reconstruirla a partir del cuerpo ya
decodificado a texto arriesga un mismatch por la codificación que Slack usó de
verdad.
"""

from __future__ import annotations

import hashlib
import hmac
import time

MAX_AGE_SECONDS = 60 * 5


def verify(secret: str, timestamp: str, signature: str, body: bytes) -> bool:
    """True solo si la firma es de Slack, reciente, y hay secreto configurado.

    Un secreto vacío falla cerrado a propósito (nunca "todo vale"), igual que
    un timestamp que no sea un entero -- ambos son exactamente lo que un
    atacante mandaría para intentar colarse.
    """
    if not secret:
        return False
    try:
        age = abs(time.time() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > MAX_AGE_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
