"""The recipe list's search/filter bar.

Covers the parts that are easy to break silently: the active-filter chips have
to remove exactly one filter and carry the rest through, and the tag bank has
to stay reachable from the Filter dropdown.
"""
import html as html_lib
import re

import pytest

from app import parse, queries


@pytest.fixture
def bank(app):
    """Two recipes, two tags: Pesto has both tags, Ragu has only Pasta."""
    with app.app_context():
        queries.save_recipe(title="Pesto Pasta", servings=4, prep_minutes=20,
                            parsed_ingredients=parse.parse_lines(["200 g pasta"]),
                            status="active")
        queries.save_recipe(title="Slow Ragu", servings=4, prep_minutes=180,
                            parsed_ingredients=parse.parse_lines(["500 g gehakt"]),
                            status="active")
        pasta = queries.upsert_tag("Pasta", "free")
        veggie = queries.upsert_tag("Vegetarian", "diet")
        queries.set_recipe_tags(1, [pasta, veggie])
        queries.set_recipe_tags(2, [pasta])
        queries.get_db().commit()
        return {"pasta": pasta, "vegetarian": veggie}


def chips(markup: str) -> dict[str, str]:
    """Map each active chip's label to the URL that removes it."""
    found = re.finditer(
        r'class="chip chip--active"\s+href="([^"]+)">\s*(.*?)<svg', markup, re.S
    )
    return {
        " ".join(match.group(2).split()).strip("\u201c\u201d"): html_lib.unescape(match.group(1))
        for match in found
    }


def test_no_filters_means_no_active_row(client, bank):
    markup = client.get("/recipes/").get_data(as_text=True)
    assert "filters__active" not in markup


def test_every_active_filter_gets_a_chip(client, bank):
    markup = client.get(
        f"/recipes/?q=pasta&max_minutes=30&tag={bank['pasta']}&tag={bank['vegetarian']}"
    ).get_data(as_text=True)

    assert set(chips(markup)) == {"pasta", "under 30 min", "Pasta", "Vegetarian"}


def test_removing_a_tag_chip_keeps_the_other_filters(client, bank):
    markup = client.get(
        f"/recipes/?q=pasta&max_minutes=30&tag={bank['pasta']}&tag={bank['vegetarian']}"
    ).get_data(as_text=True)

    after = client.get(chips(markup)["Vegetarian"]).get_data(as_text=True)

    assert set(chips(after)) == {"pasta", "under 30 min", "Pasta"}
    assert 'value="pasta"' in after      # search box still filled
    assert 'value="30"' in after         # time limit still set


def test_removing_the_search_chip_keeps_the_tags(client, bank):
    markup = client.get(
        f"/recipes/?q=pasta&tag={bank['pasta']}"
    ).get_data(as_text=True)

    after = client.get(chips(markup)["pasta"]).get_data(as_text=True)
    assert set(chips(after)) == {"Pasta"}


def test_clear_all_drops_everything(client, bank):
    markup = client.get(
        f"/recipes/?q=pasta&max_minutes=30&tag={bank['pasta']}"
    ).get_data(as_text=True)
    assert 'href="/recipes/"' in markup

    after = client.get("/recipes/").get_data(as_text=True)
    assert "filters__active" not in after


def test_the_filter_dropdown_carries_the_tag_bank(client, bank):
    markup = client.get("/recipes/").get_data(as_text=True)
    assert markup.count('name="tag"') == 2
    assert "filter-menu__panel" in markup


def test_the_filter_button_counts_what_is_applied(client, bank):
    markup = client.get(
        f"/recipes/?max_minutes=30&tag={bank['pasta']}&tag={bank['vegetarian']}"
    ).get_data(as_text=True)
    assert re.search(r'filter-menu__count">(\d+)', markup).group(1) == "3"


def test_filters_actually_narrow_the_results(client, bank):
    both = client.get("/recipes/").get_data(as_text=True)
    assert "2 recipes found" in both

    quick = client.get("/recipes/?max_minutes=30").get_data(as_text=True)
    assert "1 recipe found" in quick
    assert "Slow Ragu" not in quick

    veggie = client.get(f"/recipes/?tag={bank['vegetarian']}").get_data(as_text=True)
    assert "1 recipe found" in veggie
    assert "Pesto Pasta" in veggie
