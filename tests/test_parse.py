"""Ingredient line parsing.

Cases are taken from the real demo bank (NYT Cooking, Jamie Oliver) plus the
Dutch forms the grocery side has to handle.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.parse import canonical_name, parse_line  # noqa: E402


@pytest.mark.parametrize(
    "line,quantity,unit,name",
    [
        # --- straightforward metric / Dutch ---
        ("500 g rundergehakt", 500, "g", "rundergehakt"),
        ("1,5 kg aardappelen", 1.5, "kg", "aardappelen"),
        ("2 uien", 2, "piece", "uien"),
        ("3 tenen knoflook", 3, "clove", "knoflook"),
        ("1 blik tomatenblokjes", 1, "can", "tomatenblokjes"),
        ("2 el olijfolie", 2, "tbsp", "olijfolie"),
        ("1 tl komijn", 1, "tsp", "komijn"),
        # --- US customary, straight from NYT ---
        ("1 pint cherry or grape tomatoes", 1, "pint", "cherry or grape tomatoes"),
        ("2 cups orzo", 2, "cup", "orzo"),
        ("1 pound ground beef", 1, "lb", "ground beef"),
        ("4 ounces feta", 4, "oz", "feta"),
        ("2 tablespoons olive oil", 2, "tbsp", "olive oil"),
        # --- fractions ---
        ("½ cup olive oil", 0.5, "cup", "olive oil"),
        ("1½ cups pitted kalamata olives", 1.5, "cup", "pitted kalamata olives"),
        ("1/2 teaspoon salt", 0.5, "tsp", "salt"),
        ("1 1/2 cups farro", 1.5, "cup", "farro"),
        ("¼ cup mint", 0.25, "cup", "mint"),
    ],
)
def test_quantities(line, quantity, unit, name):
    parsed = parse_line(line)
    assert parsed.quantity == pytest.approx(quantity)
    assert parsed.unit == unit
    assert parsed.name == name


def test_range_takes_upper_bound_and_warns():
    parsed = parse_line("2 to 3 cloves garlic")
    assert parsed.quantity == 3
    assert parsed.unit == "clove"
    assert parsed.name == "garlic"
    assert any("range" in w for w in parsed.warnings)


def test_hyphen_range():
    parsed = parse_line("4-6 sprigs thyme")
    assert parsed.quantity == 6
    assert parsed.unit == "sprig"


def test_note_split_on_comma():
    parsed = parse_line("1 red onion, halved and cut into 1/4-inch slices")
    assert parsed.quantity == 1
    assert parsed.unit == "piece"
    assert parsed.name == "red onion"
    assert parsed.note == "halved and cut into 1/4-inch slices"


def test_parenthetical_becomes_note():
    parsed = parse_line("Kosher salt (such as Diamond Crystal)")
    assert parsed.name == "Kosher salt"
    assert parsed.note == "such as Diamond Crystal"
    assert parsed.quantity is None


def test_unquantified_line_warns_but_parses():
    parsed = parse_line("Salt and black pepper")
    assert parsed.name == "Salt and black pepper"
    assert parsed.quantity is None
    assert parsed.unit is None
    assert parsed.warnings


def test_to_taste_is_vague():
    parsed = parse_line("Black pepper, to taste")
    assert parsed.is_vague
    assert parsed.name == "Black pepper"


def test_pinch_is_vague():
    parsed = parse_line("snufje zout")
    assert parsed.is_vague
    assert parsed.name == "zout"


def test_optional_flagged_and_stripped():
    parsed = parse_line("2 tablespoons capers (optional)")
    assert parsed.optional
    assert parsed.quantity == 2
    assert parsed.unit == "tbsp"
    assert parsed.name == "capers"


def test_families_do_not_collide():
    assert parse_line("400 g tomaten").family == "mass"
    assert parse_line("1 blik tomaten").family == "count:can"
    assert parse_line("2 tenen knoflook").family == "count:clove"
    # A can and a clove must never be summed together.
    assert parse_line("1 blik x").family != parse_line("1 teen x").family


def test_multiword_unit():
    parsed = parse_line("8 fl oz cream")
    assert parsed.unit == "floz"
    assert parsed.name == "cream"


def test_empty_line():
    parsed = parse_line("   ")
    assert parsed.name == ""
    assert parsed.warnings


def test_fraction_in_a_note_stays_readable():
    """Vulgar fractions outside the amount must not turn into decimals."""
    parsed = parse_line("8 oz halloumi cheese, chopped into ½-inch pieces")
    assert parsed.quantity == 8
    assert parsed.unit == "oz"
    assert parsed.note == "chopped into 1/2-inch pieces"
    assert "0.5" not in parsed.note


def test_modifier_between_amount_and_unit():
    parsed = parse_line("2 packed cups basil leaves")
    assert parsed.quantity == 2
    assert parsed.unit == "cup"
    assert parsed.name == "basil leaves"
    assert parsed.note == "packed"


def test_modifier_before_vague_unit():
    parsed = parse_line("2 big handfuls arugula")
    assert parsed.quantity == 2
    assert parsed.unit == "handful"
    assert parsed.name == "arugula"


def test_modifier_without_a_unit_stays_in_the_name():
    """'1 large onion' has no unit -- 'large' describes the onion."""
    parsed = parse_line("1 large onion")
    assert parsed.quantity == 1
    assert parsed.unit == "piece"
    assert parsed.name == "large onion"


@pytest.mark.parametrize(
    "value,rendered",
    [
        (0.5, "½"), (1.5, "1½"), (1 / 3, "⅓"), (2 / 3, "⅔"),
        (0.25, "¼"), (2.25, "2¼"), (2.0, "2"), (500.0, "500"),
        (1.2, "1.2"), (None, ""),
    ],
)
def test_format_quantity(value, rendered):
    from app.parse import format_quantity
    assert format_quantity(value) == rendered


def test_quantity_display_round_trips():
    """What the edit form shows must parse back to the same number."""
    from app.parse import format_quantity
    for original in (0.5, 1.5, 1 / 3, 0.25, 2, 500):
        text = format_quantity(original)
        assert parse_line(f"{text} cup x").quantity == pytest.approx(original, abs=0.005)


def test_canonical_name_normalises_case_and_space():
    assert canonical_name("  Red   Onion ") == "red onion"
    # Descriptors are preserved deliberately -- merging is a human decision.
    assert canonical_name("Pitted Kalamata Olives") == "pitted kalamata olives"
