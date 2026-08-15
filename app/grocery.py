"""Turning a planned week into a shopping list.

Two rules govern everything here, both aimed at the same failure: a list that
is quietly wrong is worse than a list that admits what it doesn't know.

1. **Aggregate only within a unit family.** Grams and pounds combine exactly.
   Grams and cans do not, so "1 blik + 400 g tomaten" is printed as two
   segments on one line rather than invented into a single number.
2. **Render in the units the recipes used.** 2 cups + 1 cup of orzo is 3 cups,
   not 720 ml -- a volume of a solid is only meaningful in the unit it was
   written in. Converting it to grams would need a per-ingredient density
   table, and guessing one is exactly the fabrication rule 1 exists to stop.

Staples (salt, pepper, "to taste") carry no amount at all. They are collected
into their own section rather than dropped: "you already have salt" is true
until the day it isn't.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.parse import UNITS, format_quantity

# Order families appear in a line. Mass and volume first because they are the
# amounts you actually weigh out; counts read as "and also 2 cans".
FAMILY_ORDER = ["mass", "volume"]

# Aisle order is a placeholder until it can be matched to the real shop layout;
# it is at least the order most Dutch supermarkets roughly follow.
AISLE_ORDER = [
    "groente & fruit", "brood", "zuivel", "vlees & vis", "kaas",
    "kruiden", "pasta & rijst", "conserven", "diepvries", "dranken", "overig",
]
DEFAULT_AISLE = "overig"


@dataclass
class Line:
    ingredient_id: int | None
    name: str
    aisle: str
    segments: list[str] = field(default_factory=list)
    staple: bool = False
    split: bool = False        # more than one family -> could not be combined

    @property
    def amount(self) -> str:
        return " + ".join(self.segments)

    @property
    def label(self) -> str:
        return f"{self.amount} {self.name}".strip() if self.segments else self.name


def scale_factor(recipe_servings: int | None, target_servings: int | None) -> float:
    """How much to multiply a recipe's quantities by for this day."""
    if not recipe_servings or not target_servings:
        return 1.0
    return target_servings / recipe_servings


# Units that measure by eye in a kitchen rather than on a scale. A cup of orzo
# is only meaningful as a cup, so these are never rewritten as metric -- but
# anything measured by weight always is, because the shop is metric.
COOKING_MEASURES = {"cup", "tbsp", "tsp", "pint", "quart", "floz"}


# Spelled-out units pluralise; abbreviations (g, kg, tbsp, oz) never do.
PLURALS = {
    "cup": "cups", "can": "cans", "jar": "jars", "clove": "cloves",
    "piece": "pieces", "package": "packages", "bunch": "bunches",
    "sprig": "sprigs", "stalk": "stalks", "head": "heads", "slice": "slices",
    "pint": "pints", "quart": "quarts",
}


def _pluralise(unit: str, value: float) -> str:
    """Plural above one only -- "½ cups" is wrong, "½ cup" is what a recipe says."""
    return PLURALS.get(unit, unit) if value > 1.001 else unit


def _decimal(value: float, whole: bool = False) -> str:
    """Weights read better as decimals: 1.1 kg, not 1⅒ kg.

    Grams and millilitres are rounded to whole numbers. A pound is 453.592 g,
    but printing "453.59 g pasta" claims a precision nobody shops to and makes
    the list look machine-generated; "454 g" says the same thing honestly.
    """
    return f"{round(value):g}" if whole else f"{round(value, 2):g}"


def _render_segment(family: str, base_total: float, unit_votes: Counter) -> str:
    """Base-unit total -> a human amount.

    Mass is always rendered metric: pounds and ounces convert exactly, and
    "1.10231 lb" helps nobody in a Dutch supermarket. Volume keeps whichever
    cooking measure the recipes used, since converting a cup of a solid to
    millilitres implies a precision that isn't there.
    """
    if family == "mass":
        if base_total >= 1000:
            return f"{_decimal(base_total / 1000)} kg"
        return f"{_decimal(base_total, whole=True)} g"

    if family == "volume":
        cooking = Counter({unit: n for unit, n in unit_votes.items()
                           if unit in COOKING_MEASURES})
        if cooking:
            unit = cooking.most_common(1)[0][0]
            value = base_total / UNITS[unit][1]
            return f"{format_quantity(value)} {_pluralise(unit, value)}"
        if base_total >= 1000:
            return f"{_decimal(base_total / 1000)} l"
        return f"{_decimal(base_total, whole=True)} ml"

    # Counts: "2 clove garlic" reads badly, so a bare piece names nothing.
    unit = unit_votes.most_common(1)[0][0]
    noun = "" if unit == "piece" else _pluralise(unit, base_total)
    return f"{format_quantity(base_total)} {noun}".strip()


def build_lines(entries) -> list[Line]:
    """Aggregate scaled ingredient entries into shopping lines.

    Each entry is a mapping with: ingredient_id, name, aisle, quantity, unit,
    scale. Quantity may be None (a staple), in which case only the name matters.
    """
    totals: dict = defaultdict(lambda: defaultdict(float))
    votes: dict = defaultdict(lambda: defaultdict(Counter))
    meta: dict = {}
    staples: set = set()

    for entry in entries:
        key = entry["ingredient_id"]
        meta.setdefault(key, (entry["name"], entry.get("aisle") or DEFAULT_AISLE))

        unit = entry.get("unit")
        quantity = entry.get("quantity")

        if quantity is None or unit is None or UNITS[unit][0] == "vague":
            staples.add(key)
            continue

        family, factor = UNITS[unit]
        totals[key][family] += quantity * factor * entry.get("scale", 1.0)
        votes[key][family][unit] += 1

    lines = []
    for key, (name, aisle) in meta.items():
        families = totals.get(key, {})
        ordered = sorted(
            families,
            key=lambda fam: (FAMILY_ORDER.index(fam) if fam in FAMILY_ORDER else 99, fam),
        )
        segments = [_render_segment(fam, families[fam], votes[key][fam]) for fam in ordered]
        lines.append(
            Line(
                ingredient_id=key,
                name=name,
                aisle=aisle,
                segments=segments,
                # A staple only counts as one if it never had a real amount
                # anywhere in the week.
                staple=not segments and key in staples,
                split=len(segments) > 1,
            )
        )

    lines.sort(key=_sort_key)
    return lines


def _sort_key(line: Line):
    aisle_index = AISLE_ORDER.index(line.aisle) if line.aisle in AISLE_ORDER else 98
    return (aisle_index, line.aisle, line.name.lower())


def group_by_aisle(lines) -> list[tuple[str, list]]:
    """Lines grouped for display, staples excluded (they get their own block)."""
    grouped: dict = defaultdict(list)
    for line in lines:
        if not line.staple:
            grouped[line.aisle].append(line)

    return sorted(
        grouped.items(),
        key=lambda pair: (AISLE_ORDER.index(pair[0]) if pair[0] in AISLE_ORDER else 98, pair[0]),
    )


def as_text(grouped, staples, manual) -> str:
    """Plain text for pasting into a phone -- the fallback for every way the
    Picnic integration can fail, so it has to stand on its own."""
    out = []
    for aisle, lines in grouped:
        out.append(f"{aisle.upper()}")
        for line in lines:
            out.append(f"  {line.label}")
        out.append("")
    if manual:
        out.append("OVERIG")
        for item in manual:
            out.append(f"  {item['label']}")
        out.append("")
    if staples:
        out.append("CHECK THE CUPBOARD")
        out.append("  " + ", ".join(line.name for line in staples))
    return "\n".join(out).strip()
