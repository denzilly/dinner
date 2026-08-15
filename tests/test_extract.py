"""Extraction, offline.

Fixtures mirror shapes seen in the real demo bank: PT0H10M durations, yields as
ranges and as lists, @graph wrapping, @type as a list, HowToStep instructions.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import extract  # noqa: E402
from app.extract import FetchError  # noqa: E402


# --- durations ------------------------------------------------------------

@pytest.mark.parametrize(
    "value,minutes",
    [
        ("PT10M", 10),
        ("PT0H10M", 10),          # NYT emits this form on some recipes
        ("PT1H30M", 90),
        ("PT0H60M", 60),
        ("P1DT2H", 1560),
        (None, None),
        ("", None),
        ("later", None),
        ("PT0M", None),
    ],
)
def test_parse_duration_minutes(value, minutes):
    assert extract.parse_duration_minutes(value) == minutes


# --- yields ---------------------------------------------------------------

@pytest.mark.parametrize(
    "value,servings",
    [
        ("4 servings", 4),
        ("4", 4),
        (4, 4),
        ("10 to 14 servings", 10),       # lower bound -> scales to more food
        ("4 to 6 servings", 4),
        (["6 to 8 servings", "12 cups"], 6),
        (None, None),
        ("a bowlful", None),
    ],
)
def test_parse_servings(value, servings):
    assert extract.parse_servings(value)[0] == servings


def test_range_yield_warns():
    _, warnings = extract.parse_servings("4 to 6 servings")
    assert any("range" in w for w in warnings)


def test_list_yield_warns():
    _, warnings = extract.parse_servings(["6 to 8 servings", "12 cups"])
    assert any("several yields" in w for w in warnings)


# --- JSON-LD --------------------------------------------------------------

def _page(jsonld: str) -> str:
    return f'<html><head><script type="application/ld+json">{jsonld}</script></head><body></body></html>'


def test_plain_recipe():
    recipe = extract.from_html(_page("""
        {"@type":"Recipe","name":"Carbonara",
         "recipeIngredient":["200 g spaghetti","2 eggs"],
         "recipeInstructions":"Boil the pasta.",
         "recipeYield":"2 servings","prepTime":"PT5M","cookTime":"PT15M"}
    """), "https://example.com/carbonara")

    assert recipe.title == "Carbonara"
    assert recipe.ingredient_lines == ["200 g spaghetti", "2 eggs"]
    assert recipe.servings == 2
    assert recipe.prep_minutes == 5
    assert recipe.cook_minutes == 15
    assert recipe.extraction == "jsonld"


def test_graph_wrapped_recipe():
    recipe = extract.from_html(_page("""
        {"@context":"https://schema.org","@graph":[
          {"@type":"WebSite","name":"Some Site"},
          {"@type":["Recipe","NewsArticle"],"name":"Nested",
           "recipeIngredient":["1 onion"]}]}
    """), "https://example.com/x")
    assert recipe.title == "Nested"
    assert recipe.ingredient_lines == ["1 onion"]


def test_recipe_in_top_level_array():
    recipe = extract.from_html(_page("""
        [{"@type":"Organization","name":"Pub"},
         {"@type":"Recipe","name":"Arrayed","recipeIngredient":["2 eggs"]}]
    """), None)
    assert recipe.title == "Arrayed"


def test_howto_steps_and_sections():
    recipe = extract.from_html(_page("""
        {"@type":"Recipe","name":"Steps","recipeIngredient":["x"],
         "recipeInstructions":[
           {"@type":"HowToSection","itemListElement":[
             {"@type":"HowToStep","text":"Chop the onion."},
             {"@type":"HowToStep","text":"Fry it."}]},
           {"@type":"HowToStep","text":"Serve."}]}
    """), None)
    assert recipe.instructions == "Chop the onion.\n\nFry it.\n\nServe."


def test_html_stripped_from_instructions():
    recipe = extract.from_html(_page("""
        {"@type":"Recipe","name":"H","recipeIngredient":["x"],
         "recipeInstructions":"<p>Boil <b>water</b>.</p>"}
    """), None)
    assert recipe.instructions == "Boil water."


def test_publisher_becomes_source_name():
    recipe = extract.from_html(_page("""
        {"@type":"Recipe","name":"P","recipeIngredient":["x"],
         "publisher":{"@type":"Organization","name":"NYT Cooking"}}
    """), "https://cooking.nytimes.com/recipes/1")
    assert recipe.source_name == "NYT Cooking"


def test_hostname_fallback_for_source_name():
    recipe = extract.from_html(_page("""
        {"@type":"Recipe","name":"P","recipeIngredient":["x"]}
    """), "https://cooking.nytimes.com/recipes/1")
    assert recipe.source_name == "cooking.nytimes.com"


def test_cuisine_and_category_become_tags():
    recipe = extract.from_html(_page("""
        {"@type":"Recipe","name":"T","recipeIngredient":["x"],
         "recipeCuisine":"Italian","recipeCategory":["Dinner","Lunch"]}
    """), None)
    assert recipe.suggested_tags == ["Italian", "Dinner", "Lunch"]


def test_malformed_jsonld_block_is_skipped():
    html = (
        '<html><head>'
        '<script type="application/ld+json">{ not json </script>'
        '<script type="application/ld+json">'
        '{"@type":"Recipe","name":"Survivor","recipeIngredient":["1 egg"]}'
        '</script></head></html>'
    )
    assert extract.from_html(html, None).title == "Survivor"


def test_recipe_without_ingredients_is_not_accepted():
    # A stub Recipe node with no ingredients should not win over a later block.
    html = (
        '<html><head>'
        '<script type="application/ld+json">{"@type":"Recipe","name":"Stub"}</script>'
        '<script type="application/ld+json">'
        '{"@type":"Recipe","name":"Real","recipeIngredient":["1 egg"]}'
        '</script></head></html>'
    )
    assert extract.from_html(html, None).title == "Real"


def test_no_markup_raises():
    with pytest.raises(FetchError, match="No recipe markup"):
        extract.from_html("<html><body><h1>Just a page</h1></body></html>", None)


# --- microdata ------------------------------------------------------------

def test_microdata_fallback():
    html = """
    <div itemscope itemtype="http://schema.org/Recipe">
      <h1 itemprop="name">Microdata Stew</h1>
      <span itemprop="recipeYield">4 servings</span>
      <time itemprop="prepTime" datetime="PT20M">20 min</time>
      <li itemprop="recipeIngredient">500 g beef</li>
      <li itemprop="recipeIngredient">2 onions</li>
      <p itemprop="recipeInstructions">Simmer for an hour.</p>
    </div>
    """
    recipe = extract.from_html(html, "https://old.example/stew")
    assert recipe.extraction == "microdata"
    assert recipe.title == "Microdata Stew"
    assert recipe.ingredient_lines == ["500 g beef", "2 onions"]
    assert recipe.servings == 4
    assert recipe.prep_minutes == 20


# --- SSRF guard -----------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata service
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ],
)
def test_private_addresses_refused(url):
    with pytest.raises(FetchError, match="private network"):
        extract._validate_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/"])
def test_non_http_schemes_refused(url):
    with pytest.raises(FetchError, match="http and https"):
        extract._validate_url(url)


def test_public_url_accepted():
    assert extract._validate_url("https://cooking.nytimes.com/recipes/1").startswith("https://")
