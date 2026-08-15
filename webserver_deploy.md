# Dinner — webserver deployment

Handoff doc for a Claude Code session running *on* the webserver. The app was
built on a Windows dev machine with no access to the server, so **the Docker
build has never been executed**. Everything below is written but unverified;
the first `docker compose up -d --build` is its real test.

Read alongside `project.md` (the plan) and `README.md`. The conventions here
follow `~/projects/research_aggregator/webserver_deploy.md` — that project's
deploy record is the authority if anything conflicts.

## What this is

A weekly meal planner: pick a recipe per weekday, get one aggregated grocery
list. Flask + SQLite, no build step, no external services. Phases 0–3 are
complete (recipe bank, week planner, grocery list). Phase 5 (LLM suggestion
sweep) and phase 6 (Picnic) are not built.

## Prerequisites

- The shared infra stack in `~/projects/infra` (Caddy + cloudflared) running,
  and the external Docker network `web` present:
  `docker network ls | grep web`
- Host user's uid is 1000 (the Dockerfile creates its app user at that uid so
  the bind-mounted `./data` stays writable). Check with `id -u`.

## Deploy

```bash
git clone https://github.com/denzilly/dinner ~/projects/dinner
cd ~/projects/dinner
cp .env.example .env
mkdir -p data
```

Fill `.env`. **Generate the secrets on the server** — do not reuse development
values:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

- `SECRET_KEY` — signs the session cookie. **Required** whenever
  `SITE_PASSWORD` is set; the app raises on startup if it is missing, so a
  blank one here means a crash-looping container, not a silent downgrade.
- `SITE_PASSWORD` — the whole-site gate. If left empty the gate is **disabled
  entirely**, which is fine locally and wrong here.
- `INGEST_TOKEN` — bearer token for `POST /api/recipes/ingest`. Fails closed:
  while it is unset every API call gets a 401, so the Share Sheet shortcut and
  Hermes simply won't work until it is set.
- `DATABASE_PATH` — leave as `/app/data/dinner.db`.

Then:

```bash
docker compose up -d --build
docker compose logs -f dinner
```

Migrations run from the container CMD before gunicorn binds, so there is no
separate migrate step — `up -d --build` is the whole deploy, and a restart
always brings the schema up to date before a request is served.

## Ingress

Caddy block in `~/projects/infra/caddy/Caddyfile`:

```
http://dinner.btblog.dev {
    reverse_proxy dinner:8000
}
```

Then reload Caddy, add the Cloudflare Tunnel public hostname
`dinner.btblog.dev` → `http://caddy:80` in the dashboard, and add a row to
`~/projects/infra/SERVICES.md`.

`docker-compose.yml` also publishes `127.0.0.1:8001:8000`. That is **not** for
public traffic — Caddy reaches the container by name over the `web` network.
It exists so the Hermes gateway (`network_mode: host`, and therefore unable to
resolve container names on `web`) can POST to
`http://127.0.0.1:8001/api/recipes/ingest`. Drop the `ports:` block if you
don't want that. Check nothing else already holds 8001: `ss -ltnp | grep 8001`.

## Verification checklist

- [ ] `docker compose ps` shows `dinner` healthy (the healthcheck hits `/healthz`)
- [ ] `curl -s localhost:8001/healthz` returns `{"status":"ok"}`
- [ ] `https://dinner.btblog.dev` asks for the password; the wrong one is
      rejected, the right one grants a session, logout revokes it
- [ ] Import works: paste a recipe URL into the box on `/recipes/review`
- [ ] Accept it, plan it on a day, and check `/groceries` aggregates it
- [ ] `POST /api/recipes/ingest` with the bearer token returns 201 and is
      **not** redirected to the login page
- [ ] `data/dinner.db` is picked up by whatever backup process this server uses
      (single file, a periodic copy is enough)

## Known risks on first build

The build is unverified, so expect to debug at least one of these:

- **`useradd --uid 1000 dinner`** fails if something already occupies uid 1000
  in `python:3.13-slim`. If so, pick another uid and `chown` `./data` to match.
- **`./data` permissions** — the container runs as non-root. If SQLite reports
  "unable to open database file", the bind-mounted directory isn't writable by
  the container user. `chown 1000:1000 data`.
- **Pinned versions** in `requirements.txt` (Flask 3.1.0, gunicorn 23.0.0,
  requests 2.32.3, beautifulsoup4 4.12.3) may be older than what is current.
  They are pinned deliberately so the server matches what was developed
  against; bump only with a reason.
- **`jamieoliver.com` fails TLS verification** with certifi (incomplete
  certificate chain on their side — browsers paper over it via AIA fetching,
  Python does not). It surfaces as a readable error pointing at manual entry.
  Worth re-testing here: it may behave differently on Linux.

## First real use

The bank starts empty, and that is deliberate — gather recipes against the
live site rather than a local database, so there is never a `dinner.db` to
migrate. `demo_recipes.txt` in the repo has nine URLs to start with (eight NYT
Cooking, one Jamie Oliver); paste them into the box on `/recipes/review`.

Assign aisles at `/ingredients` once there are recipes in — until then every
grocery line lands in "overig" and the aisle grouping does nothing.

## Not built yet

- **Phase 4 (hardening)**: backup confirmation, a JSON export/import so the
  bank isn't hostage to one SQLite file.
- **Phase 5 (weekly LLM sweep)**: deliberately unspecified. Would call
  OpenRouter directly and run from the `bart` crontab, not Hermes — Hermes'
  cron scripts are sandboxed to `~/.hermes/scripts/` and it has no docker
  socket, which is why `research_aggregator` runs from OS cron.
- **Phase 6 (Picnic)**: `python-picnic-api2` is the library to use. Scoped to
  building a cart, never placing an order.

## Deployment record — 2026-08-15

First deploy, done live on `bartserver`. Ran essentially as planned above —
nothing here needed a real deviation from the plan, unlike
`research_aggregator`'s Docker/cron/auth rewrites. Differences from the
"Known risks" section worth recording:

- **No uid 1000 collision.** `useradd --uid 1000 dinner` in `python:3.13-slim`
  succeeded cleanly on the first build — the risk flagged above didn't
  materialize on this image.
- **`jamieoliver.com` TLS worked fine here.** The dev-machine certifi failure
  didn't reproduce on this host/Python combination — all nine
  `demo_recipes.txt` URLs, including the Jamie Oliver one, imported
  successfully via `/recipes/import` with `jsonld` extraction. Worth
  re-flagging if it starts failing later, but not a deploy blocker.
- **`~/projects/dinner` was already present on the server** (cloned ahead of
  time), so the `git clone` step was skipped — everything from `.env` onward
  ran as documented.
- **Secrets generated on-server** as instructed (`SECRET_KEY`, `SITE_PASSWORD`,
  `INGEST_TOKEN`, all via `secrets.token_urlsafe`), none reused from dev.
- **Cloudflare Tunnel route** was added by the user directly in the dashboard
  (Zero Trust > Networks > Tunnels > Published application routes), same
  manual step as every other service on this host — no API/CLI path existed
  for it (`infra/.env` only holds `TUNNEL_TOKEN`, no API credential).

### Verification checklist — actual results

- [x] `docker compose ps` shows `dinner` healthy
- [x] `curl -s localhost:8001/healthz` → `{"status":"ok"}`
- [x] `https://dinner.btblog.dev` asks for the password; wrong password
      rejected (200, no session granted), correct password grants a session
      (subsequent loads 200), logout revokes it (subsequent loads redirect) —
      confirmed both over loopback and the public domain
- [x] Import works: pasted all nine `demo_recipes.txt` URLs into
      `/recipes/review`'s box — 9/9 imported, `jsonld` extraction on all
- [x] Accepted a recipe, planned it on a weekday (note: the board only shows
      Mon–Fri by design — a plan set on a weekend date saves to the DB but
      never renders on the board, confirmed via direct DB read; not a bug),
      `/groceries` correctly aggregated its ingredients into "overig" (no
      aisles assigned yet, as expected)
- [x] `POST /api/recipes/ingest` with the bearer token → 201 with real
      extraction data, not a login redirect; no/wrong token → 401 both times
- [ ] **`data/dinner.db` backup coverage — still open.** Confirmed there is
      no backup process anywhere on this host (empty crontab besides
      `research_aggregator`'s ingestion job, nothing in `/etc/cron.d`,
      `cron.daily`, or `cron.weekly`). This isn't dinner-specific — see
      `infra/SERVICES.md`'s "Backups" section, now also listing
      `dinner/data/dinner.db`. Setting up host-wide backups (even a simple
      off-host `rsync`/`restic`) is worth doing once, not per-project.

Row added to `infra/SERVICES.md`. Next real work is phase 4 (hardening) per
`HANDOVER.md`: confirm backup coverage (blocked on the host-wide gap above)
and add JSON export/import.
