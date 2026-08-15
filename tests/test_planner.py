"""Week planner: day actions, filtered random picking, week-level fill/reroll."""
import random
from datetime import date, timedelta

import pytest

from app import extract, planner, queries, weeks


MONDAY = "2026-08-10"


def _bank(client, auth, monkeypatch, entries):
    """Put recipes straight into the bank. entries: [(title, minutes, [tags])]"""
    ids = {}
    for title, minutes, tags in entries:
        def fake(url, title=title, minutes=minutes):
            return extract.ExtractedRecipe(
                title=title, source_url=url, ingredient_lines=["1 egg"],
                servings=4, prep_minutes=0, cook_minutes=minutes, extraction="jsonld",
            )

        monkeypatch.setattr(extract, "from_url", fake)
        created = client.post("/api/recipes/ingest",
                              json={"url": f"https://x.test/{title}"}, headers=auth).get_json()
        data = {"title": title}
        if tags:
            data["new_tag"] = list(tags)
            data["tag_kind"] = "cuisine"
        client.post(f"/recipes/{created['id']}/accept", data=data, follow_redirects=True)
        ids[title] = created["id"]
    return ids


@pytest.fixture
def bank(client, auth, monkeypatch):
    return _bank(client, auth, monkeypatch, [
        ("Carbonara", 20, ["italiaans"]),
        ("Boerenkool", 60, ["hollands"]),
        ("Pad Thai", 30, ["thais"]),
        ("Risotto", 45, ["italiaans"]),
        ("Chili", 90, ["mexicaans"]),
        ("Omelet", 10, []),
    ])


# --- day actions ----------------------------------------------------------

def test_set_day_plans_a_recipe(client, bank, app):
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    with app.app_context():
        row = queries.plan_day(date.fromisoformat(MONDAY))
        assert row["state"] == "planned"
        assert row["recipe_id"] == bank["Carbonara"]


def test_planning_updates_last_planned_on(client, bank, app):
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    with app.app_context():
        assert queries.get_recipe(bank["Carbonara"])["last_planned_on"] == MONDAY


def test_clearing_a_day_recomputes_last_planned_on(client, bank, app):
    """Un-planning has to walk the value back, not leave it stale."""
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    client.post(f"/plan/{MONDAY}/clear", follow_redirects=True)
    with app.app_context():
        assert queries.get_recipe(bank["Carbonara"])["last_planned_on"] is None


def test_last_planned_on_tracks_the_latest_of_several(client, bank, app):
    later = (date.fromisoformat(MONDAY) + timedelta(days=2)).isoformat()
    client.post(f"/plan/{later}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    with app.app_context():
        assert queries.get_recipe(bank["Carbonara"])["last_planned_on"] == later


def test_skip_and_undo(client, bank, app):
    client.post(f"/plan/{MONDAY}/skip", follow_redirects=True)
    with app.app_context():
        assert queries.plan_day(date.fromisoformat(MONDAY))["state"] == "skip"
    client.post(f"/plan/{MONDAY}/clear", follow_redirects=True)
    with app.app_context():
        assert queries.plan_day(date.fromisoformat(MONDAY))["state"] == "empty"


def test_lock_toggles_and_survives_a_recipe_change(client, bank, app):
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    client.post(f"/plan/{MONDAY}/lock", follow_redirects=True)
    with app.app_context():
        assert queries.plan_day(date.fromisoformat(MONDAY))["locked"] == 1

    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Risotto"]},
                follow_redirects=True)
    with app.app_context():
        # Setting a recipe must not silently drop the lock.
        assert queries.plan_day(date.fromisoformat(MONDAY))["locked"] == 1


def test_servings_and_note(client, bank, app):
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    client.post(f"/plan/{MONDAY}/details", data={"servings": "6", "note": "schoonouders"},
                follow_redirects=True)
    with app.app_context():
        row = queries.plan_day(date.fromisoformat(MONDAY))
        assert row["servings"] == 6
        assert row["note"] == "schoonouders"
        assert row["state"] == "planned"     # details must not reset the day


def test_bad_date_404s(client):
    assert client.post("/plan/not-a-date/skip").status_code == 404
    assert client.get("/plan/2026-13-45/choose").status_code == 404


def test_set_unknown_recipe_404s(client, bank):
    assert client.post(f"/plan/{MONDAY}/set", data={"recipe_id": 9999}).status_code == 404


# --- random picking -------------------------------------------------------

def test_random_never_repeats_within_the_week(client, bank, app):
    for offset in range(5):
        day = (date.fromisoformat(MONDAY) + timedelta(days=offset)).isoformat()
        client.post(f"/plan/{day}/random", follow_redirects=True)

    with app.app_context():
        days = weeks.weekdays(date.fromisoformat(MONDAY))
        planned = queries.plan_days_between(days[0], days[-1])
        chosen = [row["recipe_id"] for row in planned.values()]
    assert len(chosen) == 5
    assert len(set(chosen)) == 5


def test_random_respects_filters(client, bank, app):
    with app.app_context():
        italian = next(t["id"] for t in queries.all_tags() if t["name"] == "italiaans")

    client.post(f"/plan/{MONDAY}/random", data={"tag": str(italian)}, follow_redirects=True)
    with app.app_context():
        row = queries.plan_day(date.fromisoformat(MONDAY))
        assert queries.get_recipe(row["recipe_id"])["title"] in {"Carbonara", "Risotto"}


def test_random_respects_time_filter(client, bank, app):
    client.post(f"/plan/{MONDAY}/random", data={"max_minutes": "20"}, follow_redirects=True)
    with app.app_context():
        row = queries.plan_day(date.fromisoformat(MONDAY))
        assert queries.get_recipe(row["recipe_id"])["title"] in {"Carbonara", "Omelet"}


def test_impossible_filter_says_so_rather_than_ignoring_it(client, bank, app):
    response = client.post(f"/plan/{MONDAY}/random", data={"max_minutes": "1"},
                           follow_redirects=True)
    assert b"Nothing matches those filters" in response.data
    with app.app_context():
        # Crucially, no recipe was planned in spite of the filter.
        assert queries.plan_day(date.fromisoformat(MONDAY)) is None


# --- pick weighting -------------------------------------------------------

def test_pick_prefers_the_stale_half():
    rows = [{"id": n, "title": str(n)} for n in range(20)]
    rng = random.Random(0)
    picks = {planner.pick(rows, rng)["id"] for _ in range(200)}
    # The pool is stalest-first, so only the first half is ever drawn from.
    assert max(picks) < 10


def test_pick_keeps_a_minimum_pool_on_a_small_bank():
    rows = [{"id": n} for n in range(4)]
    rng = random.Random(0)
    picks = {planner.pick(rows, rng)["id"] for _ in range(200)}
    # With 4 recipes, halving would make "random" nearly deterministic.
    assert picks == {0, 1, 2, 3}


def test_pick_on_empty_pool():
    assert planner.pick([]) is None


# --- week actions ---------------------------------------------------------

def test_fill_week_fills_only_empty_days(client, bank, app):
    tuesday = (date.fromisoformat(MONDAY) + timedelta(days=1)).isoformat()
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    client.post(f"/plan/{tuesday}/skip", follow_redirects=True)

    client.post(f"/plan/week/{MONDAY}/fill", follow_redirects=True)

    with app.app_context():
        days = weeks.weekdays(date.fromisoformat(MONDAY))
        planned = queries.plan_days_between(days[0], days[-1])
        assert planned[MONDAY]["recipe_id"] == bank["Carbonara"]   # untouched
        assert planned[tuesday]["state"] == "skip"                 # untouched
        assert sum(1 for r in planned.values() if r["state"] == "planned") == 4


def test_reroll_leaves_locked_days_alone(client, bank, app):
    tuesday = (date.fromisoformat(MONDAY) + timedelta(days=1)).isoformat()
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    client.post(f"/plan/{MONDAY}/lock", follow_redirects=True)
    client.post(f"/plan/{tuesday}/set", data={"recipe_id": bank["Risotto"]},
                follow_redirects=True)

    client.post(f"/plan/week/{MONDAY}/reroll", follow_redirects=True)

    with app.app_context():
        days = weeks.weekdays(date.fromisoformat(MONDAY))
        planned = queries.plan_days_between(days[0], days[-1])
        assert planned[MONDAY]["recipe_id"] == bank["Carbonara"]
        # The unlocked day was replaced, and never with the locked recipe.
        assert planned[tuesday]["recipe_id"] != bank["Carbonara"]


def test_reroll_with_everything_locked_says_so(client, bank):
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    client.post(f"/plan/{MONDAY}/lock", follow_redirects=True)
    response = client.post(f"/plan/week/{MONDAY}/reroll", follow_redirects=True)
    assert b"every planned day is locked" in response.data


# --- board and choose pages ----------------------------------------------

def test_board_shows_planned_recipe(client, bank):
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    page = client.get(f"/?week={MONDAY}")
    assert b"Carbonara" in page.data


def test_choose_page_marks_recipes_already_in_the_week(client, bank):
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": bank["Carbonara"]},
                follow_redirects=True)
    tuesday = (date.fromisoformat(MONDAY) + timedelta(days=1)).isoformat()
    page = client.get(f"/plan/{tuesday}/choose")
    assert b"already this week" in page.data


def test_choose_page_filters(client, bank, app):
    with app.app_context():
        italian = next(t["id"] for t in queries.all_tags() if t["name"] == "italiaans")
    page = client.get(f"/plan/{MONDAY}/choose?tag={italian}")
    assert b"Carbonara" in page.data and b"Boerenkool" not in page.data


def test_choose_carries_filters_into_surprise_me(client, bank, app):
    with app.app_context():
        italian = next(t["id"] for t in queries.all_tags() if t["name"] == "italiaans")
    page = client.get(f"/plan/{MONDAY}/choose?tag={italian}&max_minutes=30")
    assert b'name="tag" value="%d"' % italian in page.data
    assert b'name="max_minutes" value="30"' in page.data
