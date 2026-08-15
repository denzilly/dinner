# Dinner — weekly meal planner

Implementation plan. Derived from `plan.md.txt` (the original brainstorm,
kept as-is). Follows the same shape as `research_aggregator` — that project
is the closest structural analogue (SQLite + Flask + cron-driven LLM
ingestion + a review feed), and its `webserver_deploy.md` records the
deployment conventions this one should reuse from the start.

**Goal**: pick a recipe for each weekday, get one aggregated grocery list
for the week. Later: auto-suggested recipes from an LLM sweep, and Picnic
ordering.

## Decisions

| | |
|---|---|
| Stack | Python 3.13 + Flask + gunicorn, Jinja templates, vanilla JS (no build step) |
| DB | SQLite, single file at `data/dinner.db` |
| Deploy | Docker + `docker-compose.yml` on the external `web` network, `~/projects/dinner` on the server |
| Ingress | Caddy block `http://dinner.btblog.dev { reverse_proxy dinner:8000 }` → Cloudflare Tunnel hostname → `http://caddy:80` |
| Auth | Whole-site `SITE_PASSWORD` gate + `SECRET_KEY` session cookie, from day one |
| Users | Household, shared. One plan, one recipe bank. No `user_id` columns anywhere |
| Locale | Metric, Dutch ingredient names (Picnic is NL) |
| LLM (phase 5) | OpenRouter direct, `deepseek/deepseek-v4-flash` pinned via `OPENROUTER_MODEL` |
| Scheduling | OS crontab as `bart`, not Hermes (see "Hermes" below) |

Three things carried over deliberately from `research_aggregator`'s
experience:

1. **Password gate is built in phase 0**, not bolted on after deployment.
2. **No second write-secret.** One password, one trusted household.
3. **A real migration runner** (`db/migrations/NNN_*.sql` + a `schema_version`
   table) instead of ad-hoc `ALTER TABLE` calls inside `init_db()`. Every
   schema change in that project turned into a "must run `python db.py`
   before deploying app code or the site 500s" footgun. Versioned migrations
   that run automatically on container start remove the whole class of
   problem.

## Development → deployment

Built on the Windows dev machine, deployed by cloning
`github.com/denzilly/dinner` to `~/projects/dinner` on the server — same route
`research_aggregator` took. Docker builds on the server, so nothing
Windows-specific ships.

- **`.env` is never committed.** Create it on the server from `.env.example`
  and **generate `SECRET_KEY` / `SITE_PASSWORD` / `INGEST_TOKEN` there** —
  dev and prod values should differ, so a leaked dev secret is worthless
  against the live site.
- **`.gitignore` from commit one**: `.env`, `data/`, `*.db`, `__pycache__/`,
  `*.pyc`, `.venv/` (copy `research_aggregator`'s).
- **`.gitattributes` with `* text=auto eol=lf`** — a CRLF shebang in any shell
  script surfaces on Linux as Docker's cryptic `exec: no such file or
  directory`.
- **Filenames rigidly lowercase.** Windows is case-insensitive, Linux is not;
  `render_template("Base.html")` against `base.html` works locally and 500s in
  production. The most common dev→server breakage there is.
- **`data/` won't exist after clone** (gitignored). `mkdir data` on the server;
  it must be writable by the container user — see `marketsarchive`'s Dockerfile
  comment on the uid-1000 reasoning.
- **Pin versions in `requirements.txt`** so the server build matches what was
  developed against.

**Gather recipes against the live site, not locally.** Phase 0 deploys before
any feature exists precisely so the real recipe bank is only ever built on the
server. Otherwise weeks of gathering produce a local `dinner.db` that has to be
migrated over — the exact dance `research_aggregator` went through with its 177
papers. Keep the local DB disposable test data.

## Repo layout

```
dinner/
  app/
    __init__.py        # app factory, password gate, template filters
    routes_plan.py     # week board
    routes_recipes.py  # recipe bank CRUD
    routes_grocery.py  # grocery list
    routes_api.py      # token-gated ingest endpoint (phase 1)
    extract.py         # URL -> recipe: JSON-LD first, LLM fallback
    parse.py           # free-text ingredient lines -> quantity/unit/ingredient
    queries.py         # all SQL lives here, not in routes
    weeks.py           # week arithmetic (Monday-of, weekday list)
    planner.py         # random pick + filter logic
    grocery.py         # ingredient aggregation + unit math
    templates/
    static/
  db/
    schema.sql
    migrations/
  ingest/              # phase 5: scraper + OpenRouter scoring
  data/                # dinner.db (bind-mounted, gitignored)
  Dockerfile
  docker-compose.yml
  .env.example
  run.py
  config.py
```

## Data model

Keyed on real dates rather than week-number + weekday: "this week" becomes a
date-range query, past weeks stay queryable for free, and "what did we eat
last month" needs no extra tables.

```sql
CREATE TABLE recipes (
  id            INTEGER PRIMARY KEY,
  title         TEXT NOT NULL,
  source_url    TEXT,
  source_name   TEXT,              -- 'AH Allerhande', 'manual', ...
  instructions  TEXT,
  servings      INTEGER NOT NULL DEFAULT 4,   -- what the quantities below assume
  prep_minutes  INTEGER,
  cook_minutes  INTEGER,
  image_path    TEXT,
  notes         TEXT,              -- household notes: "kids liked this"
  status        TEXT NOT NULL DEFAULT 'active',
                                   -- active | suggested | rejected | archived
  extraction    TEXT,              -- jsonld | microdata | llm | manual
  extraction_warnings TEXT,        -- JSON array: lines parse.py wasn't sure about
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  last_planned_on TEXT             -- drives "don't suggest what we just ate"
);

CREATE TABLE ingredients (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,   -- canonical, lowercase: 'ui', 'kipfilet'
  aisle        TEXT,                   -- groente | zuivel | vlees | kast | ...
  default_unit TEXT
);

CREATE TABLE recipe_ingredients (
  id            INTEGER PRIMARY KEY,
  recipe_id     INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
  quantity      REAL,
  unit          TEXT,               -- g | ml | el | tl | stuk | teen | blik | snuf
  note          TEXT,               -- 'fijngesneden'
  optional      INTEGER NOT NULL DEFAULT 0,
  sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL          -- cuisine | diet | protein | effort | season | free
);

CREATE TABLE recipe_tags (
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (recipe_id, tag_id)
);

CREATE TABLE plan_days (
  plan_date TEXT PRIMARY KEY,       -- 'YYYY-MM-DD'
  state     TEXT NOT NULL,          -- empty | skip | planned
  recipe_id INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
  servings  INTEGER,                -- override; NULL = recipe default
  locked    INTEGER NOT NULL DEFAULT 0,  -- survives "reroll the week"
  note      TEXT                    -- 'eten bij mijn ouders'
);

CREATE TABLE grocery_lists (
  id           INTEGER PRIMARY KEY,
  week_start   TEXT NOT NULL UNIQUE,   -- the Monday
  generated_at TEXT NOT NULL
);

CREATE TABLE grocery_items (
  id            INTEGER PRIMARY KEY,
  list_id       INTEGER NOT NULL REFERENCES grocery_lists(id) ON DELETE CASCADE,
  ingredient_id INTEGER REFERENCES ingredients(id),
  label         TEXT NOT NULL,      -- rendered line, incl. manual free-text items
  quantity      REAL,
  unit          TEXT,
  checked       INTEGER NOT NULL DEFAULT 0,
  manual        INTEGER NOT NULL DEFAULT 0   -- added by hand, survives regeneration
);
```

Plus FTS5 over `recipes(title, instructions)` for the recipe bank search box —
same pattern as the papers index in `research_aggregator`.

### Tags vs. numeric filters

The brainstorm was undecided here. Split it: **tags are categorical**
(`kind` groups them so the filter UI can render "Cuisine: italiaans /
mexicaans / ..." as separate chip rows), **prep time is numeric** on the
recipe itself and filtered with a slider, because bucketing minutes into tags
(`<30min`) goes stale the moment a recipe is edited. `kind = 'free'` is the
escape hatch for tags that don't fit a group yet — if one accumulates enough
members, promote it to its own kind later.

### Unit aggregation — the actually-hard part

Summing "2 uien + 1 ui" is easy; "1 blik tomaten + 400 g tomaten" is not.
The rule for `grocery.py`:

The demo bank turned out to be American (NYT Cooking: cups, pints, pounds)
while the shopping is Dutch and metric, so the lexicon carries both and the
rendering rule is **convert what is exact, flag what is not**:

- `lb → 454 g`, `oz → 28.3 g`, `cup → 240 ml`, `pint → 473 ml` are exact
  conversions within a family and are applied silently.
- A **cup of a solid** is not convertible to grams without a per-ingredient
  density (a cup of flour and a cup of spinach are not the same weight), so it
  stays "2 cups orzo" with a marker showing it was left alone. Fabricating that
  number is exactly the silent wrongness this whole section exists to avoid.
- Quantities are stored **as parsed, in their source unit**. Conversion happens
  at grocery-list render time, never at ingest -- so the rule can change later
  without a migration or a re-import.

- Units belong to **families**: mass (`g`, `kg`, `oz`, `lb`), volume (`ml`, `l`,
  `el`/`tsp`, `tl`/`tbsp`, `cup`, `pint`), count (`stuk`, `teen`, `blik`, `bos`),
  and vague (`snuf`, `naar smaak`).
- Within a family, convert to a base unit, sum, then render in the friendliest
  unit (1200 g → "1,2 kg").
- Across families for the same ingredient, **do not guess** — emit both on one
  line: "tomaten — 1 blik + 400 g". Guessing a can's weight is exactly the
  kind of silent wrongness that makes people stop trusting the list.
- Vague units never aggregate; they're dropped from the grocery list entirely
  (you're not shopping for a pinch of salt).
- Scaling: a day's quantities are multiplied by `servings / recipe.servings`
  before aggregation.

## Getting recipes in

There is **no existing recipe bank to import** — it gets built from scratch by
gathering recipes over time. So the ingestion path is not a phase-5 nicety, it
is how the bank comes to exist at all, and it gets built in phase 1.

One entry point serves every source:

```
POST /api/recipes/ingest        Authorization: Bearer <INGEST_TOKEN>

{"url": "https://..."}                  # app fetches and extracts
{"title": ..., "ingredients": [...],    # loose JSON, free-text lines
 "instructions": ..., "source_url": ...}
```

- Everything arrives as `status = 'suggested'`. Nothing enters the bank
  unreviewed, regardless of who or what posted it.
- **Exempt from the `SITE_PASSWORD` gate** — it authenticates with
  `INGEST_TOKEN` instead. The `before_request` guard needs the same exemption
  treatment `/login` and static assets get, or API clients receive an HTML
  login page instead of a 200.
- Response returns the new id and a deep link to its review card.
- Idempotent on `source_url` — re-posting the same URL updates the pending
  suggestion instead of creating a duplicate.

**Extraction** (`extract.py`) — three tiers, all normalizing to one internal
shape so nothing downstream cares which one fired:

1. **schema.org JSON-LD** — `<script type="application/ld+json">` with
   `@type: Recipe`. Present on most commercial recipe sites, because Google's
   recipe rich-result cards require it. Gives title, ingredient lines,
   instructions, `recipeYield`, `prepTime`/`cookTime`, cuisine. Deterministic,
   free, cannot hallucinate. Handle the shape variance: bare object vs list vs
   `@graph`, instructions as string / list / `HowToStep[]`, ISO-8601 durations
   (`PT45M` → 45), `recipeYield` as `"4"` or `"4 personen"`.
2. **Microdata/RDFa** (`itemtype="schema.org/Recipe"`) for older sites. Same
   fields, different parse. Add when a site actually needs it.
3. **LLM fallback** — only if 1 and 2 find nothing. Readability-strip the HTML,
   send to OpenRouter (`deepseek-v4-flash`) demanding strict JSON in the same
   shape, then **validate against that shape before accepting**. Cents per
   call, rarely needed, and the only tier that can be confidently wrong —
   hence `recipes.extraction`, so the review card can flag it.

**Normalization stays app-side and always runs** (`parse.py`). Note that
JSON-LD gives *fields*, not ingredient *structure* — `recipeIngredient` is an
array of free text, so every tier's output still needs parsing:

```
"500 g rundergehakt"    -> 500,  g,     rundergehakt
"2 uien, fijngesneden"  -> 2,    stuk,  ui            note='fijngesneden'
"1½ el olijfolie"       -> 1.5,  el,    olijfolie
"snufje zout"           -> None, snuf,  zout          (dropped from groceries)
```

Rule-based: a unit lexicon plus a quantity regex, matched against existing
canonical `ingredients` (case- and plural-tolerant), creating rows only when
genuinely new. Dutch specifics that will bite: decimal **commas** (`1,5 kg`),
unicode vulgar fractions (`½`, `¼`), ranges (`2-3 tenen`), and plural→singular
so `uien` and `ui` share one row. Lines it can't confidently parse go into
`extraction_warnings` and surface on the review card — a silently wrong
quantity reaching the grocery list is worse than asking.

**SSRF guard**: this endpoint makes the server fetch an arbitrary URL. Even
behind the token and password gate, resolve the host first and refuse private
and loopback ranges, cap response size, set a short timeout, and don't follow
redirects into those ranges either. The container sits on the shared `web`
network with everything else behind Caddy, so an unguarded fetcher is a way to
reach neighbours.

### Clients of that endpoint

| Client | Effort | Use |
|---|---|---|
| "Add from URL" box in the webapp | trivial | Desk-based adding, **bulk import runs** — paste a list of gathered URLs, review the queue |
| iOS/Android Share Sheet shortcut | ~30 min, no code in this repo | See a recipe on your phone → share → it's in the queue. Expected everyday path |
| Bookmarklet | trivial | Desktop browser equivalent |
| Hermes | later | Only for *conversational* adds ("add this, but vegetarian") |
| Weekly LLM sweep (phase 5) | later | Unattended discovery |

**Reaching it from Hermes**: its gateway runs `network_mode: host`, so it is in
the host's network namespace and cannot resolve `dinner:8000` — that name only
exists on Docker's `web` network. Publish a loopback-bound port in
`docker-compose.yml` (`ports: ["127.0.0.1:8001:8000"]`) and let Hermes POST to
`http://127.0.0.1:8001/api/recipes/ingest`. Traffic stays on the box, and the
`127.0.0.1` bind keeps it unreachable from outside; Caddy still serves the
public side over the `web` network unchanged. The alternative — calling the
public `https://dinner.btblog.dev` URL — works too, but hairpins out through
Cloudflare and back for no gain.

**On Hermes specifically**: it should be a client of this API, not a thing that
knows the schema. It can't reach `data/dinner.db` regardless (only `~/.hermes`
is mounted in its container), so HTTP is the only available path anyway — and
teaching it table structure would mean every migration here silently breaks it,
plus ingredient canonicalization would get improvised per call instead of
happening in `parse.py`. As a client it needs to know one URL and one token,
and never needs updating when this schema changes.

Because the Share Sheet shortcut covers the same ground with no LLM, no cost
and nothing to maintain, Hermes is worth wiring up only once the conversational
case is actually wanted. The endpoint makes both possible; neither is blocked
on the other.

## Phases

### Phase 0 — Scaffold and deploy an empty shell ✅ built, not yet deployed

Get a live, password-gated "Dinner" page through the tunnel *before* building
features, so deployment is never a big-bang step at the end.

- Flask app factory, `before_request` password gate, `/login`, `/logout`.
- `config.py` reading `.env`; `DATABASE_PATH` set **explicitly** to
  `/app/data/dinner.db` in `.env` (a present-but-empty value is not the same
  as unset — this bit the last project).
- Base image `python:3.13-slim` rather than 3.12 (which `research_aggregator`
  uses) — `python-picnic-api2` requires 3.13+, and adopting it now costs
  nothing versus bumping the image later.
- Migration runner: applies `db/migrations/*.sql` in order on startup, records
  applied versions in `schema_version`.
- Dockerfile (python:3.12-slim, gunicorn, non-root `node`-equivalent user,
  healthcheck), `docker-compose.yml` joining the external `web` network and
  mounting `./data`.
- Server: clone to `~/projects/dinner`, `docker compose up -d`, add the Caddy
  block, add the Cloudflare Tunnel hostname, add a row to
  `~/projects/infra/SERVICES.md`.

**Done when**: `https://dinner.btblog.dev` asks for a password and renders an
empty week board behind it.

### Phase 1 — Recipe bank and ingestion ✅ built, not yet deployed

Built in this order, because the bank starts empty and the fastest path to a
useful bank is the extractor, not the form:

1. `parse.py` — free-text ingredient line parsing, canonical ingredient
   matching. Everything else depends on it.
2. `extract.py` + `POST /api/recipes/ingest` — JSON-LD extraction, `suggested`
   status, idempotent on `source_url`.
3. **Review queue** page — card per suggestion: extracted fields shown
   editable, Accept (→ `active`) / Reject (→ `rejected`, never resurfaces).
   Extraction will get things wrong; a fast edit-on-accept flow is what makes
   that acceptable rather than annoying. Tags found in the markup
   (`recipeCuisine`, `recipeCategory`) appear as **unselected** chips to click
   — NYT labels everything "Dinner"/"Lunch", and auto-importing that would
   fill the tag list with noise that makes filtering useless.
4. "Add from URL" box in the webapp, accepting a newline-separated list for
   **bulk import runs**.
5. Recipe list page: FTS5 search, filter chips by tag kind, prep-time slider,
   sort by name / recently added / least recently cooked.
6. Manual add/edit form — the same editable card as (3): repeating
   quantity / unit / ingredient rows with autocomplete against `ingredients`,
   plus a paste-a-block textarea routed through `parse.py`. This is the
   fallback for cookbooks and handwritten recipes, not the main path.
7. Tag management (create/rename/recolor, grouped by kind).

Delete = `status = 'archived'`, so past plans keep their reference.

**Done when**: a batch of gathered URLs has been run through import and
reviewed into ~10 real, searchable, correctly-parsed recipes.

### Phase 2 — Week planner ✅ built, not yet deployed

- Main page: Mon–Fri boxes for the current week (`plan_date` derived from
  today's ISO week), with prev/next week navigation.
- Per box: **Skip**, **Choose** (search modal, reuses phase 1's filter
  component), **Random**, plus servings override, lock toggle, and a free-text
  note.
- Random picking (`planner.py`): draw from the filtered pool, excluding
  recipes already placed in the visible week, weighted toward least-recently
  cooked via `last_planned_on`. If the filtered pool is empty, say so plainly
  rather than silently falling back to the unfiltered set.
- "Fill empty days" and "Reroll unlocked" buttons for the whole week.
- Setting a day to `planned` updates the recipe's `last_planned_on`.

**Done when**: a full week can be planned in under a minute.

### Phase 3 — Grocery list

A permanent, first-class feature — **not** a stopgap until Picnic. It is the
fallback for every way phase 6 can break (Picnic ships an app update, 2FA
fails, a product can't be matched), so it has to be good enough to use
indefinitely on its own.

- Tied to the **visible** week, so prev/next navigation shows that week's list —
  including past weeks, which doubles as a record of what was actually bought.
- Generate from the visible week: scale by servings, aggregate per the unit
  rules above, group by `aisle`, order aisles by how you actually walk a shop.
- Persisted per `week_start` so ticked-off boxes survive a reload; regenerating
  re-derives recipe-sourced lines but preserves `manual` items and `checked`
  state where the line is unchanged.
- Add manual items ("koffie", "wc-papier").
- "Copy as text" for phone use — this is the real interim answer until Picnic.
- Mobile layout matters here more than anywhere else; this page gets used
  one-handed in a supermarket.

**Done when**: one week's shopping is actually done off this list.

### Phase 4 — Hardening

- `data/dinner.db` added to the server's backup sweep (still an unconfirmed
  open item on `research_aggregator` — worth settling for both at once).
- Seed/export: a JSON dump-and-load script, so the recipe bank isn't hostage
  to one SQLite file.
- A basic smoke test over the aggregation math — unit conversion is the one
  place a silent bug is both likely and expensive.

### Phase 5 — Weekly discovery sweep (later, to be specified)

**Deliberately not designed yet** — to be worked out in more detail before
building. Sketch only, so phases 0–4 don't paint it into a corner:

- OpenRouter called directly (not via Hermes), same as `research_aggregator`
  settled on. `deepseek/deepseek-v4-flash` pinned via `OPENROUTER_MODEL`.
- `ingest/` module sweeps a configured list of trusted recipe sites, scores
  candidates for "would this household like it" against a stored preferences
  prompt, and posts survivors through the *existing* phase-1 ingest path — so
  it reuses `extract.py`, `parse.py`, `suggested` status and the review queue
  rather than adding a parallel one.
- Weekly crontab entry as `bart` (not Hermes — its cron scripts are sandboxed
  to `~/.hermes/scripts/` and it has no docker socket, which is exactly why
  `research_aggregator` runs from OS crontab):
  `docker compose run --rm dinner python -m ingest.sweep`.
- A `runs` table + header status line, since cron alerts on nothing.

Open for that phase: which sites, how preferences get expressed and edited,
how aggressively to filter, and whether "seen and rejected" should feed back
into scoring.

### Phase 6 — Picnic (later, and genuinely uncertain)

Picnic has no official public API. Every client below is reverse-engineered
from the mobile app's endpoints and can break whenever Picnic ships an update.

**Library choice** — evaluated Aug 2026:

| | |
|---|---|
| [MRVDH/picnic-api](https://github.com/MRVDH/picnic-api) | TypeScript. 104★, MIT, pushed 2026-07-02, actively maintained. Covers `catalog.search()`, `cart.addProductsToCart()`, `cart.getDeliverySlots()`, 2FA. Good library — but Node, so using it means a sidecar container purely for groceries |
| [python-picnic-api2](https://pypi.org/project/python-picnic-api2/) | **Preferred.** Python, Apache-2.0, v2.0.1 released 2026-08-05, pydantic models. Maintained because Home Assistant's Picnic integration depends on it — a downstream consumer with real users keeps it current against API drift. Needs Python 3.13+ (hence the base image choice in phase 0) |
| [MikeBrink/python-picnic-api](https://github.com/MikeBrink/python-picnic-api) | Avoid — the original, last pushed 2024-05. `api2` is the live fork |

**The library is not the hard part.** Product matching is: turning
`500 g rundergehakt` into a Picnic product id when search returns a dozen hits
at different pack sizes (Picnic sells 300 g, we need 500 g — two packs? a
different product?). That is a fuzzy-match plus confirmation-UI problem, and
it is bigger than the API integration itself. A learned mapping table
(`ingredient_id` → `picnic_product_id`, remembered after the first manual
confirmation) is what makes it tolerable over time.

**Scope: build the cart, do not place the order.** Push matched items into the
basket, then review and check out in the Picnic app. Ordering the wrong thing
unattended is a real cost, and it avoids automating a purchase behind SMS 2FA.

**Risks** to accept before starting: credentials live in `.env`; SMS 2FA
complicates unattended runs; unofficial use may sit awkwardly with Picnic's
terms. Failure at any point degrades to phase 3's copyable text list, which is
why that has to stay good on its own. Worth a spike — log in, search one
product, add it to a cart — before committing to the phase.

## Out of scope

- Weekends (Sat/Sun) — the brainstorm says Mon–Fri; adding two boxes later is
  a template change, not a schema one.
- Nutrition tracking, cost tracking, pantry/stock modelling ("we already have
  rice"). Pantry is the most tempting and the most complex — it needs an
  inventory that someone has to keep accurate, which nobody does.
- Multi-user accounts, sharing, public recipe pages.

## Open questions

1. **What form do the gathered recipes arrive in?** Assumed: mostly URLs from
   recipe sites, which makes JSON-LD extraction the main path and manual entry
   the fallback. If a meaningful share are cookbook photos or handwritten
   cards, that flips — phase 1 then needs an image upload path and a vision
   model call, which is a different piece of work.
2. **Aisle ordering** — needs your actual shop's layout to be useful; a
   generic order is a placeholder.
3. **Leftovers** — should a recipe planned on Monday be markable as "cooked
   double, Tuesday is leftovers"? Cheap to add as a day `state`, but only if
   you'd use it.
