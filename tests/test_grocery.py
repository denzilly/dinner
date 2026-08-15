"""Grocery aggregation and the list page.

The aggregation tests are pure -- no database -- because the unit maths is the
part where a silent bug is both most likely and most expensive.
"""
from datetime import date, timedelta

import pytest

from app import extract, grocery, queries

MONDAY = "2026-08-10"


def entry(name, quantity, unit, scale=1.0, ingredient_id=None, aisle=None):
    return {
        "ingredient_id": ingredient_id if ingredient_id is not None else name,
        "name": name,
        "aisle": aisle,
        "quantity": quantity,
        "unit": unit,
        "scale": scale,
    }


def line_for(lines, name):
    return next(line for line in lines if line.name == name)


# --- aggregation within a family -----------------------------------------

def test_same_unit_sums():
    lines = grocery.build_lines([entry("gehakt", 500, "g"), entry("gehakt", 300, "g")])
    assert line_for(lines, "gehakt").amount == "800 g"


def test_grams_promote_to_kilos():
    lines = grocery.build_lines([entry("aardappelen", 800, "g"), entry("aardappelen", 700, "g")])
    assert line_for(lines, "aardappelen").amount == "1.5 kg"


def test_millilitres_promote_to_litres():
    lines = grocery.build_lines([entry("bouillon", 600, "ml"), entry("bouillon", 700, "ml")])
    assert line_for(lines, "bouillon").amount == "1.3 l"


def test_cups_stay_cups():
    """A volume of a solid is only meaningful in the unit it was written in --
    720 ml of orzo is not a shoppable quantity."""
    lines = grocery.build_lines([entry("orzo", 2, "cup"), entry("orzo", 1, "cup")])
    assert line_for(lines, "orzo").amount == "3 cups"


@pytest.mark.parametrize(
    "quantity,unit,expected",
    [
        (1, "cup", "1 cup"), (2, "cup", "2 cups"),
        (1, "can", "1 can"), (3, "can", "3 cans"),
        (1, "clove", "1 clove"), (4, "clove", "4 cloves"),
        (2, "tbsp", "2 tbsp"),          # abbreviations never pluralise
        (500, "g", "500 g"),
    ],
)
def test_units_pluralise_when_spelled_out(quantity, unit, expected):
    lines = grocery.build_lines([entry("x", quantity, unit)])
    assert line_for(lines, "x").amount == expected


def test_cups_do_not_promote_to_litres():
    """5 cups is 1200 ml, but "1.2 l of orzo" is not a thing you buy."""
    assert line_for(grocery.build_lines([entry("water", 5, "cup")]), "water").amount == "5 cups"


def test_pounds_always_render_metric():
    """A US weight in a Dutch shop is only useful once converted.

    Rounded to whole grams: 453.592 is exact but claims a precision nobody
    shops to, and it makes the list look machine-generated.
    """
    lines = grocery.build_lines([entry("beef", 1, "lb")])
    assert line_for(lines, "beef").amount == "454 g"


def test_mixed_mass_units_render_metric_whichever_came_first():
    lines = grocery.build_lines([entry("beef", 1, "lb"), entry("beef", 46.408, "g")])
    assert line_for(lines, "beef").amount == "500 g"


def test_weights_use_decimals_not_fractions():
    """1½ kg reads like a recipe; 1.5 kg reads like a scale."""
    lines = grocery.build_lines([entry("aardappelen", 1500, "g")])
    assert line_for(lines, "aardappelen").amount == "1.5 kg"


def test_tablespoons_and_cups_share_a_family():
    """Both are volume, so they combine -- rendered in the commoner unit."""
    lines = grocery.build_lines([
        entry("olive oil", 1, "cup"),
        entry("olive oil", 2, "tbsp"),
        entry("olive oil", 2, "tbsp"),
    ])
    # tbsp appears twice, cup once: 240 + 30 + 30 = 300 ml -> 20 tbsp
    assert line_for(lines, "olive oil").amount == "20 tbsp"


def test_metric_volume_stays_metric():
    lines = grocery.build_lines([entry("melk", 250, "ml"), entry("melk", 250, "ml")])
    assert line_for(lines, "melk").amount == "500 ml"


def test_fractions_render_as_fractions():
    lines = grocery.build_lines([entry("mint", 0.25, "cup"), entry("mint", 0.25, "cup")])
    assert line_for(lines, "mint").amount == "½ cup"


# --- families that must not merge ----------------------------------------

def test_cans_and_grams_are_kept_separate():
    lines = grocery.build_lines([entry("tomaten", 1, "can"), entry("tomaten", 400, "g")])
    line = line_for(lines, "tomaten")
    assert line.split
    assert "400 g" in line.amount and "1 blik" not in line.amount
    assert line.amount.count("+") == 1


def test_cloves_and_pieces_do_not_merge():
    lines = grocery.build_lines([entry("knoflook", 2, "clove"), entry("knoflook", 1, "piece")])
    assert line_for(lines, "knoflook").split


def test_count_units_name_themselves():
    lines = grocery.build_lines([entry("knoflook", 3, "clove")])
    assert line_for(lines, "knoflook").amount == "3 cloves"


def test_bare_pieces_have_no_unit_word():
    lines = grocery.build_lines([entry("uien", 2, "piece"), entry("uien", 1, "piece")])
    assert line_for(lines, "uien").amount == "3"


# --- staples --------------------------------------------------------------

def test_unquantified_becomes_a_staple():
    lines = grocery.build_lines([entry("kosher salt", None, None)])
    assert line_for(lines, "kosher salt").staple
    assert line_for(lines, "kosher salt").amount == ""


def test_vague_units_are_staples():
    lines = grocery.build_lines([entry("zout", 1, "pinch")])
    assert line_for(lines, "zout").staple


def test_an_ingredient_with_a_real_amount_anywhere_is_not_a_staple():
    """Salt with no amount in one recipe and 5 g in another is still shoppable."""
    lines = grocery.build_lines([entry("zout", None, None), entry("zout", 5, "g")])
    line = line_for(lines, "zout")
    assert not line.staple
    assert line.amount == "5 g"


def test_staples_excluded_from_aisle_groups():
    lines = grocery.build_lines([entry("salt", None, None), entry("ui", 2, "piece")])
    grouped = grocery.group_by_aisle(lines)
    names = [line.name for _, group in grouped for line in group]
    assert names == ["ui"]


# --- scaling --------------------------------------------------------------

def test_servings_override_scales_quantities():
    lines = grocery.build_lines([entry("gehakt", 500, "g", scale=1.5)])
    assert line_for(lines, "gehakt").amount == "750 g"


@pytest.mark.parametrize(
    "recipe_servings,target,expected",
    [(4, 8, 2.0), (4, 2, 0.5), (4, 4, 1.0), (4, None, 1.0), (None, 4, 1.0), (0, 4, 1.0)],
)
def test_scale_factor(recipe_servings, target, expected):
    assert grocery.scale_factor(recipe_servings, target) == expected


# --- aisles ---------------------------------------------------------------

def test_grouped_in_shop_order():
    lines = grocery.build_lines([
        entry("melk", 1, "l", aisle="zuivel"),
        entry("appel", 3, "piece", aisle="groente & fruit"),
        entry("iets", 1, "piece", aisle=None),
    ])
    assert [aisle for aisle, _ in grocery.group_by_aisle(lines)] == [
        "groente & fruit", "zuivel", "overig",
    ]


def test_text_version_has_aisles_and_staples():
    lines = grocery.build_lines([
        entry("melk", 1, "l", aisle="zuivel"),
        entry("salt", None, None),
    ])
    text = grocery.as_text(grocery.group_by_aisle(lines),
                           [l for l in lines if l.staple], [])
    assert "ZUIVEL" in text and "1 l melk" in text
    assert "CHECK THE CUPBOARD" in text and "salt" in text


# --- the page ------------------------------------------------------------

@pytest.fixture
def planned_week(client, auth, monkeypatch):
    def fake(url):
        return extract.ExtractedRecipe(
            title="Pasta", source_url=url, servings=4,
            ingredient_lines=["500 g gehakt", "2 uien", "1 blik tomaten", "Kosher salt"],
            extraction="jsonld",
        )

    monkeypatch.setattr(extract, "from_url", fake)
    created = client.post("/api/recipes/ingest",
                          json={"url": "https://x.test/pasta"}, headers=auth).get_json()
    client.post(f"/recipes/{created['id']}/accept", data={"title": "Pasta"},
                follow_redirects=True)
    client.post(f"/plan/{MONDAY}/set", data={"recipe_id": created["id"]},
                follow_redirects=True)
    return created["id"]


def test_page_lists_the_weeks_ingredients(client, planned_week):
    page = client.get(f"/groceries?week={MONDAY}")
    assert page.status_code == 200
    assert b"500 g" in page.data and b"gehakt" in page.data
    assert b"check the cupboard" in page.data      # kosher salt landed there


def test_empty_week_says_so(client):
    page = client.get(f"/groceries?week={MONDAY}")
    assert b"Nothing planned for this week" in page.data


def test_generate_then_tick_survives_reload(client, planned_week, app):
    client.post(f"/groceries/{MONDAY}/generate", follow_redirects=True)

    with app.app_context():
        grocery_list = queries.get_or_create_list(date.fromisoformat(MONDAY))
        items = queries.grocery_items(grocery_list["id"], manual=False)
        target = next(row for row in items if "gehakt" in row["label"])

    client.post(f"/groceries/item/{target['id']}/toggle", follow_redirects=True)

    with app.app_context():
        again = queries.grocery_items(grocery_list["id"], manual=False)
        assert next(r for r in again if r["id"] == target["id"])["checked"] == 1


def test_regenerating_keeps_ticks_on_unchanged_lines(client, planned_week, app):
    client.post(f"/groceries/{MONDAY}/generate", follow_redirects=True)
    with app.app_context():
        list_id = queries.get_or_create_list(date.fromisoformat(MONDAY))["id"]
        item = next(r for r in queries.grocery_items(list_id, manual=False)
                    if "gehakt" in r["label"])
    client.post(f"/groceries/item/{item['id']}/toggle", follow_redirects=True)

    client.post(f"/groceries/{MONDAY}/generate", follow_redirects=True)

    with app.app_context():
        rebuilt = next(r for r in queries.grocery_items(list_id, manual=False)
                       if "gehakt" in r["label"])
        assert rebuilt["checked"] == 1


def test_changed_amount_clears_the_tick(client, planned_week, app):
    """Doubling the servings means you genuinely need to buy more."""
    client.post(f"/groceries/{MONDAY}/generate", follow_redirects=True)
    with app.app_context():
        list_id = queries.get_or_create_list(date.fromisoformat(MONDAY))["id"]
        item = next(r for r in queries.grocery_items(list_id, manual=False)
                    if "gehakt" in r["label"])
    client.post(f"/groceries/item/{item['id']}/toggle", follow_redirects=True)

    client.post(f"/plan/{MONDAY}/details", data={"servings": "8"}, follow_redirects=True)
    client.post(f"/groceries/{MONDAY}/generate", follow_redirects=True)

    with app.app_context():
        rebuilt = [r for r in queries.grocery_items(list_id, manual=False)
                   if "gehakt" in r["label"]]
        assert rebuilt[0]["label"].startswith("1 kg")   # 500 g scaled by 2
        assert rebuilt[0]["checked"] == 0


def test_manual_items_survive_regeneration(client, planned_week, app):
    client.post(f"/groceries/{MONDAY}/add", data={"label": "koffie"}, follow_redirects=True)
    client.post(f"/groceries/{MONDAY}/generate", follow_redirects=True)

    with app.app_context():
        list_id = queries.get_or_create_list(date.fromisoformat(MONDAY))["id"]
        manual = queries.grocery_items(list_id, manual=True)
    assert [row["label"] for row in manual] == ["koffie"]


def test_manual_item_can_be_deleted(client, planned_week, app):
    client.post(f"/groceries/{MONDAY}/add", data={"label": "koffie"}, follow_redirects=True)
    with app.app_context():
        list_id = queries.get_or_create_list(date.fromisoformat(MONDAY))["id"]
        item = queries.grocery_items(list_id, manual=True)[0]
    client.post(f"/groceries/item/{item['id']}/delete", follow_redirects=True)
    with app.app_context():
        assert queries.grocery_items(list_id, manual=True) == []


def test_skipped_days_contribute_nothing(client, planned_week, app):
    tuesday = (date.fromisoformat(MONDAY) + timedelta(days=1)).isoformat()
    client.post(f"/plan/{tuesday}/skip", follow_redirects=True)
    with app.app_context():
        days = [date.fromisoformat(MONDAY), date.fromisoformat(MONDAY) + timedelta(days=4)]
        entries = queries.week_ingredients(days[0], days[1])
    # Only Monday's recipe contributes its four lines.
    assert len(entries) == 4


def test_aisle_editor_assigns_and_regroups(client, planned_week, app):
    with app.app_context():
        ingredient = next(row for row in queries.all_ingredients() if row["name"] == "uien")

    client.post("/ingredients", data={f"aisle-{ingredient['id']}": "groente & fruit"},
                follow_redirects=True)

    page = client.get(f"/groceries?week={MONDAY}")
    assert b"groente &amp; fruit" in page.data
