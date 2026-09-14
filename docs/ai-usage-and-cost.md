# How AI usage and cost tracking works

> How the Founders Club agent measures what it spends on AI and paid services, and how it keeps that spend under control.

---

## The short version

Every time the agent uses a service that charges money, it **records what it cost and which person it was for**. On top of that, **three spending brakes** stop costs from running away.

That means we can always answer two questions:

- *How much did it cost to research this person?*
- *How much have we spent this month?*

---

## What costs money

The agent relies on two paid services:

| Service | What the agent uses it for | How it charges |
|---|---|---|
| **OpenAI (GPT-4.1)** | Reading what was found about a person and writing their dossier | By the amount of text sent and received, measured in tokens |
| **Brave Search** | Searching the web | Per search, about $0.005 each |

Everything else is free: reading web pages, looking up job postings, and SearXNG, a free search engine that only steps in when Brave is unavailable. None of those generate a cost.

---

## Tokens, and why caching matters

OpenAI doesn't charge per word or per request. It charges per **token**, a chunk of text roughly 3 or 4 characters long. The agent splits every call into three kinds of tokens, because each one has its own price:

| Token type | What it is | Price per million |
|---|---|---|
| **Input** | Text the agent sends to OpenAI | $2.00 |
| **Cached input** | Text OpenAI saw recently and can reuse | $0.50 |
| **Output** | Text OpenAI sends back | $8.00 |

**Caching makes a real difference.** A large part of every request is the same set of instructions. When OpenAI already has those in memory, it charges a quarter of the normal price for them. In our real-world test runs, caching made calls **72% cheaper**.

OpenAI reports cached tokens as part of the input. The agent separates them so each token is billed at exactly the right price.

---

## What gets recorded

Everything lives in the project's database (**Supabase**), which is the source of truth for spend.

| Record | What it holds |
|---|---|
| **AI calls** | One entry per OpenAI call: tokens of each type, cost in dollars, how long it took, which person it was for, and which step of the process made it. |
| **Other costs** | One entry per non-AI cost. Today that's Brave searches; later it will include SMS messages and any other paid service. |
| **Agent activity** | A log of what the agent did, including the total cost of each research run and whether it hit its limit. |
| **Settings** | The values of the three brakes, which can be changed without redeploying anything. |

A Brave search is only recorded when Brave actually answers. If it fails and the agent falls back to the free search engine, nothing is charged, so nothing is recorded.

---

## Why the numbers live in the database

The metric that matters most is **cost per closed deal**. To calculate it, spend has to be matched against people, their status, and the deals that came out of them. All of that lives in the same database, so that's where the spend lives too.

The agent also sends every AI call to **Langfuse**, a tool built for inspecting AI activity. It keeps the full picture of each call: what was asked, what came back, and how long it took. Langfuse is for **understanding** unusual spend. For example, it can show that the agent got stuck repeating the same step. It is not the source of the numbers, though: if Langfuse is down, spend is still recorded in full.

---

## What happens on every AI call

1. **The brakes are checked.** If the emergency stop is on, or the monthly budget is used up, OpenAI is never called.
2. **The call is made**, and its duration is measured.
3. **Tokens are split** into input, cached, and output, so each is priced correctly.
4. **The details go to Langfuse.** If Langfuse is unavailable, the agent carries on.
5. **The cost is saved** to the database.
6. **If saving fails**, the call has already been paid for. The agent doesn't stop or throw the result away. Instead it writes a critical warning to its logs with everything needed to rebuild the entry by hand. **A cost is never lost silently.**

---

## The three brakes

| Brake | What it does | Current value |
|---|---|---|
| **Emergency stop** | Halts all spending immediately. | Off |
| **Monthly budget** | Once this month's total (AI plus other costs) reaches the limit, the agent stops spending. | $150 |
| **Per-research budget** | If researching a single person goes over the limit, that research is cut short and the person is marked as incomplete. It isn't retried on the spot, but the next time they post in Slack, they get researched again. | $1 |

All three can be changed on the fly, **without redeploying**.

### When the emergency stop or the monthly budget kicks in

- An **alert goes to Handoff's own Slack**. Never to the Founders Club.
- The agent **waits 10 minutes** and checks again.
- Anyone waiting in the research queue **keeps their place**. The stop isn't that person's fault, so it doesn't count as a failed attempt.

---

## What it costs in practice

Measured with real money, researching 6 companies:

| | |
|---|---|
| **Cost per person researched** | $0.0217 (about 2 cents) |
| **Total for all 6** | $0.13 |
| **Sources invented by the AI** | 0 |
| **Savings from OpenAI caching** | 72% |

These figures come from before Brave was switched on. With Brave, each research run adds a handful of searches at about half a cent each.

---

## Where to see the numbers

- **Quick summary:** the agent can report total spend over the last few days.
- **Breakdown by person, by day, or by service:** straight from the database.
- **The full story of a single AI call:** in Langfuse.
- **Coming later:** a costs screen in the web dashboard.

---

## Good to know

- **The per-research budget only counts AI.** Brave searches are recorded and count toward the monthly budget, but not toward the $1 per-person limit.
- **A search run by hand, outside a research run, bypasses the brakes.** This doesn't happen during normal operation, only if someone uses the search tool directly.
- **Prices are kept up to date manually.** If OpenAI or Brave change their rates, the prices in the settings need updating to match. They're worth checking against the real invoice each month.
- **The month runs on UTC time.** In Colombia, the monthly budget resets at 7 p.m. on the last day of the previous month.
- **Langfuse isn't connected yet.** Spend is fully recorded, but per-call details aren't viewable until it is.
