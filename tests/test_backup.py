"""Round-tripping the whole database through dump/load.

The risk here isn't the JSON serialising -- it's the reload silently losing
or misattaching something (a tag on the wrong recipe, a checked box reset,
search going dead because recipes_fts didn't come back in sync). So the test
seeds one of everything through the real app/query layer, dumps, reloads into
a *different* empty database, and checks the copy is indistinguishable from
the original rather than just checking row counts.
"""
from datetime import date

import pytest

import backup
import config
import db as db_module
from app import queries

MONDAY = "2026-08-10"


@pytest.fixture
def seeded(app, client, auth):
    """One recipe with a tag, planned on a day, with a generated grocery list
    that has a manual item and a ticked box -- one of everything load() touches.
    """
    with app.app_context():
        created = client.post(
            "/api/recipes/ingest",
            json={
                "title": "Pasta",
                "source_url": "https://x.test/pasta",
                "servings": 4,
                "ingredients": ["500 g gehakt", "2 uien", "Kosher salt"],
            },
            headers=auth,
        ).get_json()
        recipe_id = created["id"]
        queries.set_status(recipe_id, "active")

        tag_id = queries.upsert_tag("weeknight", kind="effort")
        queries.set_recipe_tags(recipe_id, [tag_id])
        queries.get_db().commit()

        queries.set_plan_day(date.fromisoformat(MONDAY), state="planned", recipe_id=recipe_id)

        entries = queries.week_ingredients(date.fromisoformat(MONDAY), date.fromisoformat(MONDAY))
        from app import grocery
        list_id = queries.get_or_create_list(date.fromisoformat(MONDAY))["id"]
        queries.replace_generated_items(list_id, grocery.build_lines(entries))
        queries.add_manual_item(list_id, "koffie")

        checked = next(r for r in queries.grocery_items(list_id, manual=False)
                       if "gehakt" in r["label"])
        queries.toggle_item(checked["id"])

    return recipe_id, list_id


def test_round_trip_recreates_full_state(app, seeded, tmp_path, monkeypatch):
    recipe_id, list_id = seeded

    conn = db_module.get_connection()
    data = backup.dump(conn)
    conn.close()

    # A second, independent database -- a real restore never lands on the
    # same file it was dumped from.
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "restored.db"))
    db_module.migrate(verbose=False)
    restored = db_module.get_connection()
    counts = backup.load(restored, data)

    assert counts["recipes"] == 1
    # gehakt, uien, kosher salt (a staple, still gets its own row) + the manual "koffie".
    assert counts["grocery_items"] == 4

    recipe = restored.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    assert recipe["title"] == "Pasta"
    assert recipe["status"] == "active"

    tags = restored.execute(
        """SELECT t.name FROM tags t JOIN recipe_tags rt ON rt.tag_id = t.id
            WHERE rt.recipe_id = ?""",
        (recipe_id,),
    ).fetchall()
    assert [row["name"] for row in tags] == ["weeknight"]

    plan = restored.execute(
        "SELECT recipe_id, state FROM plan_days WHERE plan_date = ?", (MONDAY,)
    ).fetchone()
    assert plan["recipe_id"] == recipe_id and plan["state"] == "planned"

    items = {
        row["label"]: row["checked"]
        for row in restored.execute(
            "SELECT label, checked FROM grocery_items WHERE list_id = ?", (list_id,)
        )
    }
    assert items["koffie"] == 0
    assert any(label.endswith("gehakt") and checked == 1 for label, checked in items.items())

    # recipes_fts is rebuilt via the AFTER INSERT trigger, not copied directly
    # -- prove search actually still works rather than just that the table exists.
    hit = restored.execute(
        "SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'Pasta'"
    ).fetchall()
    assert [row["rowid"] for row in hit] == [recipe_id]

    restored.close()


def test_load_rejects_unknown_format_version():
    with pytest.raises(ValueError, match="format_version"):
        backup.load(None, {"format_version": 999, "tables": {}})


def test_load_rejects_unknown_table():
    with pytest.raises(ValueError, match="unknown table"):
        backup.load(None, {"format_version": backup.FORMAT_VERSION,
                           "tables": {"not_a_real_table": []}})


def test_dump_is_json_serialisable(app, seeded):
    import json
    conn = db_module.get_connection()
    data = backup.dump(conn)
    conn.close()
    json.dumps(data)  # must not raise
    assert data["format_version"] == backup.FORMAT_VERSION
    assert len(data["tables"]["recipes"]) == 1
