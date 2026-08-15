-- Initial schema: recipes, ingredients, tags, the week plan and grocery lists.
--
-- Written in full up front rather than grown per phase, because the shape is
-- already settled in project.md and an empty table costs nothing. Everything
-- uses IF NOT EXISTS so re-running a partially applied migration is safe.

CREATE TABLE IF NOT EXISTS recipes (
    id                  INTEGER PRIMARY KEY,
    title               TEXT NOT NULL,
    source_url          TEXT,
    source_name         TEXT,
    instructions        TEXT,
    servings            INTEGER NOT NULL DEFAULT 4,
    prep_minutes        INTEGER,
    cook_minutes        INTEGER,
    image_path          TEXT,
    notes               TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
                        -- active | suggested | rejected | archived
    extraction          TEXT,   -- jsonld | microdata | llm | manual
    extraction_warnings TEXT,   -- JSON array of ingredient lines to double check
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_planned_on     TEXT
);

CREATE INDEX IF NOT EXISTS idx_recipes_status ON recipes (status);
-- Suggestions are deduped on source_url; partial so archived/rejected copies
-- of the same URL don't block a fresh one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_recipes_source_url
    ON recipes (source_url)
    WHERE source_url IS NOT NULL AND status IN ('active', 'suggested');

CREATE TABLE IF NOT EXISTS ingredients (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    aisle        TEXT,
    default_unit TEXT
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id            INTEGER PRIMARY KEY,
    recipe_id     INTEGER NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients (id),
    quantity      REAL,
    unit          TEXT,
    note          TEXT,
    optional      INTEGER NOT NULL DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe
    ON recipe_ingredients (recipe_id, sort_order);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL   -- cuisine | diet | protein | effort | season | free
);

CREATE TABLE IF NOT EXISTS recipe_tags (
    recipe_id INTEGER NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (recipe_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_recipe_tags_tag ON recipe_tags (tag_id);

-- Keyed on the real date rather than week-number + weekday: "this week" is a
-- range query, past weeks stay queryable for free, and week navigation needs
-- no extra tables.
CREATE TABLE IF NOT EXISTS plan_days (
    plan_date TEXT PRIMARY KEY,             -- YYYY-MM-DD
    state     TEXT NOT NULL DEFAULT 'empty', -- empty | skip | planned
    recipe_id INTEGER REFERENCES recipes (id) ON DELETE SET NULL,
    servings  INTEGER,
    locked    INTEGER NOT NULL DEFAULT 0,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS grocery_lists (
    id           INTEGER PRIMARY KEY,
    week_start   TEXT NOT NULL UNIQUE,      -- the Monday
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grocery_items (
    id            INTEGER PRIMARY KEY,
    list_id       INTEGER NOT NULL REFERENCES grocery_lists (id) ON DELETE CASCADE,
    ingredient_id INTEGER REFERENCES ingredients (id),
    label         TEXT NOT NULL,
    quantity      REAL,
    unit          TEXT,
    checked       INTEGER NOT NULL DEFAULT 0,
    manual        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_grocery_items_list ON grocery_items (list_id);

-- Full-text search over the recipe bank. External-content table, kept in sync
-- by the triggers below.
CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5 (
    title,
    instructions,
    content='recipes',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS recipes_fts_ai AFTER INSERT ON recipes BEGIN
    INSERT INTO recipes_fts (rowid, title, instructions)
    VALUES (new.id, new.title, new.instructions);
END;

CREATE TRIGGER IF NOT EXISTS recipes_fts_ad AFTER DELETE ON recipes BEGIN
    INSERT INTO recipes_fts (recipes_fts, rowid, title, instructions)
    VALUES ('delete', old.id, old.title, old.instructions);
END;

CREATE TRIGGER IF NOT EXISTS recipes_fts_au AFTER UPDATE ON recipes BEGIN
    INSERT INTO recipes_fts (recipes_fts, rowid, title, instructions)
    VALUES ('delete', old.id, old.title, old.instructions);
    INSERT INTO recipes_fts (rowid, title, instructions)
    VALUES (new.id, new.title, new.instructions);
END;
