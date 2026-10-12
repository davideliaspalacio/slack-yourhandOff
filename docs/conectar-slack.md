# Conectar el agente a un Slack y probarlo

Todo esto se hace desde el panel (**Settings**), sin tocar Railway ni
redesplegar nada: el worker relee la credencial y los canales en cada ciclo,
cada ~2 minutos.

Hacen falta tres cosas:

| Qué | Empieza por | De dónde sale |
|---|---|---|
| Token de sesión | `xoxc-` | Local Storage del navegador |
| Cookie `d` | `xoxd-` | Cookies del navegador |
| ID de cada canal | `C` (o `G`) | La dirección del canal en Slack |

Las dos primeras son la sesión de quien esté dentro de ese Slack, así que
valen lo mismo que su contraseña: se pegan en el panel, nunca por chat ni por
correo. Caducan cada pocos días y al cerrar sesión en Slack.

Si en ese workspace **sí** se puede instalar una app, es mejor: el token
`xoxp-` de la app no caduca. En ese caso el campo de la cookie se deja vacío.

## 1. El token (`xoxc-`)

1. Abrir **app.slack.com** en el navegador (no la aplicación de escritorio) y
   entrar al workspace.
2. **F12** → pestaña **Console**. Si Chrome lo pide, escribir `allow pasting`
   y Enter.
3. Pegar esto y Enter, cambiando el nombre del workspace:

   ```js
   copy(Object.values(JSON.parse(localStorage.localConfig_v2).teams).find(t => t.name === "Handoff").token)
   ```

   No imprime nada: deja el token copiado en el portapapeles.

Para ver qué workspaces hay y cómo se llaman exactamente:

```js
Object.values(JSON.parse(localStorage.localConfig_v2).teams).map(t => t.name)
```

**Importante:** lo que la consola *muestra* sale recortado con `…`. Eso es la
pantalla, no el token. Por eso se usa `copy(...)`, que copia el valor entero.
Si se pega un valor recortado, el panel lo rechaza con "the value looks
truncated".

Si `localConfig_v2` no trae tokens: **F12** → **Network** → filtro
`api.slack.com/api` → clic en cualquier canal → abrir una petición → pestaña
**Payload** → campo `token` → clic derecho → **Copy value**.

## 2. La cookie (`xoxd-`)

Esta no se puede sacar con código: Slack la protege (HttpOnly).

1. **F12** → pestaña **Application**.
2. **Storage** → **Cookies** → `https://app.slack.com`.
3. Clic en la fila **`d`** (esa letra sola, no `d-s`).
4. Copiar desde el panel de abajo, **Cookie Value**, donde el valor sale
   entero: empieza por `xoxd-` y termina en `%3D%3D`. También vale clic
   derecho en la fila → **Copy value**. Nunca copiar de la columna **Value**
   de la tabla, que recorta.

El token y la cookie tienen que salir de la **misma sesión**. Mezclar la
cookie de un navegador con el token de otro no funciona.

## 3. El ID de cada canal

En la dirección del canal, lo que empieza por `C` es el canal y lo que empieza
por `T` es el workspace:

```
https://app.slack.com/client/T06FYABE5L6/C0C1PMDRHK5
                            ^workspace    ^canal
```

También: clic en el nombre del canal → abajo del todo de la ventana que se
abre está el **Channel ID**.

Dos reglas:

- La cuenta dueña del token tiene que **estar dentro** de cada canal.
- **Nunca** poner el canal donde el bot publica las tarjetas
  (`HANDOFF_SLACK_CHANNEL_ID`). El sistema se niega a publicar en un canal que
  vigila, para no leerse a sí mismo.

## 4. Pegarlo en el panel

**Settings** → **Founders Club reader token**:

- Primer campo: el `xoxc-` (o el `xoxp-` de una app).
- Segundo campo: el `xoxd-` (vacío si es un token de app).
- **Save token**.

**Settings** → **Watched channels**: los IDs separados por comas, y **Save
channels**.

## 5. Comprobar que quedó bien

En la misma tarjeta, bajo el estado, aparece la línea del lector:

- `Last successful read: Oct 2, 2026, 7:59 PM · workspace Handoff · 1 channel`
  — está leyendo. El sello se actualiza cada ciclo, y dice a qué workspace
  entró y cuántos canales leyó.
- `(stale — the worker may be down)` — la última lectura buena es de hace más
  de 15 minutos.
- `Reading failed … — …` — la credencial no sirve (lo normal: caducó). Se
  pega un par nuevo.
- `The worker has not polled yet.` — todavía no ha corrido ningún ciclo.

Ojo: "Configured" significa solo que hay algo guardado. La prueba de verdad es
la línea de **Last successful read**.

## 6. Probar el flujo completo

### Sin tocar Slack: Test mode

**Test mode** en el panel crea una persona de prueba con su mensaje y la
encola; el worker la investiga y publica su tarjeta como si fuera real. La
casilla **Post the card to Slack** se puede desmarcar para investigar sin
publicar nada.

Sirve para probar research, ficha, costes y tarjeta sin depender de que
alguien escriba.

### Con un mensaje real

1. Que **otra persona** escriba algo con sustancia en un canal vigilado. Los
   mensajes de la cuenta dueña del token se ignoran a propósito.
2. Esperar hasta ~2 minutos.
3. En **People** aparece la persona; en su ficha, el dossier con sus fuentes.
4. Si el encaje es 2 o 3, sale la tarjeta en el canal de Handoff.

### Desde la terminal

`scripts/simular.py` hace lo mismo sin pasar por Slack, y además permite
probar los botones firmados, el SMS y el email diario. Ver
[`docs/pruebas-manuales.md`](pruebas-manuales.md).

## 7. Qué puede fallar

| Lo que se ve | Qué pasa | Qué hacer |
|---|---|---|
| "the value looks truncated" al guardar | Se copió el valor recortado con `…` | Usar `copy(...)` para el token y **Cookie Value** para la cookie |
| "must start with xoxp- or xoxc-" | Se puso la cookie en el primer campo | El `xoxc-` arriba, el `xoxd-` abajo |
| "a session token (xoxc-) needs the d cookie" | Falta la cookie | Rellenar el segundo campo |
| `Reading failed … SlackAuthFailed` | La sesión caducó o se cerró sesión en Slack | Sacar un par nuevo y pegarlo |
| Lee bien pero un canal no trae nada | Ese canal es de otro workspace, o la cuenta no está dentro | Añadir la cuenta al canal, o usar la credencial de ese workspace |
| Llegan mensajes pero no sale tarjeta | El encaje salió 0 o 1, o la persona está descartada o contactada | Mirar la ficha en el panel: ahí está la puntuación y el motivo |
| "falta HANDOFF_SLACK_BOT_TOKEN" en los logs | Al worker le falta la variable del bot en Railway | Añadir `HANDOFF_SLACK_BOT_TOKEN` y `HANDOFF_SLACK_CHANNEL_ID` al servicio del worker |

## 8. Límites que conviene saber

- **Un workspace a la vez.** La credencial pertenece a un solo Slack; los
  canales de otro workspace fallarán aunque estén en la lista.
- **La sesión caduca** cada pocos días. Un token de app (`xoxp-`) no.
- **El agente nunca escribe** en el Slack que lee. Publicar es cosa del bot de
  Handoff, con otra credencial y en su propio canal.
- Se vigilan **hasta 20 canales**.
