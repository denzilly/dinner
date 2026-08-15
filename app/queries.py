"""Every SQL statement in the app lives here, not in the route modules.

Routes stay about HTTP; this stays about data. It also means the one place to
look when a schema change lands is this file.
"""
import json
import re
import sqlite3
from datetime import date, datetime, timezone

from flask import g

import db
from app import parse


def get_db() -> sqlite3.Connection:
    """Per-request connection, closed by the teardown handler in __init__."""
    if "db" not in g:
        g.db = db.get_connection()
    return g.db


def close_db(_exception=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def plan_days_between(start: date, end: date) -> dict[str, sqlite3.Row]:
    """Planned days in [start, end], keyed by ISO date string.

    Days with no row simply aren't present -- the caller fills the gaps, so an
    untouched week needs no writes just to be viewed.
    """
    rows = get_db().execute(
        """SELECT p.plan_date, p.state, p.recipe_id, p.servings, p.locked, p.note,
                  r.title AS recipe_title, r.servings AS recipe_servings,
                  r.prep_minutes, r.cook_minutes
             FROM plan_days p
             LEFT JOIN recipes r ON r.id = p.recipe_id
            WHERE p.plan_date BETWEEN ? AND ?""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {row["plan_date"]: row for row in rows}


def recipe_count(status: str = "active") -> int:
    row = get_db().execute(
        "SELECT COUNT(*) AS n FROM recipes WHERE status = ?", (status,)
    ).fetchone()
    return row["n"]


# --------------------------------------------------------------------------
# Ingredients
# --------------------------------------------------------------------------

def upsert_ingredient(name: str) -> int:
    """Find or create the canonical ingredient row for `name`.

    Matching compares plural *stems* rather than trying singular variants of the
    incoming name only. That difference matters: importing "2 uien" before
    "1 ui" would otherwise create two rows, because the fallback could find an
    existing singular but never recognise that an existing row was the plural.
    Import order deciding whether the grocery list says "3 ui" or "2 uien + 1
    ui" is exactly the kind of quiet wrongness that erodes trust in the list.

    Stemming is a heuristic and stays conservative -- Dutch vowel changes
    ("tomaat"/"tomaten") are not caught, and descriptors ("pitted kalamata
    olives") are deliberately left alone. The review card is the backstop for
    both, since automatic merging destroys distinctions that sometimes matter.
    """
    conn = get_db()
    canonical = parse.canonical_name(name)
    if not canonical:
        raise ValueError("ingredient name cannot be empty")

    row = conn.execute("SELECT id FROM ingredients WHERE name = ?", (canonical,)).fetchone()
    if row:
        return row["id"]

    stem = _stem(canonical)
    for existing in conn.execute("SELECT id, name FROM ingredients"):
        if _stem(existing["name"]) == stem:
            return existing["id"]

    return conn.execute("INSERT INTO ingredients (name) VALUES (?)", (canonical,)).lastrowid


def _stem(name: str) -> str:
    """Strip a common plural ending so singular and plural share a key.

    Only applied to words long enough that the result is still meaningful, to
    avoid mangling short names ("ei" must not become "e").
    """
    if len(name) > 4 and name.endswith("ies"):
        return name[:-3] + "y"          # berries -> berry
    if len(name) > 4 and name.endswith("es"):
        return name[:-2]                # tomatoes -> tomato
    if len(name) > 3 and name.endswith("en"):
        return name[:-2]                # uien -> ui
    if len(name) > 3 and name.endswith("s"):
        return name[:-1]                # onions -> onion
    return name


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------

def upsert_tag(name: str, kind: str = "free") -> int:
    conn = get_db()
    name = " ".join(name.split())
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO tags (name, kind) VALUES (?, ?)", (name, kind)
    ).lastrowid


def all_tags() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT id, name, kind FROM tags ORDER BY kind, name COLLATE NOCASE"
    ).fetchall()


def set_recipe_tags(recipe_id: int, tag_ids) -> None:
    conn = get_db()
    conn.execute("DELETE FROM recipe_tags WHERE recipe_id = ?", (recipe_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO recipe_tags (recipe_id, tag_id) VALUES (?, ?)",
        [(recipe_id, tag_id) for tag_id in tag_ids],
    )


def tags_for_recipe(recipe_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        """SELECT t.id, t.name, t.kind
             FROM tags t
             JOIN recipe_tags rt ON rt.tag_id = t.id
            WHERE rt.recipe_id = ?
            ORDER BY t.kind, t.name COLLATE NOCASE""",
        (recipe_id,),
    ).fetchall()


# --------------------------------------------------------------------------
# Recipes
# --------------------------------------------------------------------------

def find_by_source_url(source_url: str) -> sqlite3.Row | None:
    """Live rows only. A previously rejected URL does not block a fresh import
    -- you may well change your mind about a recipe."""
    return get_db().execute(
        """SELECT id, title, status FROM recipes
            WHERE source_url = ? AND status IN ('active', 'suggested')""",
        (source_url,),
    ).fetchone()


def save_recipe(
    *,
    title: str,
    parsed_ingredients,
    instructions: str | None = None,
    source_url: str | None = None,
    source_name: str | None = None,
    servings: int | None = None,
    prep_minutes: int | None = None,
    cook_minutes: int | None = None,
    image_path: str | None = None,
    notes: str | None = None,
    status: str = "suggested",
    extraction: str = "manual",
    extraction_warnings=None,
    recipe_id: int | None = None,
) -> int:
    """Insert or replace a recipe and its ingredient rows in one transaction.

    Ingredient rows are rewritten wholesale rather than diffed: they are cheap,
    always derived from the parse, and a diff would be more code with more ways
    to leave the two out of step.
    """
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    warnings_json = json.dumps(extraction_warnings) if extraction_warnings else None

    if recipe_id is None:
        cursor = conn.execute(
            """INSERT INTO recipes
                   (title, source_url, source_name, instructions, servings,
                    prep_minutes, cook_minutes, image_path, notes, status,
                    extraction, extraction_warnings, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, source_url, source_name, instructions, servings, prep_minutes,
             cook_minutes, image_path, notes, status, extraction, warnings_json,
             now, now),
        )
        recipe_id = cursor.lastrowid
    else:
        conn.execute(
            """UPDATE recipes
                  SET title=?, source_url=?, source_name=?, instructions=?, servings=?,
                      prep_minutes=?, cook_minutes=?, image_path=?, notes=?, status=?,
                      extraction=?, extraction_warnings=?, updated_at=?
                WHERE id=?""",
            (title, source_url, source_name, instructions, servings, prep_minutes,
             cook_minutes, image_path, notes, status, extraction, warnings_json,
             now, recipe_id),
        )
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))

    for order, item in enumerate(parsed_ingredients):
        if not item.name:
            continue
        conn.execute(
            """INSERT INTO recipe_ingredients
                   (recipe_id, ingredient_id, quantity, unit, note, optional, sort_order)
               VALUES (?,?,?,?,?,?,?)""",
            (recipe_id, upsert_ingredient(item.name), item.quantity, item.unit,
             item.note, 1 if item.optional else 0, order),
        )

    conn.commit()
    return recipe_id


def get_recipe(recipe_id: int) -> sqlite3.Row | None:
    return get_db().execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()


def ingredients_for_recipe(recipe_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        """SELECT ri.id, ri.quantity, ri.unit, ri.note, ri.optional, i.name
             FROM recipe_ingredients ri
             JOIN ingredients i ON i.id = ri.ingredient_id
            WHERE ri.recipe_id = ?
            ORDER BY ri.sort_order""",
        (recipe_id,),
    ).fetchall()


def suggestions() -> list[sqlite3.Row]:
    return get_db().execute(
        """SELECT id, title, source_url, source_name, servings, prep_minutes,
                  cook_minutes, extraction, extraction_warnings, created_at
             FROM recipes
            WHERE status = 'suggested'
            ORDER BY created_at DESC, id DESC"""
    ).fetchall()


def suggestion_count() -> int:
    return get_db().execute(
        "SELECT COUNT(*) AS n FROM recipes WHERE status = 'suggested'"
    ).fetchone()["n"]


def _fts_query(text: str) -> str:
    """Turn user input into a safe FTS5 MATCH expression.

    Bare user text is not a valid FTS5 query -- an unbalanced quote or a stray
    NEAR/OR is a syntax error, i.e. a 500 on someone searching for "salt & pepper".
    Quoting each word defuses every operator and gives prefix matching.
    """
    words = [word for word in re.findall(r"\w+", text, re.UNICODE) if word]
    return " AND ".join(f'"{word}"*' for word in words)


def search_recipes(
    query: str = "",
    tag_ids=(),
    max_minutes: int | None = None,
    sort: str = "title",
    status: str = "active",
) -> list[sqlite3.Row]:
    conn = get_db()
    sql = ["SELECT r.id, r.title, r.servings, r.prep_minutes, r.cook_minutes,",
           "       r.source_name, r.last_planned_on",
           "  FROM recipes r"]
    where = ["r.status = ?"]
    params: list = [status]

    match = _fts_query(query)
    if match:
        sql.append("  JOIN recipes_fts f ON f.rowid = r.id")
        where.append("recipes_fts MATCH ?")
        params.append(match)

    tag_ids = [int(tag_id) for tag_id in tag_ids]
    if tag_ids:
        # AND across tags: picking "italian" and "vegetarian" should mean both,
        # otherwise adding a filter would widen the results rather than narrow them.
        placeholders = ",".join("?" * len(tag_ids))
        where.append(
            f"""r.id IN (SELECT recipe_id FROM recipe_tags
                          WHERE tag_id IN ({placeholders})
                          GROUP BY recipe_id HAVING COUNT(DISTINCT tag_id) = ?)"""
        )
        params.extend(tag_ids)
        params.append(len(tag_ids))

    if max_minutes:
        where.append("COALESCE(r.prep_minutes, 0) + COALESCE(r.cook_minutes, 0) <= ?")
        params.append(max_minutes)

    sql.append("WHERE " + " AND ".join(where))
    sql.append({
        "recent": "ORDER BY r.created_at DESC, r.id DESC",
        "unused": "ORDER BY r.last_planned_on IS NOT NULL, r.last_planned_on, r.title COLLATE NOCASE",
        "time": "ORDER BY COALESCE(r.prep_minutes,0) + COALESCE(r.cook_minutes,0), r.title COLLATE NOCASE",
    }.get(sort, "ORDER BY r.title COLLATE NOCASE"))

    return conn.execute("\n".join(sql), params).fetchall()


def tags_with_counts() -> list[sqlite3.Row]:
    return get_db().execute(
        """SELECT t.id, t.name, t.kind, COUNT(rt.recipe_id) AS uses
             FROM tags t
             LEFT JOIN recipe_tags rt ON rt.tag_id = t.id
             LEFT JOIN recipes r ON r.id = rt.recipe_id AND r.status = 'active'
            GROUP BY t.id
            ORDER BY t.kind, t.name COLLATE NOCASE"""
    ).fetchall()


def ingredient_names() -> list[str]:
    return [row["name"] for row in
            get_db().execute("SELECT name FROM ingredients ORDER BY name COLLATE NOCASE")]


def set_status(recipe_id: int, status: str) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE recipes SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.now(timezone.utc).isoformat(timespec="seconds"), recipe_id),
    )
    conn.commit()
