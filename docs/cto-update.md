> **Instantánea del Plan 1, enviada el 2026-09-09.** Se conserva tal cual se
> mandó; no es el estado actual. Desde entonces han entrado los planes 2a
> (research worker) y 2b (ingesta desde Slack). Para saber dónde está el
> proyecto hoy, ver `README.md`. En particular, la línea de abajo sobre "no
> paid search API" ya no se sostiene: desde el Plan 2b la búsqueda usa la API
> de Brave por delante de SearXNG, y Brave cobra por consulta.

*Founders Club Sales Agent — build update*

*What this is*
An agent that watches the Founders Club Slack (1,400+ founders), researches
whoever participates, and hands Anthony an actionable dossier with a suggested
angle. The agent recommends; Anthony acts. It never writes in that workspace.

*Design decisions worth knowing*

• *The unit of work is the person, not the message.* Research is paid for once,
  and every later message from that person is scored against the dossier we
  already hold. This inverts the usual pipeline — research runs *before*
  scoring — which is where the intelligence comes from. "My week is just
  meetings and firefighting" matches no keyword list, but read against a
  dossier showing 8 open ops roles and a Series A three months ago, it's an
  obvious buying signal.

• *No keyword prefilter.* We deliberately removed it. A prefilter is the only
  component that can lose revenue silently: what it drops is never seen by the
  model, by Anthony, or by any log. Discarding happens afterwards, per person,
  with human judgment in the panel, and it's sticky.

• *Open source first.* SearXNG for search, trafilatura for page extraction,
  JobSpy for open roles. No paid search API. JobSpy reads LinkedIn's public
  guest surface, so no LinkedIn account is involved — worth stating explicitly
  given LinkedIn shut down Proxycurl ($10M ARR) in 2025 over exactly this.

• *Supabase for data, Langfuse for traces.* The cost ledger stays in Supabase
  because our key metric — cost per closed deal — is a JOIN against business
  tables, which Langfuse cannot do. Langfuse holds the execution trees that
  explain those numbers. Running Langfuse self-hosted would mean six containers
  and 8 GB of RAM for ~20k calls a month, so it's Cloud for now.

*Shipped tonight — Plan 1 of 4 (Foundation)*

Database schema, cost ledger, budget guardrails, and the research toolbox
exposed over MCP. 91 tests green, verified end-to-end against live services.

The toolbox is an MCP server rather than internal functions: one contract, three
consumers — the automated pipeline, us debugging from Claude Code, and the CEO
Command Center reusing it later without a rewrite.

Every paid call is metered. Kill switch, monthly cap and per-run cap all live in
a config table, changeable without a redeploy.

*What review found and we fixed*

Two adversarial review passes ran against the code. Nine real defects, all fixed
with tests that fail before the fix:

• *SSRF.* `leer_sitio` accepted any URL and followed redirects blindly. On
  Railway that reaches the instance metadata endpoint; anywhere it reaches our
  own Supabase on loopback. The URLs come from search results and links people
  paste into someone else's Slack — hostile input by definition.
• *Lost paid research.* Concurrent dossier saves raced on the version number and
  the second one died, discarding a ~$0.30 dossier. Both triggers can land on
  the same person at once.
• *The monthly cap enforced nothing.* It had zero callers. You could delete it
  entirely and the whole suite stayed green.
• *Job search returned other companies' vacancies* — each one adding false
  corroboration to the prospect's score.
• Plus: no response size cap, SearXNG exposed on 0.0.0.0 with a committed secret
  key, an escapable untrusted-content fence, and three tests that passed
  regardless of whether the code worked.

*Next*

Plan 2 (Slack ingestion + research worker), then Plan 3 (scoring and delivery),
then Plan 4 (panel). Nothing is blocked — development runs against an isolated
test Slack.

*What we need from Anthony, for Plan 2*

1. Slack user OAuth token
2. Historical data on the 6 closed deals, with the conversations — this is what
   calibrates detection, and shadow mode validates against it
3. Twilio number
4. Confirmation that reading the channel with his account is acceptable under
   the community's rules

*Before production*

Verify OpenAI's per-token prices against their pricing page. Our defaults are an
estimate and the entire cost accounting derives from them.
