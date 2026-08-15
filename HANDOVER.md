# Handover — 2026-08-16

For the Claude Code session picking this up on the webserver.

## Where things stand

A weekly meal planner (Flask + SQLite, no build step). Phases 0–3 are built,
committed and pushed: `main` is at `89f75ce`. 181 tests pass on the dev
machine. **Nothing has ever been deployed, and the Docker image has never been
built** — that is the job.

Built on Windows with no server access, so every deployment artefact
(`Dockerfile`, `docker-compose.yml`) is written but untested.

## Your task

First deployment: clone to `~/projects/dinner`, build, get it live behind
Caddy at `dinner.btblog.dev`, work through the verification checklist.

**Read [webserver_deploy.md](webserver_deploy.md) first** — steps,
prerequisites, ingress config, checklist, and the specific things most likely
to break on first build. Everything you need is there.

## Reading order

| file | what it gives you |
|---|---|
| `webserver_deploy.md` | how to deploy, and what will probably go wrong |
| `project.md` | the plan, and *why* each decision went the way it did |
| `README.md` | code layout, local dev, tests |
| `plan.md.txt` | the original brainstorm, kept for provenance |

`project.md` is worth skimming before changing anything — most of the
non-obvious code has a reason recorded there rather than in the code.

## Things that will bite

- **`SECRET_KEY` is mandatory when `SITE_PASSWORD` is set.** The app raises on
  startup instead of silently running unsigned, so a blank one is a
  crash-looping container, not a degraded gate.
- **`INGEST_TOKEN` fails closed.** While unset, every `/api/recipes/ingest`
  call gets a 401. That is deliberate — an unset token must not mean "open".
- **The container runs non-root (uid 1000).** If SQLite says "unable to open
  database file", the bind-mounted `./data` isn't writable: `chown 1000:1000 data`.
- **Migrations run from the container CMD** before gunicorn binds. There is no
  separate migrate step and there must never be one — that footgun is exactly
  what `research_aggregator` kept tripping over.

## Don't

- **Don't migrate a database over.** The recipe bank is empty on purpose;
  recipes get gathered against the live site so there is never a `dinner.db` to
  move. `demo_recipes.txt` has nine URLs to start with.
- **Don't bump the pinned dependency versions** without a reason. They are
  pinned so the server matches what the code was developed and tested against.
- **Don't build phase 5 or 6.** The LLM sweep is deliberately unspecified, and
  Picnic needs a feasibility spike first. See `project.md`.

## Afterwards

Record what actually happened — especially anything that diverged from
`webserver_deploy.md` — by appending to that file, the same way
`research_aggregator/webserver_deploy.md` documents its deviations. That file
is the deployment record, not just a plan.

Then phase 4 (hardening) is the next piece of work: confirm `data/dinner.db` is
covered by this server's backups, and add a JSON export/import so the bank
isn't hostage to a single SQLite file.
