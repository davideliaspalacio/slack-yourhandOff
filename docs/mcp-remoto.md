# Conectar tu Claude al agente (MCP remoto, solo lectura)

## Qué es

Una conexión para que **tu Claude** (el que usas tú, desde tu ordenador) pueda
**preguntarle al agente de Handoff** lo que ya sabe: quién es quién en el Founders Club,
qué dossiers hay, qué ha pedido la gente en Slack, cuánto se ha gastado y cómo está el
sistema. Funciona desde cualquier sitio, sin instalar nada del proyecto.

Es **solo lectura**: Claude puede mirar, no puede cambiar nada (ni estados, ni colas,
ni dossiers) ni gastar dinero en research.

## 1. Crear tu token en el panel

1. Entra al panel y abre **Settings**.
2. En la tarjeta **Connect your Claude**, escribe un nombre que te ayude a reconocerlo
   (por ejemplo "Claude de Anthony") y pulsa **Create token**.
3. El panel te enseña el token **una sola vez**. Cópialo ahora: no se puede volver a ver.
   Si lo pierdes, revócalo (**Revoke**) y crea otro.
4. Cada persona puede tener hasta 5 tokens activos (uno por ordenador, por ejemplo).
   Si un token se filtra o ya no lo usas, pulsa **Revoke**: deja de funcionar al instante.

El token es personal. No lo pegues en Slack, ni en un documento compartido.

## 2. Añadirlo a Claude

Los tres datos que necesitas:

| Dato | Valor |
|---|---|
| Nombre | `handoff` (el que quieras) |
| URL | `https://buttons-slack-production.up.railway.app/mcp` |
| Token | el que acabas de copiar, como cabecera `Authorization: Bearer <token>` |

(El panel muestra la URL exacta debajo del botón; si cambia el dominio del servidor,
es la que manda.)

**Claude Code** (terminal), una sola vez:

```bash
claude mcp add --transport http handoff https://buttons-slack-production.up.railway.app/mcp \
  --header "Authorization: Bearer PEGA_AQUI_TU_TOKEN"
```

**Claude Desktop** no habla con servidores remotos con cabecera directamente; se usa
el puente `mcp-remote` (necesita Node.js). En el archivo de configuración de Claude
Desktop, dentro de `mcpServers`:

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

Reinicia Claude y comprueba que `handoff` aparece con cinco herramientas.

## 3. Qué puedes preguntar

Habla con Claude con normalidad; él elige la herramienta. Algunos ejemplos reales:

- "¿Quién pidió gente de soporte esta semana?" (mira las señales de los últimos 7 días)
- "Enséñame las personas con encaje 3 que todavía no hemos contactado."
- "Dame la ficha de Ada Ruiz: qué sabemos de su empresa y qué pidió en Slack."
- "¿Cuánto hemos gastado este mes y está el lector de Slack funcionando?"

Las herramientas que hay detrás: `personas_buscar`, `persona_ver`, `senales_recientes`,
`resumen_costes` y `estado_sistema`.

## Qué NO puede hacer (todavía)

- Cambiar el estado de una persona, descartarla o marcarla como contactada.
- Pedir un research nuevo, escanear una cuenta o buscar un decisor.
- Escribir en Slack ni mandar mensajes.

Todo eso se hace en el panel. Poder hacerlo desde Claude es la siguiente fase.

## Límites y avisos

- **Tope diario:** cada token puede hacer 500 consultas al día (se cuenta cada pregunta
  que Claude hace al agente). Pasado el tope, Claude recibirá un error 429 hasta el día
  siguiente. El tope se cambia en Settings (`mcp_llamadas_por_dia`; 0 apaga el MCP remoto
  para todos).
- **Los dossiers son texto de terceros.** Lo que viene de webs y de mensajes de Slack
  llega a Claude dentro de bloques marcados como "no confiables": son datos, no órdenes.
  Si Claude se comporta raro después de leer una ficha, díselo al equipo.
- **Constancia:** el sistema anota una vez al día, por token, que se usó (quién y con qué
  herramienta empezó). El token nunca se guarda ni se registra.

## Para quien despliega

- Migración `0023_mcp_tokens.sql` (tabla `mcp_tokens`, funciones del panel y la clave
  `mcp_llamadas_por_dia`). Se aplica como las anteriores.
- El MCP vive dentro del servicio web de Railway ("Buttons-Slack", `handoff web`), en
  `/mcp`. No hay variables nuevas en el servicio.
- El panel (Vercel) muestra la URL de `MCP_PUBLIC_URL` si existe esa variable; si no,
  usa `https://buttons-slack-production.up.railway.app/mcp`.
