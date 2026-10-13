"""Verificar el correo y la contraseña del panel, sin guardar ni registrar nada de ellos.

Quién decide que alguien puede entrar: Supabase Auth (GoTrue), el MISMO sitio contra el
que entra el panel (`panel/app/login/LoginForm.tsx` llama a `signInWithPassword`). Aquí
no hay una segunda base de contraseñas, ni se guarda ninguna: la contraseña llega en el
formulario, se manda por HTTPS a Supabase y se olvida al acabar la petición.

Se llama al endpoint por HTTP en vez de usar un SDK porque hace falta una sola cosa
(`POST {SUPABASE_URL}/auth/v1/token?grant_type=password`) y el servicio web no tiene
ninguna dependencia de Supabase. La clave que lo autoriza es la publishable, la misma
que lleva el navegador en el panel: no concede nada por sí misma.

Dos comprobaciones, en este orden y siempre las dos:

1. La contraseña, contra Supabase. Supabase deja registrarse a cualquiera, así que esto
   solo prueba que el correo es de quien lo escribe.
2. Que ese correo esté en `panel_users`, igual que `is_panel_user()` en el panel y que
   `_mcp_exigir_panel` en el MCP. Una contraseña correcta de alguien que no está en el
   equipo no obtiene nada.

El orden importa para no filtrar quién existe: se pregunta siempre a Supabase, aunque el
correo no esté en panel_users, y el mensaje de error es el mismo en los dos casos. Lo que
el token de Supabase trae (su `user.email`) es lo que se usa como identidad, no lo que
venía en el formulario.

Nada de esto entra en un log, en el ledger ni en un mensaje de error: ni la contraseña,
ni el access token que devuelve Supabase (que se descarta: aquí solo interesa el correo).
"""

from __future__ import annotations

import logging

import httpx

from .. import db
from ..config import Settings

logger = logging.getLogger(__name__)

# El login de una persona delante de un formulario: si Supabase tarda más que esto, es
# mejor decir "inténtalo de nuevo" que dejar la pestaña colgada.
TIMEOUT_SEGUNDOS = 10.0

OK, RECHAZADO, NO_DISPONIBLE = "ok", "rechazado", "no_disponible"


def verificar(settings: Settings, email: str, password: str) -> tuple[str, str | None]:
    """`(resultado, correo)`. El correo es el que confirma Supabase, en minúsculas.

    "rechazado" tapa los tres casos que no deben distinguirse desde fuera: la contraseña
    no es, el correo no existe en Supabase, o existe pero no está en `panel_users`.
    "no_disponible" es solo un fallo de Supabase (red, 5xx): eso sí se distingue, porque
    decirle "contraseña incorrecta" a quien la escribió bien manda a nadie a ningún sitio.
    """
    if not settings.supabase_url or not settings.supabase_publishable_key:
        logger.error("oauth: falta SUPABASE_URL o SUPABASE_PUBLISHABLE_KEY")
        return NO_DISPONIBLE, None

    try:
        respuesta = httpx.post(
            f"{settings.supabase_url}/auth/v1/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
            headers={
                "apikey": settings.supabase_publishable_key,
                "Authorization": f"Bearer {settings.supabase_publishable_key}",
            },
            timeout=TIMEOUT_SEGUNDOS,
        )
    except httpx.HTTPError as exc:
        # Nunca el cuerpo ni la URL con parámetros: solo el tipo de fallo.
        logger.error("oauth: Supabase Auth no responde: %s", type(exc).__name__)
        return NO_DISPONIBLE, None

    if respuesta.status_code in (400, 401, 403):
        # Credenciales que Supabase no acepta. El cuerpo lleva el motivo exacto
        # ("invalid_credentials", "email_not_confirmed"...): no se registra ni se enseña,
        # porque distinguirlos es decir si el correo existe.
        return RECHAZADO, None
    if respuesta.status_code != 200:
        logger.error("oauth: Supabase Auth devolvió %s", respuesta.status_code)
        return NO_DISPONIBLE, None

    try:
        datos = respuesta.json()
    except ValueError:
        logger.error("oauth: Supabase Auth devolvió algo que no es JSON")
        return NO_DISPONIBLE, None

    usuario = datos.get("user") if isinstance(datos, dict) else None
    confirmado = (usuario or {}).get("email") if isinstance(usuario, dict) else None
    if not isinstance(confirmado, str) or "@" not in confirmado:
        # Sin correo confirmado por Supabase no hay identidad que usar.
        logger.error("oauth: Supabase Auth no devolvió el correo del usuario")
        return NO_DISPONIBLE, None

    correo = confirmado.strip().lower()
    if not es_del_panel(correo):
        logger.warning("oauth: login correcto de un correo que no está en panel_users")
        return RECHAZADO, None
    return OK, correo


def es_del_panel(email: str) -> bool:
    """Lo mismo que `is_panel_user()` para un llamante identificado por correo.
    `panel_users.email` está siempre en minúsculas (check de 0006)."""
    return (
        db.fetch_one("select 1 as ok from panel_users where email = %s", (email.lower(),))
        is not None
    )
