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

## Recipe photos

Imports download the recipe's photo and store it in `data/images/`, served
back from `/recipes/images/<name>`. Downloading rather than hotlinking: recipe
sites rotate CDN URLs and some refuse requests carrying a foreign referrer, so
a stored URL rots. Files are named by the SHA-256 of their own bytes, so
re-importing an unchanged photo doesn't accumulate duplicates.

A failed download is a warning on the review card, never a rejected recipe.

Recipes imported before this existed have no photo. Backfill them:

```bash
.venv/Scripts/python.exe fetch_images.py
```

`--all` re-fetches every recipe (replacing existing photos) and `--dry-run`
reports what it would fetch without writing.

Because the images live under `data/`, the existing bind mount covers them —
there is no second volume to add on deploy. Note that `backup.py` dumps the
database only, so photos are not in a JSON export; re-run `fetch_images.py`
after a restore.

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
| `fetch_images.py` | backfill recipe photos for rows imported before images existed |
| `db/migrations/` | versioned schema, applied in filename order |
| `app/__init__.py` | app factory, password gate |
| `app/queries.py` | all SQL |
| `app/weeks.py` | week arithmetic |
| `app/planner.py` | filtered, staleness-weighted random picking |
| `app/grocery.py` | unit aggregation and shopping-list rendering |
| `app/parse.py` | ingredient lines → quantity/unit/name |
| `app/extract.py` | URL → recipe (JSON-LD, microdata) + fetch guards |
| `app/images.py` | downloads and stores recipe photos |
| `app/routes_*.py` | blueprints |
| `tests/` | pytest suite |
| `data/` | SQLite file and `images/` (gitignored, bind mounted in Docker) |
