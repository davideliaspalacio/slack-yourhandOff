"""Lo que el servidor OAuth guarda y lo que nunca guarda (migración 0026).

Todo el SQL del OAuth vive aquí, para que `oauth.py` sea solo protocolo y HTTP. Las
reglas que esta capa hace cumplir, y que los tests comprueban una por una:

1. Nada en claro. De los códigos de autorización y de los access y refresh tokens solo
   se guarda el sha256 (como los tokens estáticos de 0023). El valor se devuelve una vez
   y no se puede recuperar. De la contraseña no se guarda ni el hash: la verifica
   Supabase Auth y no sale de la petición.
2. Un código es de un solo uso y dura 60 segundos. El canje es un UPDATE condicional
   (`where usado_at is null`), así que de dos canjes simultáneos solo uno gana. Si llega
   un segundo, el código se reusó: se revoca la sesión que salió de él (OAuth 2.1 manda
   tratar un código reusado como robo).
3. Un código está ligado a su cliente, a su `redirect_uri` y a su `code_challenge`: el
   canje los vuelve a comparar los tres.
4. Los refresh rotan: canjear uno lo marca usado y emite otro. Presentar uno ya usado
   revoca la sesión entera, por lo mismo que el punto 2.
5. El contador del tope diario vive en la SESIÓN, no en el access token: el token rota
   cada 8 horas y el contador se reiniciaría con él (ver la cabecera de 0026).

Los prefijos (`hmo_`, `hmr_`, `hmc_`) no son decoración: la puerta distingue por ellos un
access token OAuth de un token estático sin consultar dos tablas, y un valor filtrado en
un pegado accidental se reconoce de un vistazo.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from .. import db

# Vidas. El access token es corto porque es el único que viaja en cada petición al MCP;
# el refresh es largo para que nadie tenga que volver a entrar cada día.
VIDA_ACCESS_SEGUNDOS = 8 * 60 * 60
VIDA_REFRESH_SEGUNDOS = 30 * 24 * 60 * 60
# Un código solo tiene que sobrevivir a una redirección del navegador.
VIDA_CODIGO_SEGUNDOS = 60
# Lo que puede tardar una persona en escribir su correo y su contraseña.
VIDA_SOLICITUD_SEGUNDOS = 10 * 60

PREFIJO_ACCESS = "hmo_"
PREFIJO_REFRESH = "hmr_"
PREFIJO_CODIGO = "hmc_"

# Límite de intentos con espera creciente. A partir del tercer fallo se bloquea, y la
# espera se duplica con cada fallo siguiente hasta un cuarto de hora.
UMBRAL_INTENTOS = 3
ESPERA_BASE_SEGUNDOS = 5
ESPERA_MAXIMA_SEGUNDOS = 15 * 60
# Pasada una hora sin fallar, el contador vuelve a empezar: quien se equivoca hoy no
# arrastra lo de la semana pasada.
VENTANA_INTENTOS_SEGUNDOS = 60 * 60

# Un cliente que se registró y que nadie llegó a usar se borra al mes: el registro es
# abierto (RFC 7591) y la tabla no puede crecer para siempre.
VIDA_CLIENTE_SIN_USO_DIAS = 30

# Conexiones OAuth activas por persona. Reconectar es normal (Claude en el ordenador, en
# el móvil, un conector que se borra y se vuelve a añadir), así que no se rechaza la
# nueva: se cierra la más vieja. Así la lista del panel sigue siendo legible y una
# autorización olvidada no vive para siempre. El equivalente al "máximo 5 tokens" de 0023.
MAXIMO_SESIONES = 10


def hash_valor(valor: str) -> str:
    """sha256 en hexadecimal: lo único que se guarda de un código o de un token."""
    return hashlib.sha256(valor.encode()).hexdigest()


def _nuevo(prefijo: str) -> str:
    """32 bytes aleatorios de `secrets` en base64url, con su prefijo."""
    return prefijo + secrets.token_urlsafe(32)


def _una(sql: str, params: tuple) -> dict:
    """Como `db.fetch_one`, para sentencias con `returning`, que siempre devuelven fila."""
    fila = db.fetch_one(sql, params)
    if fila is None:
        raise RuntimeError("the database returned no row")
    return fila


# --- clientes (RFC 7591) ---------------------------------------------------------


def registrar_cliente(
    nombre: str | None,
    redirect_uris: list[str],
    grant_types: list[str],
    metadata: dict,
) -> dict:
    """Un cliente público nuevo. No hay client_secret: lo que protege el canje es PKCE."""
    return _una(
        """
        insert into oauth_clients (client_id, client_name, redirect_uris, grant_types, metadata)
        values (%s, %s, %s, %s, %s::jsonb)
        returning client_id, client_name, redirect_uris, grant_types, creado_at
        """,
        (
            "cli_" + secrets.token_urlsafe(18),
            nombre,
            redirect_uris,
            grant_types,
            json.dumps(metadata),
        ),
    )


def cliente(client_id: str) -> dict | None:
    return db.fetch_one(
        "select client_id, client_name, redirect_uris, grant_types from oauth_clients"
        " where client_id = %s",
        (client_id,),
    )


# --- solicitudes y códigos -------------------------------------------------------


def crear_solicitud(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    scope: str,
    recurso: str | None,
    state: str | None,
) -> str:
    """Guarda una petición de autorización YA validada y devuelve su id.

    La página de login solo lleva este id, así que el formulario no puede cambiar el
    cliente, la redirect_uri ni el reto PKCE: ya están comprobados y guardados."""
    fila = _una(
        """
        insert into oauth_solicitudes
            (client_id, redirect_uri, code_challenge, scope, recurso, state, expira_at)
        values (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
        returning id
        """,
        (
            client_id,
            redirect_uri,
            code_challenge,
            scope,
            recurso,
            state,
            VIDA_SOLICITUD_SEGUNDOS,
        ),
    )
    return str(fila["id"])


def solicitud_pendiente(solicitud_id: str) -> dict | None:
    """La solicitud si sigue abierta: sin código emitido y sin caducar."""
    return db.fetch_one(
        """
        select s.id, s.client_id, s.redirect_uri, s.scope, s.state, s.recurso,
               c.client_name
          from oauth_solicitudes s
          join oauth_clients c on c.client_id = s.client_id
         where s.id = %s and s.code_hash is null and s.expira_at > now()
        """,
        (solicitud_id,),
    )


def emitir_codigo(solicitud_id: str, email: str) -> str | None:
    """El código de un solo uso, ligado a este correo. None si la solicitud ya no vale.

    Un solo UPDATE condicional: dos envíos del formulario a la vez no emiten dos códigos
    para la misma solicitud."""
    codigo = _nuevo(PREFIJO_CODIGO)
    fila = db.fetch_one(
        """
        update oauth_solicitudes
           set email = %s,
               code_hash = %s,
               expira_at = now() + make_interval(secs => %s)
         where id = %s and code_hash is null and expira_at > now()
        returning id
        """,
        (email, hash_valor(codigo), VIDA_CODIGO_SEGUNDOS, solicitud_id),
    )
    return codigo if fila else None


def canjear_codigo(codigo: str) -> tuple[str, dict | None]:
    """Marca el código como usado y devuelve `(resultado, solicitud)`.

    `resultado` es "ok", "reusado" (ya se había canjeado: la sesión que salió de él se
    revoca, porque alguien tiene una copia) o "desconocido" (nunca existió o caducó).
    """
    hash_codigo = hash_valor(codigo)
    with db.transaction() as cur:
        cur.execute(
            """
            update oauth_solicitudes
               set usado_at = now()
             where code_hash = %s and usado_at is null and expira_at > now()
            returning id, client_id, redirect_uri, code_challenge, scope, recurso, email
            """,
            (hash_codigo,),
        )
        fila = cur.fetchone()
        if fila is not None:
            return "ok", fila

        cur.execute(
            "select id, sesion_id, usado_at from oauth_solicitudes where code_hash = %s",
            (hash_codigo,),
        )
        previa = cur.fetchone()
        if previa is None or previa["usado_at"] is None:
            # Nunca existió, o existió y caducó sin canjearse: nada que revocar.
            return "desconocido", None
        if previa["sesion_id"] is not None:
            _revocar_sesion(cur, str(previa["sesion_id"]))
        return "reusado", None


# --- sesiones y tokens -----------------------------------------------------------


def crear_sesion(solicitud_id: str, email: str, client_id: str, scope: str) -> str:
    """La sesión que sale de un código canjeado, apuntada en la solicitud.

    Queda apuntada para poder revocarla si alguien reusa ese código."""
    with db.transaction() as cur:
        cur.execute(
            "insert into oauth_sesiones (email, client_id, scope) values (%s, %s, %s) returning id",
            (email, client_id, scope),
        )
        sesion_id = str(cur.fetchone()["id"])
        cur.execute(
            "update oauth_solicitudes set sesion_id = %s where id = %s",
            (sesion_id, solicitud_id),
        )
        cur.execute(
            "update oauth_clients set ultimo_uso_at = now() where client_id = %s", (client_id,)
        )
        # Pasado el máximo, se cierran las más viejas de esa persona (con sus tokens).
        cur.execute(
            """
            select id from oauth_sesiones
             where email = %s and revocado_at is null
             order by creado_at desc
            offset %s
            """,
            (email, MAXIMO_SESIONES),
        )
        for sobrante in [str(fila["id"]) for fila in cur.fetchall()]:
            _revocar_sesion(cur, sobrante)
    return sesion_id


def emitir_tokens(sesion_id: str) -> tuple[str, str]:
    """Un access token y un refresh token nuevos para la sesión. Solo se guarda su hash."""
    access, refresh = _nuevo(PREFIJO_ACCESS), _nuevo(PREFIJO_REFRESH)
    with db.transaction() as cur:
        for valor, tipo, vida in (
            (access, "access", VIDA_ACCESS_SEGUNDOS),
            (refresh, "refresh", VIDA_REFRESH_SEGUNDOS),
        ):
            cur.execute(
                """
                insert into oauth_tokens (sesion_id, tipo, token_hash, sufijo, expira_at)
                values (%s, %s, %s, %s, now() + make_interval(secs => %s))
                """,
                (sesion_id, tipo, hash_valor(valor), valor[-4:], vida),
            )
    return access, refresh


def sesion_de_access_token(token: str) -> dict | None:
    """`(sesion_id, email, sufijo)` del dueño, o None.

    Una sola consulta por igualdad sobre el índice único del hash: no hay temporización
    útil que medir sobre el token. La caducidad y la revocación se miran aquí, en cada
    petición y sin caché, así que un token caducado o revocado deja de servir en la
    petición siguiente."""
    fila = db.fetch_one(
        """
        select s.id as sesion_id, s.email, t.sufijo
          from oauth_tokens t
          join oauth_sesiones s on s.id = t.sesion_id
         where t.token_hash = %s
           and t.tipo = 'access'
           and t.revocado_at is null
           and t.expira_at > now()
           and s.revocado_at is null
        """,
        (hash_valor(token),),
    )
    return fila


def canjear_refresh(token: str) -> tuple[str, dict | None]:
    """Rota el refresh: lo marca usado y devuelve `(resultado, sesión)`.

    "reusado" (ya se había canjeado) revoca la sesión entera: o lo robaron, o el cliente
    guardó una copia; en cualquier caso no se puede distinguir, y OAuth 2.1 manda cortar.
    """
    hash_token = hash_valor(token)
    with db.transaction() as cur:
        cur.execute(
            """
            update oauth_tokens t
               set usado_at = now()
             where t.token_hash = %s
               and t.tipo = 'refresh'
               and t.usado_at is null
               and t.revocado_at is null
               and t.expira_at > now()
               and exists (
                   select 1 from oauth_sesiones s
                    where s.id = t.sesion_id and s.revocado_at is null
               )
            returning t.sesion_id
            """,
            (hash_token,),
        )
        fila = cur.fetchone()
        if fila is not None:
            cur.execute(
                "select id, email, client_id, scope from oauth_sesiones where id = %s",
                (fila["sesion_id"],),
            )
            return "ok", cur.fetchone()

        cur.execute(
            "select sesion_id, usado_at from oauth_tokens"
            " where token_hash = %s and tipo = 'refresh'",
            (hash_token,),
        )
        previo = cur.fetchone()
        if previo is None:
            return "desconocido", None
        if previo["usado_at"] is not None:
            _revocar_sesion(cur, str(previo["sesion_id"]))
            return "reusado", None
        # Caducado, revocado, o su sesión está revocada: no vale, y no hay nada que cortar.
        return "invalido", None


def _revocar_sesion(cur: Any, sesion_id: str) -> None:
    """La sesión y todos sus tokens, en la transacción de quien llama."""
    cur.execute(
        "update oauth_sesiones set revocado_at = now() where id = %s and revocado_at is null",
        (sesion_id,),
    )
    cur.execute(
        "update oauth_tokens set revocado_at = now() where sesion_id = %s and revocado_at is null",
        (sesion_id,),
    )


def revocar_sesion(sesion_id: str) -> None:
    with db.transaction() as cur:
        _revocar_sesion(cur, sesion_id)


def revocar_por_token(token: str) -> bool:
    """Revoca la sesión a la que pertenece el token (RFC 7009). True si había alguna.

    Vale tanto para un access como para un refresh, y en los dos casos se corta la sesión
    entera: cuando alguien desconecta el conector quiere quedarse sin acceso, no cambiar
    un token por otro. Un token desconocido no es un error (RFC 7009 §2.2)."""
    fila = db.fetch_one(
        "select sesion_id from oauth_tokens where token_hash = %s", (hash_valor(token),)
    )
    if fila is None:
        return False
    revocar_sesion(str(fila["sesion_id"]))
    return True


# --- límite de intentos ----------------------------------------------------------


def espera_restante(clave: str) -> int:
    """Segundos que faltan para poder volver a intentar con esta clave (0 si se puede)."""
    fila = db.fetch_one(
        """
        select greatest(0, ceil(extract(epoch from (bloqueado_hasta - now()))))::int as espera
          from oauth_intentos
         where clave = %s and bloqueado_hasta > now()
        """,
        (clave,),
    )
    return int(fila["espera"]) if fila else 0


def anotar_intento(clave: str, umbral: int = UMBRAL_INTENTOS) -> int:
    """Suma un intento y devuelve la espera en segundos que impone (0 si aún no bloquea).

    Se usa para los fallos de login (umbral bajo: probar contraseñas no puede salir gratis)
    y para los registros de cliente (umbral holgado: registrar es legítimo)."""
    with db.transaction() as cur:
        cur.execute(
            """
            insert into oauth_intentos as i (clave, intentos)
            values (%s, 1)
            on conflict (clave) do update
               set intentos = case
                       when i.ultimo_intento_at < now() - make_interval(secs => %s) then 1
                       else i.intentos + 1 end,
                   primer_intento_at = case
                       when i.ultimo_intento_at < now() - make_interval(secs => %s) then now()
                       else i.primer_intento_at end,
                   ultimo_intento_at = now()
            returning intentos
            """,
            (clave, VENTANA_INTENTOS_SEGUNDOS, VENTANA_INTENTOS_SEGUNDOS),
        )
        intentos = cur.fetchone()["intentos"]
        if intentos < umbral:
            return 0
        # Se duplica con cada intento pasado el umbral, con tope: 5s, 10s, 20s... 15 min.
        espera = min(ESPERA_BASE_SEGUNDOS * 2 ** (intentos - umbral), ESPERA_MAXIMA_SEGUNDOS)
        cur.execute(
            "update oauth_intentos set bloqueado_hasta = now() + make_interval(secs => %s)"
            " where clave = %s",
            (espera, clave),
        )
        return espera


def olvidar_intentos(claves: list[str]) -> None:
    """Un login válido borra el historial de fallos de sus claves."""
    db.execute("delete from oauth_intentos where clave = any(%s)", (claves,))


# --- limpieza --------------------------------------------------------------------


def limpiar() -> None:
    """Barre lo que ya no sirve. Se llama de paso, al registrar y al autorizar: este
    servicio no tiene un cron, y estas tablas no deben crecer para siempre."""
    db.execute("delete from oauth_solicitudes where expira_at < now() - interval '1 day'")
    db.execute("delete from oauth_tokens where expira_at < now() - interval '7 days'")
    db.execute(
        """
        delete from oauth_intentos
         where ultimo_intento_at < now() - interval '1 day'
           and (bloqueado_hasta is null or bloqueado_hasta < now())
        """
    )
    # Un registro que nunca llegó a usarse: ni sesiones ni solicitudes vivas.
    db.execute(
        """
        delete from oauth_clients c
         where c.ultimo_uso_at is null
           and c.creado_at < now() - make_interval(days => %s)
           and not exists (select 1 from oauth_sesiones s where s.client_id = c.client_id)
        """,
        (VIDA_CLIENTE_SIN_USO_DIAS,),
    )
