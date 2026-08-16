"""Pack arithmetic and the proposed-cart page.

Picnic is never called: `picnic.client` and `picnic.search` are replaced, the
same way test_ingest.py replaces `extract.from_url`. The arithmetic half needs
no stubbing at all, which is why it lives apart from the API half in the module.
"""
import pytest

from app import grocery, picnic, queries


# --- pack arithmetic ------------------------------------------------------

@pytest.mark.parametrize(
    "needed_g,pack_qty,expected_packs,rounded",
    [
        (500, 500, 1, False),      # exact fit
        (750, 500, 2, True),       # the case that has to be annotated
        (1000, 500, 2, False),     # exact fit across two packs
        (100, 500, 1, True),       # less than one pack still buys one
        (1500, 500, 3, False),
    ],
)
def test_plan_packs_mass(needed_g, pack_qty, expected_packs, rounded):
    plan = picnic.plan_packs({"mass": needed_g}, pack_qty, "g")
    assert plan.packs == expected_packs
    assert plan.rounded_up is rounded


def test_plan_packs_converts_units():
    """A 1 kg pack against a requirement aggregated in grams."""
    plan = picnic.plan_packs({"mass": 1500}, 1, "kg")
    assert plan.packs == 2
    assert plan.needed == pytest.approx(1.5)
    assert plan.covered == pytest.approx(2)


def test_plan_packs_counts():
    """5 onions against a 3-per-bag pack."""
    plan = picnic.plan_packs({"count:piece": 5}, 3, "piece")
    assert plan.packs == 2
    assert plan.rounded_up is True


def test_plan_packs_returns_none_when_family_absent():
    """A mapping in grams against a week that only asks for tins.

    Not zero packs -- a mapping that no longer fits the recipes, which the page
    flags rather than quietly buying nothing.
    """
    assert picnic.plan_packs({"count:can": 2}, 500, "g") is None


def test_plan_packs_rejects_nonsense_pack():
    assert picnic.plan_packs({"mass": 500}, 0, "g") is None
    assert picnic.plan_packs({"mass": 500}, 500, "furlong") is None


def test_summary_states_the_rounding():
    """Rounding up changes what you are charged, so it has to be legible."""
    summary = picnic.plan_packs({"mass": 750}, 500, "g").summary
    assert "1000" in summary and "750" in summary

    exact = picnic.plan_packs({"mass": 1000}, 500, "g").summary
    assert "need" not in exact


@pytest.mark.parametrize(
    "text,expected",
    [
        ("300 gram", (300.0, "g")),
        ("1 kilo", (1.0, "kg")),
        ("3 stuks", (3.0, "piece")),
        ("1 stuk", (1.0, "piece")),
        ("500 ml", (500.0, "ml")),
        ("750 ML", (750.0, "ml")),
        ("", None),
        (None, None),
        ("per stuk", None),        # no number
        ("2 sachets", None),       # unit the parser doesn't know
    ],
)
def test_parse_pack_quantity(text, expected):
    assert picnic.parse_pack_quantity(text) == expected


def test_price_per_unit_makes_pack_sizes_comparable():
    """The spike's actual numbers: the big pack is cheaper per kilo."""
    assert picnic.price_per_unit(425, "300 gram") == "EUR 14.17 / kg"
    assert picnic.price_per_unit(555, "500 gram") == "EUR 11.10 / kg"
    assert picnic.price_per_unit(1079, "1 kilo") == "EUR 10.79 / kg"


def test_price_per_unit_counts_per_item():
    assert picnic.price_per_unit(109, "3 stuks") == "EUR 0.36 / piece"


def test_price_per_unit_gives_up_quietly():
    assert picnic.price_per_unit(None, "300 gram") is None
    assert picnic.price_per_unit(425, "per stuk") is None


# --- the page -------------------------------------------------------------

HITS = [
    {"product_id": "s1001382", "name": "'t Slagershuys rundergehakt",
     "unit_text": "500 gram", "price_cents": 555, "price": "EUR 5.55",
     "per_unit": "EUR 11.10 / kg", "parsed": (500.0, "g")},
    {"product_id": "s1001363", "name": "'t Slagershuys rundergehakt",
     "unit_text": "300 gram", "price_cents": 425, "price": "EUR 4.25",
     "per_unit": "EUR 14.17 / kg", "parsed": (300.0, "g")},
]


@pytest.fixture
def stub_picnic(monkeypatch):
    """A client that never touches the network, and records what was added."""
    added = []

    class FakeAPI:
        def add_product(self, product_id, count=1):
            added.append((product_id, count))

    monkeypatch.setattr(picnic, "client", lambda: FakeAPI())
    monkeypatch.setattr(picnic, "search", lambda api, term: HITS)
    return added


@pytest.fixture
def planned_week(client, auth, app):
    """A week with one recipe planned, so the grocery list has something in it."""
    created = client.post("/api/recipes/ingest", headers=auth, json={
        "title": "Gehaktballen",
        "servings": 4,
        "ingredients": ["500 g rundergehakt", "2 uien", "zout"],
    }).get_json()
    client.post(f"/recipes/{created['id']}/accept",
                data={"title": "Gehaktballen", "servings": "4"})

    from app import weeks
    monday = weeks.current_monday()
    response = client.post(f"/plan/{monday.isoformat()}/set",
                           data={"recipe_id": created["id"]})
    # Guard the fixture itself: a silently unplanned week renders "nothing
    # planned" and every assertion below would fail for the wrong reason.
    assert response.status_code in (200, 302)
    with app.app_context():
        assert queries.week_ingredients(monday, monday), "fixture failed to plan a day"
    return monday


def test_page_lists_unmapped_ingredients_as_needing_a_choice(client, planned_week):
    page = client.get("/groceries/picnic").data
    assert b"Needs a choice" in page
    assert b"rundergehakt" in page
    # Nothing is auto-selected, however good the search looks.
    assert b"In the basket" not in page


def test_staples_are_listed_but_not_ticked(client, planned_week, app):
    page = client.get("/groceries/picnic").data.decode()
    assert "Staples" in page
    assert "zout" in page


def test_confirming_a_product_stores_the_mapping(client, planned_week, app, stub_picnic):
    with app.app_context():
        ingredient = next(row for row in queries.all_ingredients()
                          if row["name"] == "rundergehakt")

    client.post(f"/groceries/picnic/choose/{ingredient['id']}", data={
        "week": planned_week.isoformat(),
        "product_id": "s1001382",
        "product_name": "'t Slagershuys rundergehakt",
        "pack_covers_qty": "500",
        "pack_covers_unit": "g",
        "picnic_unit_text": "500 gram",
    })

    with app.app_context():
        mapping = queries.picnic_mappings([ingredient["id"]])[ingredient["id"]]
        assert mapping["decision"] == "mapped"
        assert mapping["product_id"] == "s1001382"

    page = client.get("/groceries/picnic").data
    assert b"In the basket" in page
    assert b"Needs a choice" not in page or b"uien" in page


def test_bad_pack_size_is_refused(client, planned_week, app, stub_picnic):
    """A wrong pack size is wrong every future week, not just once."""
    with app.app_context():
        ingredient = next(row for row in queries.all_ingredients()
                          if row["name"] == "rundergehakt")

    client.post(f"/groceries/picnic/choose/{ingredient['id']}", data={
        "week": planned_week.isoformat(),
        "product_id": "s1001382",
        "pack_covers_qty": "0",
        "pack_covers_unit": "g",
    })

    with app.app_context():
        assert queries.picnic_mappings([ingredient["id"]]) == {}


def test_never_is_remembered(client, planned_week, app):
    with app.app_context():
        ingredient = next(row for row in queries.all_ingredients()
                          if row["name"] == "rundergehakt")

    client.post(f"/groceries/picnic/choose/{ingredient['id']}",
                data={"week": planned_week.isoformat(), "action": "never"})

    page = client.get("/groceries/picnic").data
    assert b"Not via Picnic" in page

    with app.app_context():
        assert queries.picnic_mappings([ingredient["id"]])[ingredient["id"]]["decision"] == "never"


def test_push_adds_only_ticked_lines(client, planned_week, app, stub_picnic):
    with app.app_context():
        ingredient = next(row for row in queries.all_ingredients()
                          if row["name"] == "rundergehakt")

    client.post(f"/groceries/picnic/choose/{ingredient['id']}", data={
        "week": planned_week.isoformat(),
        "product_id": "s1001382",
        "product_name": "gehakt",
        "pack_covers_qty": "500",
        "pack_covers_unit": "g",
    })

    client.post("/groceries/picnic/push", data={
        "week": planned_week.isoformat(),
        "include": [str(ingredient["id"])],
    })

    # 500 g needed, 500 g pack -> exactly one.
    assert stub_picnic == [("s1001382", 1)]


def test_push_without_a_session_reports_rather_than_crashes(client, planned_week, app, monkeypatch):
    def unavailable():
        raise picnic.PicnicUnavailable("No Picnic session yet.")

    monkeypatch.setattr(picnic, "client", unavailable)

    with app.app_context():
        ingredient = next(row for row in queries.all_ingredients()
                          if row["name"] == "rundergehakt")

    response = client.post("/groceries/picnic/push", data={
        "week": planned_week.isoformat(),
        "include": [str(ingredient["id"])],
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b"No Picnic session" in response.data


def test_grocery_list_is_untouched_by_all_this(client, planned_week):
    """The simple list stays the simple list -- it just gains a link out."""
    page = client.get("/groceries").data
    assert b"Picnic basket" in page
    assert b"Needs a choice" not in page
    assert b"pack covers" not in page
