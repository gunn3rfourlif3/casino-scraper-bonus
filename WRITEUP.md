# Taking this to production: 200 casinos, updated daily

## Where it runs

One Hetzner CPX21 (3 vCPU / 4 GB / 80 GB, Falkenstein), Ubuntu 24.04, everything in Docker
Compose: the scraper, Postgres, Caddy. 200 pages a day is roughly four minutes of work —
this is a small job wearing a big word, and a single box I can SSH into beats a Lambda fan-out
I have to reconstruct from CloudWatch when a selector breaks. The role owns the full stack, so
the fewer moving parts between me and a broken scrape, the better. Scale-out point: if the
crawl grows past a few thousand pages or needs residential egress, the fetch step moves to a
queue (Redis + RQ) with workers I can add, while Postgres and the site stay where they are.

## Where the model runs

The prototype does its extraction with `qwen3:8b` on my own machine through Ollama, which
proves the pipeline needs no paid API to work. That does not survive the move to a CPX21: an
8B model needs more RAM than the box has, and on shared CPU it would turn a four-minute job
into an hour. So production uses the Claude API (Haiku) and keeps the local path as the dev
and offline option — the provider is a flag, not a rewrite, which is why it's built that way.

The alternative, if keeping the data on our own hardware matters, is a GPU box — a Hetzner
GEX44 is roughly €180/month, an order of magnitude more than the API line below. That is a
data-residency decision, not a cost one, and nothing here needs it.

## Database and backups

Postgres 17. Three tables: `casinos`, `bonuses` (current state, one row per casino), and
`bonus_snapshots` (append-only, one row per scrape per casino). The snapshot table is the
point — it makes "this casino quietly moved from 35× to 45×" a query rather than a lost fact,
and it gives you a diff to show affiliates.

Backups: nightly `pg_dump` piped into [restic](https://restic.net) to Backblaze B2, 30 daily
and 6 monthly snapshots, plus WAL archiving to the same bucket for point-in-time recovery.
A weekly cron restores the newest backup into a throwaway database and asserts the row count —
an untested backup is a rumour.

## Domain, DNS, HTTPS

Domain registered at Cloudflare Registrar (at-cost, no renewal gouging), DNS on Cloudflare with
the record proxied — free CDN in front of a static page, and the origin IP stays off public DNS.
Caddy on the box terminates TLS with automatic Let's Encrypt certs and renewal; HSTS on. The
public site is static HTML built from the database, so Cloudflare serves nearly all of it from
cache and the origin only gets hit on a purge after each build.

The prototype page is on GitHub Pages behind a CNAME, which is the same shape at zero cost:
static output, a CDN, an automatic certificate. The reason production moves off it is that the
page stops being static output committed to a repo once it's built from a database on a
schedule.

## Scheduling and knowing it failed

A systemd timer, not cron: `OnCalendar=*-*-* 04:17`, `RandomizedDelaySec=900`, logs in journald,
and an `OnFailure=` unit that fires an alert if the service exits non-zero. Cron silently emails
a mailbox nobody reads.

Silence is the enemy, so the alerting is layered:

- **A dead-man's switch.** The job pings a [Healthchecks.io](https://healthchecks.io) URL on
  success. If the run never starts — box down, timer disabled, disk full — the *absence* of
  the ping alerts. A job that fails to run is the failure mode cron monitoring misses.
- **Per-casino status rows.** Each casino gets a row in a `scrape_runs` table with status and
  error. The run exits non-zero if more than 10% failed.
- **Change-rate alarms, which matter more than errors.** A layout change usually doesn't throw;
  it returns confident nonsense. So: alert if more than 15% of casinos changed their wagering
  multiplier in one night, if any field flips to null that was populated yesterday, or if a
  record hasn't refreshed in 72 hours. Stale rows get flagged on the page rather than shown as
  current.
- **Sentry** for unhandled exceptions, alerts to a Slack webhook, Healthchecks escalating to
  email and SMS.

The same principle already shows up in the prototype. A scraper that refuses to scrape looks
well-behaved, and an extractor that reads the wrong row on a page returns a perfectly valid
number. Both are silent failures, so `validate()` cross-checks every extracted percentage
against the source URL slug, and the per-record warnings travel into the JSON rather than to
a log nobody reads.

Cost control worth doing on day one: hash the extracted page text and only call the model when
the hash changes. Bonus terms move maybe weekly, so steady-state LLM spend drops by an order
of magnitude, and the diff tells you which pages to look at.

## Rough monthly cost

| Item | € / month |
| --- | --- |
| Hetzner CPX21 | 8 |
| Domain (amortised) | 1 |
| Backblaze B2 (backups, a few GB) | 1 |
| Claude API — 200 pages/day, Haiku, with hash-skip caching | 3–6 |
| Cloudflare, Healthchecks, Sentry (free tiers) | 0 |
| **Total** | **~€13–16** |

Without the caching the API line is closer to €25–30, which is still not the thing to optimise
first. The real cost at this scale is the hour a week someone spends on broken selectors — which
is the argument for leaning on the model for interpretation and keeping the parser dumb.

---

# Five lines on AI

1. Built with Claude (Claude Opus 5 in Claude Code). The scraper's extraction step runs
   `qwen3:8b` locally via Ollama by default; `--provider anthropic` switches it to the API.
2. The bug worth reporting: its `robots.txt` check reported a confident "robots.txt disallows"
   for all three target URLs, and the run stopped having scraped nothing.
3. Nothing was actually disallowed. `RobotFileParser.read()` fetches robots.txt with urllib's
   own user agent, the CDN 403s it, and `read()` swallows that error and sets `disallow_all` —
   so every permission check returns False and the failure reads as politeness.
4. I caught it because I'd read AskGamblers' robots.txt myself before writing any code and knew
   those paths were permitted. The fix fetches robots.txt with our own user agent and treats an
   unreadable one as a loud failure; two tests pin the real rules so it can't regress quietly.
5. The lesson I'd carry: AI mistakes that fail in the *safe* direction are the expensive ones,
   because nothing looks broken. The extraction itself was fine — an 8B local model got all
   twelve fields right, including distinguishing `40xb` from `25x(d+b)` — so the model wasn't
   the risk. Its glue code was.

**Time taken:** 1h50m
