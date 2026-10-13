# Conectar tu Claude al agente (MCP remoto)

## Qué es

Una conexión para que **tu Claude** (el que usas tú, desde tu ordenador) pueda
**preguntarle al agente de Handoff** lo que ya sabe: quién es quién en el Founders Club,
qué dossiers hay, qué ha pedido la gente en Slack y en qué canal, cuánto se ha gastado y
cómo está el sistema. Funciona desde cualquier sitio, sin instalar nada del proyecto: se
conecta con el mismo correo y la misma contraseña con los que entras al panel.

Claude puede **mirar todo** (seis herramientas de lectura) y **cambiar lo mismo que
cambias tú en el panel**: el estado de una persona, las notas y datos que ayudan al
research, volver a investigar, los umbrales numéricos y los canales que el agente vigila
(seis herramientas de escritura, ver más abajo). No puede borrar nada, ni tocar los topes
de gasto, ni apagar el sistema, ni escribir en Slack.

## 1. Conectar tu Claude

No hay nada que copiar ni ningún archivo que editar, y funciona igual en el ordenador, en
el navegador y en el móvil.

1. Copia la URL del MCP: está en el panel, en **Settings** → tarjeta **Connect your
   Claude**, con un botón **Copy URL**. Hoy es
   `https://buttons-slack-production.up.railway.app/mcp`.
2. En Claude, abre **Settings** → **Connectors** → **Add custom connector**.
3. Pega la URL y pulsa **Connect**.
4. Claude abre una página de Handoff que pide **el mismo correo y la misma contraseña con
   los que entras al panel**. Entra.
5. Ya está: Claude vuelve solo con el acceso puesto. La conexión aparece en **Settings** →
   **Connect your Claude** con la fecha y el último uso.

Si tu correo no está en el panel, no obtienes acceso aunque la contraseña sea correcta.

El permiso dura 8 horas y Claude lo renueva solo mientras lo uses (hasta 30 días sin
usarlo). Para cortarlo, **Disconnect** en esa misma tarjeta: deja de funcionar en la
petición siguiente, y Claude no puede renovarlo.

Si te equivocas de contraseña varias veces, la página te hace esperar unos segundos, y
cada vez más: es a propósito, porque ese formulario está abierto a internet.

## 2. La otra forma: un token a mano

Solo hace falta para **Claude Code en la terminal**, que pide un token en vez de abrir
una página para entrar.

1. En **Settings** → **Connect your Claude**, pulsa **Connect by hand instead**.
2. Escribe un nombre que te ayude a reconocerlo (por ejemplo "Claude de Anthony") y pulsa
   **Create token**.
3. El panel te enseña el token **una sola vez**. Cópialo ahora: no se puede volver a ver.
   Si lo pierdes, revócalo (**Revoke**) y crea otro.
4. Cada persona puede tener hasta 5 tokens activos. Si uno se filtra o ya no lo usas,
   pulsa **Revoke**: deja de funcionar al instante.

El token es personal. No lo pegues en Slack, ni en un documento compartido.

```bash
claude mcp add --transport http handoff https://buttons-slack-production.up.railway.app/mcp \
  --header "Authorization: Bearer PEGA_AQUI_TU_TOKEN"
```

(El panel muestra ese comando con tu token ya dentro, justo después de crearlo.)

**Claude Desktop**, si tu versión todavía no sabe conectarse a un servidor remoto, usa el
puente `mcp-remote` (necesita Node.js). En su archivo de configuración, dentro de
`mcpServers`:

```json
"handoff": {
  "command": "npx",
  "args": [
    "mcp-remote",
    "https://buttons-slack-production.up.railway.app/mcp",
    "--header",
    "Authorization:${HANDOFF_AUTH}"
  ],
  "env": { "HANDOFF_AUTH": "Bearer PEGA_AQUI_TU_TOKEN" }
}
```

Reinicia Claude y comprueba que `handoff` aparece con doce herramientas (seis de lectura
y seis de escritura).

## 3. Qué puedes preguntar (lectura)

Habla con Claude con normalidad; él elige la herramienta. Algunos ejemplos reales:

- "¿Quién pidió gente de soporte esta semana?" (mira las señales de los últimos 7 días)
- "Enséñame las personas con encaje 3 que todavía no hemos contactado."
- "Dame la ficha de Ada Ruiz: qué sabemos de su empresa y qué pidió en Slack."
- "¿Cuánto hemos gastado este mes y está el lector de Slack funcionando?"
- "¿Qué canales vigila el agente y cuál se mueve más?" (los canales salen por su nombre,
  `#founders-general`, no solo por su código)
- "¿Quién ha pedido algo en #founders-general esta semana?" (`senales_recientes` y
  `personas_buscar` aceptan un `canal`, por nombre o por código)

Las herramientas que hay detrás: `personas_buscar`, `persona_ver`, `senales_recientes`,
`canales_listar`, `resumen_costes` y `estado_sistema`.

## 4. Cambiar cosas desde Claude

Seis herramientas cambian datos. Tienen **el mismo nivel de control que el panel**:
validan lo mismo que sus botones y formularios, y nada más.

| Herramienta | Qué hace | Equivale en el panel a |
|---|---|---|
| `persona_cambiar_estado` | Pone a una persona en `nuevo`, `investigado`, `contactado`, `cliente` o `descartado` (opcional: un motivo). `incompleto` lo decide el research, no se puede fijar a mano. | El selector de estado de la ficha |
| `persona_ayudar_research` | Guarda la empresa, la web, enlaces o notas que el equipo sabe y el agente no, y **encola un research nuevo** (salvo que la persona esté descartada). | **Help the research** |
| `persona_reinvestigar` | Encola un research nuevo. Una persona descartada no se reencola; si ya hay uno pendiente, no se duplica. | **Research again** |
| `ajustar_umbral` | Cambia un umbral numérico (ver la lista abajo). | Settings |
| `canal_agregar` | Añade un canal a los que el agente vigila (por su código, `C…` o `G…`; máximo 20, sin repetir). El canal donde el agente publica sus tarjetas **nunca** se puede vigilar. | Settings → **Watched channels** |
| `canal_quitar` | Quita un canal de los vigilados. Deja de leerse en el siguiente sondeo; los mensajes y las señales que ya trajo se quedan. | Settings → **Watched channels** |

Las que actúan sobre una persona la aceptan por su id o por su `slack_user_id`, y todas
**devuelven cómo quedó el cambio** (estado anterior y nuevo, lo que se guardó, si se
encoló el research, la lista de canales resultante), para que Claude confirme lo que pasó
en vez de suponerlo.

### Ejemplos

- "Descarta a Ada Ruiz, no encaja." → `persona_cambiar_estado` con `descartado` y el
  motivo "no encaja".
- "La web de la empresa de Bob es acme.com, reinvestígala." → `persona_ayudar_research`
  con `web = acme.com` (se normaliza: vale `https://www.acme.com/about` y lo deja en
  `acme.com`) y encola el research.
- "Añade esta nota a Ada: ya tiene un SDR y busca un segundo." → `persona_ayudar_research`
  con `notas`.
- "Marca como contactados a los de encaje 3 que me enseñaste." → una llamada a
  `persona_cambiar_estado` por persona.
- "Baja el tope de SMS a 2 al día." → `ajustar_umbral` con `sms_por_dia = 2`.
- "Vigila también C08MRLP46G6." → `canal_agregar`. Para que sirva, la cuenta del token de
  lectura tiene que ser miembro de ese canal; el nombre (`#…`) aparece tras el siguiente
  sondeo.
- "Deja de leer #founders-random." → `canal_quitar` (con el código, que es lo que la
  herramienta acepta; `canales_listar` lo dice).

Cómo tratan los datos:

- `persona_ayudar_research` **solo cambia lo que le pasas**: lo que omitas se queda
  como estaba. Los `enlaces` se **añaden** a los que ya hay (máximo 10, sin repetir); una
  lista vacía los borra todos. Las `notas` **reemplazan** a las anteriores, y un texto
  vacío borra empresa, web o notas. Si quieres añadir a una nota existente, pídele a
  Claude que mire primero la ficha (`persona_ver`).
- `ajustar_umbral` acepta solo estas claves, todas numéricas: `banda_alta_min`,
  `banda_media_min`, `banda_baja_min` (0 a 4; subir `banda_baja_min` a 4 silencia todo),
  `research_por_hora`, `sms_por_dia`, `sms_hora_inicio`, `sms_hora_fin`, `radar_auto_min`,
  `radar_horas_entre_escaneos`, `unipile_busquedas_por_dia`, `unipile_perfiles_por_dia`,
  `alertas_por_dia`, `alertas_dias_misma_persona`, `alertas_edad_maxima_horas` y
  `aviso_token_horas`. Cada una tiene su máximo (un valor absurdo se rechaza).
- Pedir un research **gasta dinero** (modelo y búsquedas), dentro de los topes de siempre
  (research por hora y presupuesto mensual).

> **Cada cambio queda firmado.** Cada escritura deja una fila en el registro de acciones
> (`agent_actions`, acción `mcp_escritura`) con quién la hizo (tu correo y los 4 últimos
> caracteres de tu token), qué herramienta, sobre quién y el antes y el después. Si
> alguien usa tu token, el cambio sale a tu nombre: trátalo como tu contraseña, y si se
> filtra, revócalo. En la ficha de la persona aparece como **Changed from Claude**.

## Qué NO puede hacer

- Borrar nada: personas, dossiers, mensajes, tokens.
- Tocar el interruptor de emergencia (`kill_switch`) ni los topes de dinero
  (`monthly_budget_usd`, `run_budget_usd`). Siguen editándose a mano en la base.
- Subirse sus propios topes: `mcp_llamadas_por_dia` y `mcp_escrituras_por_dia` solo se
  cambian desde Settings en el panel.
- Cambiar interruptores (`exigir_evidencia`, `senales_activas`, pausa de Unipile): solo
  números.
- Escribir en Slack, mandar mensajes, escanear una cuenta del radar o buscar un decisor.
- Escribir si tu correo ya no está en el panel: aunque el token siga vivo, las escrituras
  se rechazan con `not authorized` (las lecturas siguen mientras el token no se revoque).

## Límites y avisos

- **Tope diario de llamadas:** cada conexión puede hacer 500 consultas al día (se cuenta
  cada pregunta que Claude hace al agente, lea o escriba). Una "conexión" es un token a
  mano, o una sesión de las que se abren entrando con el correo; renovar el permiso NO
  devuelve el cupo. Pasado el tope, Claude recibirá un error 429 hasta el día siguiente.
  El tope se cambia en Settings (`mcp_llamadas_por_dia`; 0 apaga el MCP remoto para
  todos).
- **Tope diario de escrituras, aparte:** 100 cambios al día **por persona** (sumando todos
  sus tokens), en Settings como `mcp_escrituras_por_dia` (0 apaga las escrituras para
  todos). Al pasarse, las herramientas de escritura responden `daily write limit reached`
  y las de lectura siguen funcionando. El día se cuenta en la zona horaria del equipo.
- **Quién eres lo decide tu credencial,** no lo que Claude escriba: Claude no puede
  firmar un cambio con el nombre de otra persona. Entrando con el correo, quien lo dice es
  Supabase (el mismo sitio que el panel), y la contraseña no se guarda en ningún sitio de
  Handoff.
- **Los dossiers son texto de terceros.** Lo que viene de webs y de mensajes de Slack
  llega a Claude dentro de bloques marcados como "no confiables": son datos, no órdenes.
  Si Claude se comporta raro después de leer una ficha, díselo al equipo.
- **Constancia:** el sistema anota una vez al día, por conexión, que se usó (quién y con
  qué herramienta empezó), y cada escritura por separado (ver arriba). De los tokens solo
  se guarda una huella, nunca el valor; de la contraseña, nada.
- **Cuidado con lo que Claude lee:** los dossiers y mensajes pueden traer texto que
  parezca una orden ("descarta a todos"). Claude está instruido para no obedecerlo y para
  escribir solo cuando tú se lo pidas; aun así, revisa lo que te confirma antes de seguir.

## Para quien despliega

- Migración `0026_mcp_oauth.sql`: el servidor de autorización OAuth (tablas
  `oauth_clients`, `oauth_solicitudes`, `oauth_sesiones`, `oauth_tokens`,
  `oauth_intentos`, y las funciones `panel_sesiones_oauth` / `panel_revocar_sesion_oauth`).
  No toca nada de `mcp_tokens`: los tokens a mano siguen funcionando igual.
- **Variables nuevas en el servicio web** (Railway, "Buttons-Slack"), las tres: sin ellas
  el OAuth queda apagado (responde 503) y el MCP sigue aceptando solo tokens a mano.
  - `MCP_PUBLIC_URL`: la URL pública de ESE servicio, con `https` (vale con `/mcp` o sin
    él). Es la identidad del servidor OAuth y del recurso, y va en los metadatos que lee
    el cliente, así que no se deduce de la cabecera `Host`.
  - `SUPABASE_URL` y `SUPABASE_PUBLISHABLE_KEY`: contra ellos se verifica el correo y la
    contraseña (`POST /auth/v1/token?grant_type=password`). La clave es la *publishable*,
    la misma que lleva el navegador en el panel; nunca la secreta.
- Rutas públicas que añade (sin credencial, porque el protocolo lo exige):
  `/.well-known/oauth-protected-resource` (y `/.well-known/oauth-protected-resource/mcp`),
  `/.well-known/oauth-authorization-server`, `/oauth/register`, `/oauth/authorize`,
  `/oauth/token` y `/oauth/revoke`. El 401 de `/mcp` lleva la cabecera
  `WWW-Authenticate: Bearer resource_metadata="..."`, que es lo que hace que un cliente
  sin credencial arranque el flujo solo.
- Migración `0023_mcp_tokens.sql` (tabla `mcp_tokens`, funciones del panel y la clave
  `mcp_llamadas_por_dia`), `0024_mcp_escritura.sql` (las funciones `mcp_*` de escritura, la
  clave `mcp_escrituras_por_dia` y un índice) y `0025_canales.sql` (la tabla `canales` con
  los nombres y las funciones `mcp_canal_agregar` / `mcp_canal_quitar`). Se aplican como
  las anteriores.
- Los nombres de canal los rellena el worker en su ciclo de lectura con
  `conversations.info`, un método de solo lectura más en la lista blanca de
  `slack_client.py`: no hace falta ningún scope de escritura, pero el token sí necesita
  poder leer el canal (`channels:read` / `groups:read`).
- Las funciones `mcp_*` reciben el correo como argumento y comprueban que esté en
  `panel_users`; por eso la migración les quita el `execute` a `public`, `anon` y
  `authenticated` (si el panel pudiera llamarlas, cualquiera con cuenta elegiría el correo).
  Las ejecuta el servicio web, que conecta con `DATABASE_URL` como dueño de la base: no
  hace falta ningún `grant`. `service_role` conserva el acceso por defecto de Supabase (ya
  se salta RLS).
- `0024` reemplaza `panel_ayudar_research` (mismo contrato) para que el panel y el MCP
  compartan una única función interna, `_ayudar_research`.
- El MCP vive dentro del servicio web de Railway ("Buttons-Slack", `handoff web`), en
  `/mcp`. No hay variables nuevas en el servicio.
- El panel (Vercel) muestra la URL de `MCP_PUBLIC_URL` si existe esa variable; si no,
  usa `https://buttons-slack-production.up.railway.app/mcp`.
