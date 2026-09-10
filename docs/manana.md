# Mañana: conectar APIs

El código no está bloqueado por ninguna credencial. Esto es solo enchufar.

## 1. Arrancar lo local

```bash
supabase start
docker compose -f docker-compose.searxng.yml up -d
uv run pytest        # 91 en verde
```

`SEARXNG_SECRET` ya está generado en `.env`. Si SearXNG no arranca, es eso.

## 2. OpenAI

Poner `OPENAI_API_KEY` en `.env`. Nada más — el modelo ya es `gpt-4.1`.

**Antes de nada, contrastar los precios** con la página de OpenAI y ajustar
`PRICE_INPUT_PER_M`, `PRICE_CACHED_INPUT_PER_M` y `PRICE_OUTPUT_PER_M`. Toda la
contabilidad sale de esos tres números y ahora mismo son una estimación mía.

Comprobación de que el ledger registra de verdad:

```bash
uv run python -c "
from handoff_agent import llm, mcp_server
r = llm.complete('Di hola en cinco palabras', stage='prueba')
print(r.text, '->', r.cost_usd)
print(mcp_server.resumen_costes(days=1))
"
```

Si `llm_calls` no sube a 1, hay un problema y hay que pararse ahí.

## 3. Langfuse (opcional)

Crear cuenta en cloud.langfuse.com, poner las dos claves en `.env`. Sin ellas el
trazado es un no-op y todo lo demás funciona igual.

## 4. Probar los topes antes de gastar

```sql
-- bajar el tope y comprobar que corta
update config set value = '0.01'::jsonb where key = 'monthly_budget_usd';
-- la siguiente llamada debe lanzar MonthlyBudgetExceeded
update config set value = '150'::jsonb where key = 'monthly_budget_usd';

-- el kill switch
update config set value = 'true'::jsonb where key = 'kill_switch';
update config set value = 'false'::jsonb where key = 'kill_switch';
```

Mejor descubrir que un freno no frena con $0.01 en juego que con la factura
del mes.

## 5. Cuando lleguen las credenciales de Anthony

Eso ya es el Plan 2, no hace falta hoy:

- Token de usuario de Slack → `slack-watcher`
- Datos de los 6 deals cerrados → calibración del scoring y modo sombra
- Número de Twilio → entrega
- Confirmación sobre las reglas del Founders Club → antes de la Fase 2

## Deuda conocida, a propósito

Ninguna bloquea, todas están decididas y no olvidadas:

- **`incompleto` no lo escribe nadie todavía.** El estado existe en el esquema;
  lo marca el research worker del Plan 2 cuando revienta el tope por ejecución.
- **Rebinding de DNS.** Se comprueba la IP resuelta en cada salto, pero httpx no
  deja fijar esa IP en la conexión. El techo de 5 MB y el deadline acotan el
  daño. Cerrarlo del todo necesita un transporte propio; no vale la pena aún.
- **Políticas RLS del panel.** RLS está activado y denegando por defecto en
  todas las tablas. Las políticas llegan en el Plan 4 — y ojo: el acceso de
  escritura a `config` hay que limitarlo a los tres usuarios nombrados, nunca
  `using (true)`, o con la anon key pública cualquiera apaga el agente y sube
  los topes de gasto.
- **Crawl4AI.** Fuera a propósito: trafilatura cubre sitios corporativos y
  páginas de careers, que son estáticas casi siempre. Crawl4AI entra detrás de
  la misma firma de `leer_sitio` el día que aparezca un sitio que lo exija.
