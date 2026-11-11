# Mover el panel de Vercel a Railway

Todo en una sola plataforma. El panel pasa a ser un tercer servicio del mismo
proyecto de Railway, junto al worker y al receptor de botones.

El dominio cambia. No importa cuál sea: vale el que Railway genera.

## 1. Crear el servicio

En el proyecto `fantastic-acceptance`:

1. **New** → **GitHub Repo** → `davideliaspalacio/slack-yourhandOff`
2. Nómbralo `panel`
3. **Settings** → **Source** → **Root Directory**: `panel`

   Esto es lo que más se olvida. Sin ello Railway intenta construir el worker de
   Python y falla.
4. **Settings** → **Networking** → **Generate Domain**

Railway detecta Next solo: construye con `npm run build` y arranca con
`npm start`, que ya escucha en `0.0.0.0` y toma el puerto del entorno.

## 2. Las variables

En **Variables** del servicio nuevo, las mismas que hoy están en Vercel:

| Variable | De dónde sale |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | De Vercel, tal cual |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | De Vercel, tal cual |
| `MCP_PUBLIC_URL` | De Vercel, tal cual |
| `WORKER_URL` | La URL del servicio `slack-yourhandOff` |

`RAILWAY_PUBLIC_DOMAIN` la pone Railway sola, y es la que el panel usa para
saber que una redirección vuelve a su propio dominio (`panel/lib/redirect.ts`).
No hay que configurarla.

`NEXT_PUBLIC_SITE_HOST` solo hace falta si algún día se le pone un dominio
propio: entonces lleva ese dominio, sin `https://`.

## 3. Supabase: las URLs de redirección

**Esto es lo que deja a todo el mundo fuera si se olvida.** Supabase solo
permite volver tras el login a dominios de una lista.

En el proyecto de Supabase → **Authentication** → **URL Configuration** →
**Redirect URLs**, añadir:

```
https://<el-dominio-de-railway>/auth/callback
```

Dejar el de Vercel mientras convivan los dos.

## 4. El enlace del panel en el worker

Las tarjetas de Slack llevan un botón **Open in panel**. La dirección sale de
`PANEL_URL`, en las variables del servicio `slack-yourhandOff`. Cambiarla al
dominio nuevo, o los botones llevarán a un sitio apagado.

Esto se hace **al final**, cuando el panel nuevo ya funcione.

## 5. El orden, para no cortar el servicio

1. Crear el servicio y sus variables
2. Añadir la URL a Supabase, sin quitar la de Vercel
3. Entrar al panel nuevo: iniciar sesión, abrir una ficha, guardar un ajuste
4. Cambiar `PANEL_URL` en el worker
5. Comprobar que el botón de una tarjeta nueva de Slack abre el panel nuevo
6. Solo entonces, apagar el proyecto de Vercel

Si algo falla en los pasos 1 a 3, el panel de siempre sigue funcionando.

## Qué cambió en el código

`panel/lib/redirect.ts` decide a qué dominios puede redirigir tras el login.
Es lo que impide que una cabecera inventada se lleve el código de acceso de
Supabase a otro servidor. Ahora conoce `RAILWAY_PUBLIC_DOMAIN`.

Se usa esa variable en vez de aceptar `*.up.railway.app` entero: el comodín de
Vercel que ya existía deja pasar el dominio de cualquiera, y no hacía falta
repetir ese error. Cuando Vercel se apague, se puede quitar ese comodín.

`panel/package.json` arranca con `-H 0.0.0.0`. En este proyecto ya hubo un
healthcheck caído porque un proceso escuchaba solo en `::`.
