# dinner

what's for dinner?

Weekly meal planner: pick a recipe for each weekday, get one aggregated grocery
list for the week. See [project.md](project.md) for the full plan.

**Status**: phase 3 — the core app is complete. Recipe bank (URL import, review
queue, search, tags), week planner (choose, random, skip, lock, fill, reroll)
and an aggregated grocery list per week. Next up is hardening (phase 4), then
the optional LLM suggestion feed and Picnic integration.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

## Local development

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py
```

Then open http://localhost:5000. Migrations apply automatically on start.

No `.env` is needed locally: with `SITE_PASSWORD` unset the password gate is
disabled and the database defaults to `./data/dinner.db`.

## Deployment

Clone to `~/projects/dinner` on the webserver, then:

```bash
cp .env.example .env
```

Fill in `SECRET_KEY`, `SITE_PASSWORD` and `INGEST_TOKEN` — **generate them on
the server**, don't copy development values:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Then:

```bash
mkdir -p data && docker compose up -d --build
```

Add the Caddy block in `~/projects/infra/caddy/Caddyfile`:

```
http://dinner.btblog.dev { reverse_proxy dinner:8000 }
```

Add the Cloudflare Tunnel public hostname `dinner.btblog.dev` → `http://caddy:80`,
and a row in `~/projects/infra/SERVICES.md`.

Migrations run from the container CMD before gunicorn binds, so `docker compose
up -d --build` is the whole deploy — there is no separate migrate step to
forget.

## Layout

| path | what |
|---|---|
| `run.py` | dev entrypoint |
| `config.py` | environment config |
| `db.py` | connection helper + migration runner (`python db.py`) |
| `backup.py` | JSON dump-and-load (`python backup.py dump\|load`) — see project.md phase 4 |
| `db/migrations/` | versioned schema, applied in filename order |
| `app/__init__.py` | app factory, password gate |
| `app/queries.py` | all SQL |
| `app/weeks.py` | week arithmetic |
| `app/planner.py` | filtered, staleness-weighted random picking |
| `app/grocery.py` | unit aggregation and shopping-list rendering |
| `app/parse.py` | ingredient lines → quantity/unit/name |
| `app/extract.py` | URL → recipe (JSON-LD, microdata) + fetch guards |
| `app/routes_*.py` | blueprints |
| `tests/` | pytest suite |
| `data/` | SQLite file (gitignored, bind mounted in Docker) |
