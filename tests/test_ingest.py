"""Ingest API and the review flow.

Network is never touched: extract.from_url is replaced with a stub, so these
stay fast and deterministic. Live extraction is covered by test_extract.py's
offline fixtures plus the demo-bank check.
"""
import pytest

from app import extract, queries


SAMPLE = extract.ExtractedRecipe(
    title="Tapenade Pasta Salad",
    source_url="https://cooking.nytimes.com/recipes/785259861-tapenade-pasta-salad",
    source_name="NYT Cooking",
    instructions="Boil the pasta.\n\nToss with tapenade.",
    servings=8,
    prep_minutes=10,
    cook_minutes=60,
    ingredient_lines=[
        "1½ cups pitted kalamata olives",
        "2 to 3 cloves garlic",
        "Kosher salt",
        "1 pound short pasta",
    ],
    suggested_tags=["Mediterranean Inspired", "Dinner"],
    extraction="jsonld",
)


@pytest.fixture
def stub_fetch(monkeypatch):
    def fake(url):
        recipe = extract.ExtractedRecipe(**{**SAMPLE.__dict__})
        recipe.source_url = url
        return recipe

    monkeypatch.setattr(extract, "from_url", fake)


# --- auth -----------------------------------------------------------------

def test_ingest_requires_token(client):
    response = client.post("/api/recipes/ingest", json={"url": "https://x.test/r"})
    assert response.status_code == 401


def test_ingest_rejects_wrong_token(client):
    response = client.post(
        "/api/recipes/ingest",
        json={"url": "https://x.test/r"},
        headers={"Authorization": "Bearer nope"},
    )
    assert response.status_code == 401


def test_ingest_bypasses_site_password(client, auth, stub_fetch, monkeypatch):
    """The gate must not turn API calls into HTML login pages."""
    import config

    monkeypatch.setattr(config, "SITE_PASSWORD", "sitepw")
    response = client.post("/api/recipes/ingest",
                           json={"url": "https://x.test/r"}, headers=auth)
    assert response.status_code == 201
    assert response.is_json


# --- ingest by URL --------------------------------------------------------

def test_ingest_url_creates_suggestion(client, auth, stub_fetch, app):
    response = client.post("/api/recipes/ingest",
                           json={"url": "https://x.test/tapenade"}, headers=auth)
    assert response.status_code == 201
    body = response.get_json()
    assert body["status"] == "suggested"
    assert body["ingredients"] == 4
    assert body["extraction"] == "jsonld"

    with app.app_context():
        recipe = queries.get_recipe(body["id"])
        assert recipe["status"] == "suggested"
        assert recipe["servings"] == 8

        ingredients = queries.ingredients_for_recipe(body["id"])
        by_name = {row["name"]: row for row in ingredients}
        assert by_name["pitted kalamata olives"]["quantity"] == pytest.approx(1.5)
        assert by_name["pitted kalamata olives"]["unit"] == "cup"
        assert by_name["garlic"]["quantity"] == 3           # upper bound of 2-3
        assert by_name["kosher salt"]["quantity"] is None   # staple, no amount


def test_reposting_same_url_updates_not_duplicates(client, auth, stub_fetch, app):
    first = client.post("/api/recipes/ingest",
                        json={"url": "https://x.test/same"}, headers=auth).get_json()
    second = client.post("/api/recipes/ingest",
                         json={"url": "https://x.test/same"}, headers=auth).get_json()
    assert first["id"] == second["id"]

    with app.app_context():
        assert queries.suggestion_count() == 1


def test_accepted_recipe_is_not_reimported(client, auth, stub_fetch, app):
    created = client.post("/api/recipes/ingest",
                          json={"url": "https://x.test/keep"}, headers=auth).get_json()
    client.post(f"/recipes/{created['id']}/accept", data={"title": "Keep"})

    again = client.post("/api/recipes/ingest",
                        json={"url": "https://x.test/keep"}, headers=auth)
    assert again.get_json()["status"] == "already_in_bank"


def test_fetch_failure_is_reported_not_500(client, auth, monkeypatch):
    def boom(url):
        raise extract.FetchError("The site's HTTPS certificate could not be verified.")

    monkeypatch.setattr(extract, "from_url", boom)
    response = client.post("/api/recipes/ingest",
                           json={"url": "https://bad.test/r"}, headers=auth)
    assert response.status_code == 422
    assert "certificate" in response.get_json()["error"]


# --- ingest by JSON body --------------------------------------------------

def test_ingest_loose_json(client, auth, app):
    response = client.post("/api/recipes/ingest", headers=auth, json={
        "title": "Handgeschreven stamppot",
        "ingredients": ["1,5 kg aardappelen", "500 g boerenkool", "1 rookworst"],
        "servings": 4,
    })
    assert response.status_code == 201
    body = response.get_json()
    assert body["extraction"] == "manual"

    with app.app_context():
        by_name = {r["name"]: r for r in queries.ingredients_for_recipe(body["id"])}
        assert by_name["aardappelen"]["quantity"] == pytest.approx(1.5)
        assert by_name["aardappelen"]["unit"] == "kg"
        assert by_name["rookworst"]["unit"] == "piece"


def test_ingest_requires_title_and_ingredients(client, auth):
    assert client.post("/api/recipes/ingest", headers=auth,
                       json={"ingredients": ["1 egg"]}).status_code == 422
    assert client.post("/api/recipes/ingest", headers=auth,
                       json={"title": "Empty"}).status_code == 422


# --- review flow ----------------------------------------------------------

def test_review_page_lists_suggestion_with_warnings(client, auth, stub_fetch):
    client.post("/api/recipes/ingest", json={"url": "https://x.test/r"}, headers=auth)
    page = client.get("/recipes/review")
    assert page.status_code == 200
    assert b"Tapenade Pasta Salad" in page.data
    assert b"to check" in page.data                 # warning banner
    assert b"Mediterranean Inspired" in page.data   # tag offered, not applied


def test_accept_moves_to_bank_and_applies_chosen_tags(client, auth, stub_fetch, app):
    created = client.post("/api/recipes/ingest",
                          json={"url": "https://x.test/r"}, headers=auth).get_json()

    client.post(f"/recipes/{created['id']}/accept", data={
        "title": "Tapenade Pasta Salad",
        "servings": "6",
        "new_tag": ["Mediterranean Inspired"],
        "tag_kind": "cuisine",
    })

    with app.app_context():
        recipe = queries.get_recipe(created["id"])
        assert recipe["status"] == "active"
        assert recipe["servings"] == 6
        tags = [t["name"] for t in queries.tags_for_recipe(created["id"])]
        assert tags == ["Mediterranean Inspired"]
        assert queries.suggestion_count() == 0


def test_unpicked_tags_are_not_created(client, auth, stub_fetch, app):
    created = client.post("/api/recipes/ingest",
                          json={"url": "https://x.test/r"}, headers=auth).get_json()
    client.post(f"/recipes/{created['id']}/accept", data={"title": "T"})

    with app.app_context():
        # "Dinner" was offered by the page but never ticked, so it must not exist.
        assert [t["name"] for t in queries.all_tags()] == []


def test_reject_removes_from_queue(client, auth, stub_fetch, app):
    created = client.post("/api/recipes/ingest",
                          json={"url": "https://x.test/r"}, headers=auth).get_json()
    client.post(f"/recipes/{created['id']}/reject")

    with app.app_context():
        assert queries.suggestion_count() == 0
        assert queries.get_recipe(created["id"])["status"] == "rejected"


# --- bank list ------------------------------------------------------------

def _add_active(client, auth, monkeypatch, title, lines, tags=(), prep=10, cook=20):
    def fake(url):
        return extract.ExtractedRecipe(
            title=title, source_url=url, ingredient_lines=list(lines),
            servings=4, prep_minutes=prep, cook_minutes=cook, extraction="jsonld",
        )

    monkeypatch.setattr(extract, "from_url", fake)
    created = client.post("/api/recipes/ingest",
                          json={"url": f"https://x.test/{title}"}, headers=auth).get_json()
    data = {"title": title}
    if tags:
        data["new_tag"] = list(tags)
        data["tag_kind"] = "cuisine"
    # follow_redirects drains the flash queue; otherwise "Added “Gone” to the
    # bank." renders on whatever page the next assertion looks at.
    client.post(f"/recipes/{created['id']}/accept", data=data, follow_redirects=True)
    return created["id"]


def test_search_and_filter(client, auth, monkeypatch, app):
    _add_active(client, auth, monkeypatch, "Carbonara", ["200 g spaghetti"],
                tags=["italiaans"], prep=5, cook=15)
    _add_active(client, auth, monkeypatch, "Boerenkool", ["1 kg boerenkool"],
                tags=["hollands"], prep=15, cook=45)

    assert b"Carbonara" in client.get("/recipes/?q=carbonara").data
    assert b"Boerenkool" not in client.get("/recipes/?q=carbonara").data

    # Time filter: carbonara is 20 min total, boerenkool 60.
    quick = client.get("/recipes/?max_minutes=30").data
    assert b"Carbonara" in quick and b"Boerenkool" not in quick


def test_search_handles_fts_operators_safely(client, auth, monkeypatch):
    _add_active(client, auth, monkeypatch, "Salt and pepper pasta", ["200 g pasta"])
    # Bare user text like this is not a valid FTS5 expression on its own.
    for query in ["salt & pepper", 'unbalanced "quote', "NEAR OR AND", "*"]:
        assert client.get(f"/recipes/?q={query}").status_code == 200


def test_archive_hides_from_bank(client, auth, monkeypatch):
    recipe_id = _add_active(client, auth, monkeypatch, "Gone", ["1 egg"])
    assert f"/recipes/{recipe_id}".encode() in client.get("/recipes/").data
    client.post(f"/recipes/{recipe_id}/archive", follow_redirects=True)
    assert f"/recipes/{recipe_id}".encode() not in client.get("/recipes/").data


# --- manual form ----------------------------------------------------------

def test_manual_form_round_trips_through_the_parser(client, app):
    response = client.post("/recipes/new", data={
        "title": "Handmade",
        "ingredients": "500 g gehakt\n2 uien, fijngesneden\n1 blik tomaten",
        "servings": "4",
        "instructions": "Bak alles.",
    }, follow_redirects=True)
    assert response.status_code == 200

    with app.app_context():
        recipe = queries.search_recipes(query="Handmade")[0]
        rows = queries.ingredients_for_recipe(recipe["id"])
        assert [(r["name"], r["quantity"], r["unit"]) for r in rows] == [
            ("gehakt", 500.0, "g"),
            ("uien", 2.0, "piece"),
            ("tomaten", 1.0, "can"),
        ]
        assert rows[1]["note"] == "fijngesneden"


def test_edit_preserves_ingredients_through_the_text_round_trip(client, app):
    client.post("/recipes/new", data={
        "title": "Round trip",
        "ingredients": "1,5 kg aardappelen\n2 tenen knoflook, geperst\n2 el olie",
        "servings": "4",
    }, follow_redirects=True)

    with app.app_context():
        recipe_id = queries.search_recipes(query="Round trip")[0]["id"]
        before = [(r["name"], r["quantity"], r["unit"], r["note"])
                  for r in queries.ingredients_for_recipe(recipe_id)]

    # Load the edit form and save it back unchanged.
    form = client.get(f"/recipes/{recipe_id}/edit")
    assert b"aardappelen" in form.data
    import re
    block = re.search(rb'name="ingredients"[^>]*>(.*?)</textarea>', form.data, re.S).group(1)
    client.post(f"/recipes/{recipe_id}/edit", data={
        "title": "Round trip",
        "ingredients": block.decode(),
        "servings": "4",
    }, follow_redirects=True)

    with app.app_context():
        after = [(r["name"], r["quantity"], r["unit"], r["note"])
                 for r in queries.ingredients_for_recipe(recipe_id)]
    assert before == after


def test_ingredient_rows_are_reused_not_duplicated(client, app):
    for title in ("A", "B"):
        client.post("/recipes/new", data={
            "title": title, "ingredients": "2 uien\n1 ui", "servings": "4",
        }, follow_redirects=True)

    with app.app_context():
        names = queries.ingredient_names()
    # One row, not two. The surviving name is whichever form was seen first --
    # the stem ("ui") is only a matching key, never a display name, because
    # stemming mangles words it wasn't designed for ("kaas" -> "kaa").
    assert names == ["uien"]


def test_singular_first_also_folds(client, app):
    """The same fold, with the import order reversed."""
    client.post("/recipes/new", data={
        "title": "C", "ingredients": "1 ui\n2 uien", "servings": "4",
    }, follow_redirects=True)

    with app.app_context():
        assert queries.ingredient_names() == ["ui"]


def test_words_ending_in_es_still_fold(client, app):
    """'cloves' strips to 'clov' under a first-match-wins stemmer, which never
    meets 'clove' -- the exact case that split garlic across two rows."""
    client.post("/recipes/new", data={
        "title": "D", "ingredients": "2 garlic cloves\n1 garlic clove", "servings": "4",
    }, follow_redirects=True)

    with app.app_context():
        assert queries.ingredient_names() == ["garlic cloves"]


def test_unrelated_ingredients_are_not_merged(client, app):
    client.post("/recipes/new", data={
        "title": "E", "ingredients": "1 ui\n1 ei\n200 g kaas\n2 appels", "servings": "4",
    }, follow_redirects=True)

    with app.app_context():
        assert sorted(queries.ingredient_names()) == ["appels", "ei", "kaas", "ui"]
