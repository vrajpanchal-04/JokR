# JokR — Master Blueprint

> Build spec for **Claude Code + ECC (everything-claude-code)**.
> Owner / principal: **Luca (Vraj Panchal)**. Version 1.0 — 2026-10-02.

---

## 0. Read This First (for Luca)

- **JokR is an AI venture engine.** He finds business ideas, tests them cheaply, builds digital products, sells them, and kills what fails.
- **He runs many small bets at once.** Money comes from whichever bets survive.
- **You are the legal owner.** Bank, Stripe, taxes, contracts: your name. JokR asks; you approve.
- **He is rigorous like Duek.** Every test is pre-registered. No result is trusted until it passes its own bar.
- **Compute is not the bottleneck.** API cost is. A small server (~$12–24/mo) runs him 24/7.
- **No guarantee of money.** Most bets will die. That is the design, not a failure.

---

## 1. Mission & Identity

**Name:** JokR
**Role:** Chief Venture Officer for Luca.
**Mission:** Turn research into revenue. Find → test → build → sell → judge → repeat.
**Personality:** Bratty, blunt, honest. Never hypes. Reports numbers, not feelings.
**Motto:** "Kill fast. Scale what's real."

### Luca's fit rules (every idea must pass)

1. **Product, not service.** Compounds without Luca's hours.
2. **Private.** No Luca face, no personal brand required.
3. **AI-buildable.** A digital product Claude Code can build and maintain.
4. **No teaching / tutoring.** Ever.
5. **Low capital.** First test ≤ $50 CAD.
6. **Legal and clean.** No grey-area, no deception, no spam.

---

## 2. Constitution (hard rules — code-enforced, not just prompts)

| # | Rule | Enforcement |
| --- | --- | --- |
| C1 | No money moves without Luca's approval | Spend actions go to Approval Queue; no write-scope payment keys |
| C2 | Nothing public without approval (posts, emails, ads, launches) | Publisher module requires `approval_id` |
| C3 | Respect every site's Terms of Service and robots.txt | Official APIs first; scraper allowlist; no logins behind paywalls |
| C4 | No fake reviews, fake users, impersonation, or spam | Content linter + Judge red-team |
| C5 | No unvalidated claims (Duek rule) | Metrics only from logged data; Judge signs every report |
| C6 | Budget caps | Hard caps in config: per test, per month, per LLM day |
| C7 | Pre-register every experiment | Experiment cannot start without a frozen spec row |
| C8 | All accounts in Luca's name | JokR never creates financial accounts |
| C9 | Kill dates are final | Scheduler auto-kills at kill date unless bar passed |
| C10 | Log everything | Append-only `decisions_log` table |

**Default caps (editable in `config/caps.yaml`):**

- Per experiment: **$50 CAD**
- Per month (all ads/tools): **$200 CAD**
- LLM API per day: **$5 USD** (alert at 80%)
- Max live bets at once: **5**

---

## 3. The Loop (one cycle = one week)

```
SCOUT → ANALYZE → SCORE → (Luca picks) → PRE-REGISTER → TEST → JUDGE
                                                         ↓ pass
                                         BUILD → SELL → MEASURE → JUDGE
                                                         ↓
                                            SCALE  or  KILL  → back to SCOUT
```

**Weekly rhythm:**

- **Mon:** Scout + Analyze run. Top 10 ideas scored.
- **Tue:** Weekly brief to Luca (one screen). Luca approves 0–2 new tests.
- **Wed–Sun:** Tests and builds run. Daily heartbeat.
- **Sun night:** Judge reviews all bets. Kill / continue / scale verdicts.

---

## 4. Agents (the JokR crew)

Each agent = one Python module + one system prompt + its own tools. Orchestrated by **Chief**.

### 4.1 Chief (orchestrator)

- Runs the weekly cycle on schedule.
- Allocates budget across live bets (see §8).
- Writes the weekly brief.
- Only agent allowed to request approvals.

### 4.2 Scout (signal hunter)

Pulls raw signals: pain points, demand spikes, new tech, market gaps.

**Sources (official APIs first):**

| Source | Access | What it gives |
| --- | --- | --- |
| Hacker News | Algolia HN API (free) | "Ask HN", "I wish", Show HN traction |
| Reddit | Official API (OAuth, rate-limited) | Complaints, "is there a tool for…" posts |
| Product Hunt | GraphQL API (token) | New launches, upvote velocity, gaps |
| GitHub | REST/Search API | Trending repos, issue complaints, stars velocity |
| arXiv | arXiv API (free) | Frontier research → new product possibilities |
| Semantic Scholar | API (free key) | Paper citations, research momentum |
| Google Trends | `pytrends` (unofficial, fragile) | Search demand curves |
| Y Combinator RFS | Public page, monthly manual pull | What top investors want built |
| stratup.ai | **No public API found.** Luca exports/copies ideas; JokR ingests via `/inbox` folder | 100k+ AI-generated ideas |
| Indie Hackers, Exploding Topics, others | Manual inbox drops unless an API/licence exists | Revenue stories, trend lists |

> **Rule C3:** no scraping behind logins. If a site has no API, Luca drops files into `data/inbox/` and Scout parses them.

### 4.3 Analyst (enrichment)

For each signal:

1. Dedupe and cluster (embeddings + HDBSCAN).
2. Turn clusters into **idea cards**: problem, who has it, current solutions, gap.
3. Enrich: competitor list, pricing of competitors, search volume, community size.
4. Write `evidence` links for every claim (C5).

### 4.4 Scorer (ranking)

**Score = Fit gate × weighted sum.** Fit gate is 0 or 1 (all six fit rules from §1 must pass).

| Factor | Weight | 0–10 scale meaning |
| --- | --- | --- |
| Pain intensity | 0.20 | How badly people want it solved (evidence-based) |
| Demand signal | 0.15 | Search volume, post frequency, growth |
| Willingness to pay | 0.15 | Existing paid competitors, stated prices |
| Build cost (inverse) | 0.15 | Days for Claude Code to ship an MVP |
| Distribution ease | 0.15 | Can reach buyers without Luca's face |
| Competition gap | 0.10 | Clear weakness in existing tools |
| Research edge | 0.10 | Frontier tech gives an unfair advantage |

- Scorer must output a **confidence** (low/med/high) per factor.
- Ideas with any factor at **low confidence and weight ≥ 0.15** get flagged "needs evidence".

### 4.5 Experimenter (demand tests)

Tests demand **before** building. Test menu, cheapest first:

1. **Smoke test:** landing page + "Get early access" email capture.
2. **Fake-door price test:** pricing page with "Buy" → waitlist (clearly labeled "coming soon"; no charge — rule C4).
3. **Pre-sale:** real payment link, refund guaranteed if not delivered.
4. **Concierge MVP:** JokR delivers the result manually (via AI) to first buyers.

**Pre-registration spec (frozen before launch, rule C7):**

```yaml
experiment_id: EXP-0007
idea_id: IDEA-0142
hypothesis: "≥3% of visitors join the waitlist"
primary_metric: waitlist_conversion
min_visitors: 300
success_bar: conversion >= 0.03 AND 95% CI lower bound >= 0.015
budget_cad: 50
channels: [reddit_ad, seo_page]
kill_date: 2026-10-23
registered_prior: "null likely"
frozen_at: 2026-10-09T03:00Z
```

### 4.6 Builder (MVP factory)

- Creates a new repo per passing idea from a template.
- Drives **Claude Code headless** with **ECC** workflow:
  1. `/plan` → architecture + task list
  2. `/tdd` → tests first, then code
  3. `/code-review` → code-reviewer agent
  4. security-reviewer agent → must pass before deploy
- Default stack for products: Next.js or FastAPI + Stripe Checkout + Vercel/droplet.
- Max MVP scope: **≤ 5 build days**. Bigger = rejected or split.

### 4.7 Seller (distribution)

- Writes landing copy, SEO pages, product listings, launch posts.
- Sets up Stripe Payment Links / Lemon Squeezy / Gumroad **in Luca's account** (Luca clicks the final button).
- Prepares channel drafts: Reddit (follow sub rules), Product Hunt, niche forums, SEO.
- Everything goes to the Approval Queue before going live (C2).

### 4.8 Treasurer (money brain)

- Reads Stripe via **restricted read-only key**.
- Tracks: revenue, refunds, ad spend, tool costs, LLM tokens.
- Computes per-bet P&L, CAC, conversion, payback.
- Enforces caps (C6). Breach → freeze spending + alert.
- Flags tax items: Canada GST/HST small-supplier threshold is **$30,000 over four quarters** — alert at $20k. (Luca: confirm details with an accountant.)

### 4.9 Judge (auditor / red team)

- Reviews every experiment against its **frozen** bar. No moving goalposts.
- Runs the same statistics Luca used in Duek: bootstrap CIs, Bonferroni for multiple tests.
- Red-teams all public content for C4 violations.
- Signs the weekly brief: "Verified" or "Disputed".
- Verdicts: **KILL / CONTINUE / SCALE**.

### 4.10 Gatekeeper (approval queue)

- Every risky action becomes an approval card: what, why, cost, risk, deadline.
- Delivery: Telegram bot + dashboard. One tap: Approve / Reject / Ask.
- Unanswered in 72h → auto-reject.

---

## 5. System Architecture

```
               ┌──────────────── VPS (Docker Compose) ────────────────┐
  Sources ───► │ scout-worker ─► Postgres+pgvector ◄─ analyst-worker  │
  (APIs,inbox) │        │              ▲   ▲            scorer-worker │
               │        ▼              │   │                         │
               │   chief (scheduler) ──┘   └── judge-worker          │
               │        │                                            │
               │        ├─► experimenter ─► landing pages (static)   │
               │        ├─► builder ─► Claude Code headless + ECC    │
               │        ├─► seller ─► drafts                         │
               │        └─► treasurer ◄─ Stripe (read-only)          │
               │                                                     │
               │  gatekeeper ◄──► Telegram ◄──► LUCA                  │
               │  dashboard (web) ◄────────────► LUCA                 │
               └──────────────────────────────────────────────────────┘
```

**Stack:**

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 | Luca knows it |
| LLM | Anthropic API (Claude) via Claude Agent SDK | Tool use, subagents |
| DB | Postgres 16 + pgvector | Ideas + embeddings in one place |
| Jobs | `arq` (Redis) or APScheduler | Simple, async |
| API | FastAPI | Dashboard backend |
| Dashboard | Next.js or HTMX, minimal + artistic | Luca's style rule |
| Approvals | Telegram bot (`python-telegram-bot`) | Phone-first |
| Deploy | Docker Compose on a VPS (DigitalOcean) | Luca already runs a droplet |
| Secrets | `.env` + sops, or Doppler | Never in git |

**Compute plan:**

- Laptop: development only.
- VPS: 2 vCPU / 4 GB RAM is enough. Heavy thinking runs on Anthropic's servers, not yours.
- Upgrade only if embeddings/clustering get slow (move to 4 vCPU / 8 GB).

---

## 6. Data Model (Postgres)

| Table | Key columns |
| --- | --- |
| `sources` | id, name, type (api/inbox), tos_url, rate_limit, enabled |
| `signals` | id, source_id, url, text, author_hash, created_at, embedding |
| `ideas` | id, title, problem, audience, gap, cluster_id, status |
| `idea_evidence` | idea_id, claim, url, quote, confidence |
| `scores` | idea_id, factor, value, confidence, scored_at, model_version |
| `experiments` | id, idea_id, spec_yaml, frozen_at, kill_date, status, verdict |
| `exp_metrics` | experiment_id, ts, visitors, signups, sales, spend |
| `builds` | id, idea_id, repo_url, stage, review_passed, security_passed |
| `offers` | id, build_id, price, platform, payment_link, live |
| `transactions` | id, offer_id, stripe_id, amount, fee, refund, ts |
| `costs` | id, bet_id, category (ads/tools/llm), amount, ts |
| `approvals` | id, action, payload, cost, risk, status, decided_at |
| `decisions_log` | id, agent, action, reason, evidence_ids, ts (append-only) |
| `runs` | id, agent, started, finished, tokens, cost, error |

Lifecycle: `idea: new → scored → selected → testing → building → selling → scaled | killed`.

---

## 7. Repo Structure

```
jokr/
├── CLAUDE.md                 # rules for Claude Code (see §12)
├── docker-compose.yml
├── config/
│   ├── caps.yaml             # budgets (C6)
│   ├── fit_rules.yaml        # Luca's six rules
│   ├── scoring.yaml          # weights
│   └── sources.yaml          # allowlist + rate limits
├── jokr/
│   ├── agents/  chief.py scout.py analyst.py scorer.py experimenter.py
│   │            builder.py seller.py treasurer.py judge.py gatekeeper.py
│   ├── prompts/              # one .md system prompt per agent
│   ├── connectors/           # hn.py reddit.py producthunt.py github.py arxiv.py ...
│   ├── stats/                # bootstrap.py, bonferroni.py, ci.py
│   ├── db/                   # models.py, migrations/
│   ├── guards/               # caps.py, tos.py, content_lint.py, approval.py
│   └── api/                  # FastAPI routes for dashboard
├── dashboard/
├── data/inbox/               # manual drops (stratup.ai exports etc.)
├── experiments/              # frozen YAML specs (git-tracked)
├── tests/
└── ops/                      # systemd, backup, watchdog, deploy scripts
```

---

## 8. Portfolio Brain (budget allocation)

- Treat each live bet like an arm in a **multi-armed bandit**.
- Allocate next week's budget with **Thompson sampling** on conversion rate.
- Hard limits override math: no bet > 40% of monthly budget; caps from C6 always win.
- Lesson from Duek: **no Kelly-style aggressive sizing.** Small, flat bets until evidence is strong.

**Stage gates:**

| Gate | Pass condition | Next |
| --- | --- | --- |
| G1 Score | Fit gate = 1, score ≥ 6.5 | Luca may approve a test |
| G2 Demand | Frozen bar passed | Build MVP |
| G3 First sale | ≥ 3 paying customers | Sell harder |
| G4 Traction | ≥ $300 CAD/month for 2 months | Scale |
| Kill | Bar failed at kill date, or 0 sales 30 days after launch | Archive + lessons |

---

## 9. Reliability (Duek lessons, built in)

1. **Heartbeat** every 15 min to dashboard + Telegram if silent > 1 h.
2. **Watchdog** (systemd `Restart=always` + external uptime ping).
3. **Ledger first:** record money events *before* any reconcile step (the Duek ledger bug).
4. **Idempotent jobs:** every job can rerun safely.
5. **State never half-written:** DB transactions around multi-step actions.
6. **Retries with backoff** on every external API.
7. **Daily backup** of Postgres to object storage.
8. **Real drawdown tracking:** costs vs revenue from source data, never a cached field.

---

## 10. Security

- SSH: key-only login, no root password, `ufw` (22/80/443 only), `fail2ban`.
- Stripe: **restricted read-only key**. Payment links created by Luca.
- API keys per service, least privilege, rotated every 90 days.
- No secrets in git (pre-commit secret scan).
- Dashboard behind login + HTTPS (Caddy auto-TLS).
- ECC security-reviewer must pass on every JokR release.

---

## 11. Build Phases (Claude Code + ECC)

Each phase: `/plan` → `/tdd` → `/code-review` → security review → merge. Do not start the next phase until acceptance passes.

| Phase | Build | Acceptance test |
| --- | --- | --- |
| P0 | Repo, Docker, Postgres, config, CLAUDE.md | `docker compose up` green; tests run |
| P1 | Scout: HN + Reddit + arXiv + inbox connectors | 500 signals stored, ToS allowlist enforced |
| P2 | Analyst + Scorer | Top-10 idea cards with evidence links, scores reproducible |
| P3 | Gatekeeper + Telegram + minimal dashboard | Approval round-trip works from phone |
| P4 | Experimenter + landing page generator | One live smoke test with frozen spec |
| P5 | Judge + stats | Correct verdict on 3 simulated experiments (incl. a null) |
| P6 | Treasurer (Stripe read-only) + caps | Cap breach freezes spend in test |
| P7 | Builder (Claude Code headless + ECC) | Builds a toy MVP repo, all reviews pass |
| P8 | Seller drafts + Chief weekly loop | Full cycle runs end to end on schedule |
| P9 | Deploy to VPS + watchdog + backups | 7 days unattended, zero missed heartbeats |

**First real bet after P9:** the craftsman's-desk tool.

---

## 12. CLAUDE.md for the JokR repo (paste as-is)

```markdown
# JokR — rules for Claude Code

You are building JokR, an AI venture engine owned by Luca.

## Non-negotiables
- Follow the Constitution (C1–C10) in JokR_BLUEPRINT.md. Enforce in code, not only prompts.
- Never add code that moves money, publishes content, or sends email without an approval_id.
- Official APIs only. Never scrape behind logins. Respect robots.txt and rate limits.
- No metric may be reported unless computed from logged data.

## Workflow
- Use ECC: /plan before coding, /tdd for every module, /code-review before merge,
  security-reviewer before any deploy.
- One phase at a time (see §11). Stop and report when acceptance passes.
- Small commits. Tests must pass before commit.

## Style
- Python 3.12, type hints, ruff + mypy clean.
- Simple code over clever code. Comment the why, not the what.
- Dashboard: minimal, easy to navigate, artistic touch.
```

---

## 13. Kickoff Prompt (first message to Claude Code)

```
Read JokR_BLUEPRINT.md fully. Then:
1. Summarize the Constitution and phases back to me in 10 lines.
2. Run /plan for Phase P0 only.
3. Wait for my approval before writing code.
```

---

## 14. Luca's Setup Checklist

- [ ] Install Claude Code + ECC plugin
- [ ] Anthropic API key (set a monthly spend limit in the console)
- [ ] Reddit, Product Hunt, GitHub, Semantic Scholar API keys
- [ ] Telegram bot token (via @BotFather)
- [ ] Stripe account in your name → create a restricted read-only key
- [ ] VPS (new droplet; keep Duek's separate)
- [ ] stratup.ai account → export ideas into `data/inbox/`
- [ ] Decide business structure (Alberta sole proprietorship is simplest) — confirm with an accountant

---

## 15. Honest Risks

| Risk | Reality | Guard |
| --- | --- | --- |
| No winners for months | Likely early on | Cheap tests, many shots |
| API costs creep | Real | Daily LLM cap (C6) |
| Platform bans for spam | Real | C2 + C3 + C4 |
| Sources change or close | Real | Connector per source, fail soft |
| Overbuilding JokR instead of selling | Biggest risk | Stop at P9; ship a real bet |

**JokR is only worth it if he ships bets. The engine is the means. Revenue is the end.**
