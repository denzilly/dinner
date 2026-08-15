"""Free-text ingredient lines -> quantity / unit / name / note.

Every extraction tier ends up here: schema.org gives `recipeIngredient` as an
array of human-written strings, not structured amounts, so this runs on JSON-LD
output exactly as it does on a pasted cookbook page.

The lexicon covers English and Dutch because the sources do. The demo bank is
NYT Cooking (cups, pints, pounds) while shopping happens at a Dutch supermarket
(gram, eetlepel, teen) -- both have to parse, so both are here.

Nothing guesses silently. A line the parser is unsure about keeps its raw text
and carries a warning, which the review card shows; a wrong quantity reaching
the grocery list unnoticed is worse than being asked.
"""
import re
import unicodedata
from dataclasses import dataclass, field

# Unit -> (family, factor to the family's base unit).
#
# Mass and volume convert freely within themselves. Count units each form their
# own family: 2 cloves + 1 can is not 3 of anything, and pretending otherwise is
# how a grocery list starts lying.
UNITS: dict[str, tuple[str, float]] = {
    # mass, base gram
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "oz": ("mass", 28.3495),
    "lb": ("mass", 453.592),
    # volume, base millilitre
    "ml": ("volume", 1.0),
    "l": ("volume", 1000.0),
    "tsp": ("volume", 5.0),
    "tbsp": ("volume", 15.0),
    "cup": ("volume", 240.0),
    "floz": ("volume", 29.5735),
    "pint": ("volume", 473.176),
    "quart": ("volume", 946.353),
    # counts -- deliberately non-interchangeable
    "piece": ("count:piece", 1.0),
    "clove": ("count:clove", 1.0),
    "can": ("count:can", 1.0),
    "jar": ("count:jar", 1.0),
    "package": ("count:package", 1.0),
    "bunch": ("count:bunch", 1.0),
    "sprig": ("count:sprig", 1.0),
    "stalk": ("count:stalk", 1.0),
    "head": ("count:head", 1.0),
    "slice": ("count:slice", 1.0),
    # vague -- never aggregated, dropped from the grocery list
    "pinch": ("vague", 1.0),
    "dash": ("vague", 1.0),
    "handful": ("vague", 1.0),
    "to_taste": ("vague", 1.0),
}

# Spelling -> canonical unit. Longest match wins, so "fl oz" beats "oz".
UNIT_ALIASES: dict[str, str] = {
    # mass
    "g": "g", "gr": "g", "gram": "g", "grams": "g", "gramme": "g", "grammes": "g",
    "kg": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    # volume
    "ml": "ml", "milliliter": "ml", "millilitre": "ml", "milliliters": "ml",
    "millilitres": "ml", "cl": "ml",
    "l": "l", "liter": "l", "litre": "l", "liters": "l", "litres": "l",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "tl": "tsp", "theelepel": "tsp", "theelepels": "tsp",
    "tbsp": "tbsp", "tbs": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "el": "tbsp", "eetlepel": "tbsp", "eetlepels": "tbsp",
    "cup": "cup", "cups": "cup",
    "floz": "floz", "fl oz": "floz", "fluid ounce": "floz", "fluid ounces": "floz",
    "pint": "pint", "pints": "pint",
    "quart": "quart", "quarts": "quart",
    # counts
    "piece": "piece", "pieces": "piece", "stuk": "piece", "stuks": "piece",
    "clove": "clove", "cloves": "clove", "teen": "clove", "tenen": "clove",
    "can": "can", "cans": "can", "tin": "can", "tins": "can",
    "blik": "can", "blikken": "can", "blikje": "can",
    "jar": "jar", "jars": "jar", "pot": "jar", "potje": "jar",
    "package": "package", "packages": "package", "pack": "package",
    "pak": "package", "pakje": "package", "pakken": "package",
    "bunch": "bunch", "bunches": "bunch", "bos": "bunch", "bosje": "bunch",
    "sprig": "sprig", "sprigs": "sprig", "takje": "sprig", "takjes": "sprig",
    "stalk": "stalk", "stalks": "stalk", "stengel": "stalk", "stengels": "stalk",
    "head": "head", "heads": "head", "krop": "head",
    "slice": "slice", "slices": "slice", "plak": "slice", "plakje": "slice",
    "plakjes": "slice",
    # vague
    "pinch": "pinch", "pinches": "pinch", "snuf": "pinch", "snufje": "pinch",
    "dash": "dash", "dashes": "dash", "scheut": "dash", "scheutje": "dash",
    "handful": "handful", "handfuls": "handful", "handvol": "handful",
}

# Unit words made of two tokens, checked before single-token lookup.
MULTIWORD_UNITS = {"fl oz", "fluid ounce", "fluid ounces"}

# Trailing phrases meaning "no measurable amount".
TO_TASTE = re.compile(r",?\s*(to taste|naar smaak|as needed|indien nodig)\s*$", re.I)

OPTIONAL = re.compile(r"[\s,(]*\b(optional|optioneel)\b\)?\s*$", re.I)

# 1, 1.5, 1,5, 1/2, 1 1/2, 1½, ½
NUMBER = r"\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?"
RANGE_SEP = r"(?:\s*(?:-|–|—|to|tot|or|of)\s*)"
QUANTITY_RE = re.compile(rf"^\s*({NUMBER})(?:{RANGE_SEP}({NUMBER}))?\s*", re.I)


@dataclass
class ParsedIngredient:
    raw: str
    name: str
    quantity: float | None = None
    unit: str | None = None
    note: str | None = None
    optional: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def family(self) -> str | None:
        return UNITS[self.unit][0] if self.unit else None

    @property
    def is_vague(self) -> bool:
        return self.family == "vague"


# Vulgar fractions are rewritten as "1/2" rather than "0.5" so the conversion
# is safe to apply to the whole line: a note reading "cut into 1/2-inch slices"
# still makes sense to a human, where "cut into 0.5-inch slices" reads like a
# machine got at it. The quantity regex understands both forms anyway.
VULGAR_FRACTIONS = {
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5", "⅙": "1/6", "⅚": "5/6",
    "⅐": "1/7", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8", "⅑": "1/9",
    "⅒": "1/10",
}

# Words that sit between the amount and the unit ("2 packed cups basil").
# Without skipping these the unit is lost and the amount silently becomes a
# count of nothing.
QUANTITY_MODIFIERS = {
    "packed", "heaping", "heaped", "rounded", "level", "generous", "scant",
    "big", "large", "small", "extra", "full", "good",
    "ruime", "volle", "afgestreken", "flinke", "kleine", "grote",
}


def _fractions_to_ascii(text: str) -> str:
    """½ -> 1/2, and 1½ -> 1 1/2 so the number regex sees both parts."""
    out = []
    for char in text:
        replacement = VULGAR_FRACTIONS.get(char)
        if replacement is None:
            value = unicodedata.numeric(char, None)
            if value is not None and not char.isdigit():
                replacement = f"{value:g}"
        if replacement is not None:
            # A fraction glued to a digit ("1½") needs a separator.
            if out and out[-1].isdigit():
                out.append(" ")
            out.append(replacement)
        else:
            out.append(char)
    return "".join(out)


# Fractions a recipe actually uses. Fifths, sevenths and tenths parse fine on
# the way in but are never rendered: "1.2 kg" is a real amount someone typed,
# and showing it back as "1⅕ kg" is worse than the decimal.
DISPLAY_FRACTIONS = ("½", "⅓", "⅔", "¼", "¾", "⅛", "⅜", "⅝", "⅞")


def format_quantity(value: float | None) -> str:
    """Render a stored amount the way a recipe would write it.

    0.333333 as displayed by %g is noise on a shopping list; ⅓ is what the
    source said in the first place.
    """
    if value is None:
        return ""
    whole = int(value)
    remainder = round(value - whole, 4)
    for glyph in DISPLAY_FRACTIONS:
        numerator, _, denominator = VULGAR_FRACTIONS[glyph].partition("/")
        if abs(remainder - int(numerator) / int(denominator)) < 0.005:
            return f"{whole}{glyph}" if whole else glyph
    return f"{value:g}"


def _to_number(token: str) -> float | None:
    token = token.strip().replace(",", ".")
    if "/" in token:
        numerator, _, denominator = token.partition("/")
        try:
            return float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(token)
    except ValueError:
        return None


def _match_unit(words: list[str]) -> tuple[str | None, int, list[str]]:
    """Return (canonical unit, words consumed, modifiers skipped).

    Skipping one modifier lets "2 packed cups basil" and "2 big handfuls
    arugula" find their unit; the skipped word is handed back so it can be
    preserved in the note rather than silently dropped.
    """
    skipped: list[str] = []
    offset = 0

    while offset < len(words) and words[offset].lower().strip(".,") in QUANTITY_MODIFIERS:
        skipped.append(words[offset])
        offset += 1
        if len(skipped) > 1:      # "2 big generous heaping cups" is not a thing
            break

    remaining = words[offset:]
    if len(remaining) >= 2:
        pair = f"{remaining[0]} {remaining[1]}".lower().strip(".")
        if pair in MULTIWORD_UNITS:
            return UNIT_ALIASES[pair], offset + 2, skipped
    if remaining:
        single = remaining[0].lower().strip(".")
        if single in UNIT_ALIASES:
            return UNIT_ALIASES[single], offset + 1, skipped

    # No unit after all -- the modifier belongs to the ingredient name, so
    # report nothing consumed.
    return None, 0, []


def _extract_parentheticals(text: str) -> tuple[str, list[str]]:
    """Pull "(2-ounce)" style asides out before anything else is parsed.

    This has to happen *before* unit matching, not after: in "1 (2-ounce) can
    anchovy fillets" the aside sits between the amount and the unit, so leaving
    it in place hides the "can" and the line silently becomes a count of
    nothing.
    """
    notes: list[str] = []

    def take(match):
        notes.append(match.group(1).strip())
        return " "

    return re.sub(r"\(([^)]*)\)", take, text), notes


# NYT writes dual measures: "3 cups/8 ounces sugar snap peas". The volume half
# is unshoppable for a solid; the weight half is exactly what a shop sells by.
DUAL_MEASURE = re.compile(
    r"\b([a-z]+)\s*/\s*([\d\s./¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]+)\s*([a-z]+)\b", re.I
)


def _prefer_mass_alternative(text: str) -> tuple[str, bool]:
    """Collapse a dual measure like "3 cups/8 ounces X" down to one.

    The weight half wins when there is one, since that is what a shop sells by.
    Otherwise the first half is kept and the alternative dropped -- either way
    the line has to end up with a single measure, because "cups/500" is not a
    unit and leaving it in place loses the amount entirely.
    """
    match = DUAL_MEASURE.search(text)
    if not match:
        return text, False

    first_unit = UNIT_ALIASES.get(match.group(1).lower())
    second_unit = UNIT_ALIASES.get(match.group(3).lower())
    if not first_unit or not second_unit:
        return text, False

    if UNITS[second_unit][0] == "mass":
        # Drop everything before the measure too -- that leading amount belonged
        # to the half being discarded.
        return f"{match.group(2).strip()} {match.group(3)} {text[match.end():].strip()}", True

    return f"{text[:match.start()]}{match.group(1)} {text[match.end():].strip()}", False


def _split_note(text: str) -> tuple[str, str | None]:
    """Separate the ingredient from its preparation note.

    "red onion, halved and sliced" -> ("red onion", "halved and sliced")
    """
    head, sep, tail = text.partition(",")
    note = tail.strip() if sep and tail.strip() else None
    name = re.sub(r"\s+", " ", head).strip(" ,;")
    return name, note


def parse_line(line: str) -> ParsedIngredient:
    raw = line.strip()
    if not raw:
        return ParsedIngredient(raw=raw, name="", warnings=["empty line"])

    working = raw
    warnings: list[str] = []

    optional = bool(OPTIONAL.search(working))
    if optional:
        working = OPTIONAL.sub("", working)

    to_taste = bool(TO_TASTE.search(working))
    if to_taste:
        working = TO_TASTE.sub("", working)

    # Before fractions become ASCII, since that introduces slashes of its own
    # ("1½" -> "1 1/2") which would confuse the dual-measure pattern.
    working, used_alternative = _prefer_mass_alternative(working)
    if used_alternative:
        warnings.append("recipe gave two measures; used the weight")

    working, parentheticals = _extract_parentheticals(working)

    working = _fractions_to_ascii(working)

    quantity = None
    match = QUANTITY_RE.match(working)
    if match:
        low = _to_number(match.group(1))
        high = _to_number(match.group(2)) if match.group(2) else None

        if low is not None and high is not None:
            # Take the upper bound: being short at dinner costs more than a
            # little surplus in the fridge. Flagged either way.
            quantity = max(low, high)
            warnings.append(f"range {match.group(1)}–{match.group(2)}, used {quantity:g}")
        elif low is not None and high is None and match.group(2) is None:
            quantity = low
        else:
            quantity = low

        # "1 1/2 cups" arrives as two numbers with no range separator.
        remainder = working[match.end():]
        mixed = re.match(rf"^({NUMBER})\s+", remainder)
        if mixed and quantity is not None and match.group(2) is None:
            extra = _to_number(mixed.group(1))
            if extra is not None and extra < 1:
                quantity += extra
                remainder = remainder[mixed.end():]
        working = remainder
    else:
        working = working

    words = working.split()
    unit, consumed, modifiers = _match_unit(words)
    if unit:
        words = words[consumed:]
    working = " ".join(words)

    if to_taste:
        unit = unit or "to_taste"

    name, note = _split_note(working)

    extra_notes = [part for part in ([" ".join(modifiers).lower()] if modifiers else [])]
    extra_notes += parentheticals
    if note:
        extra_notes.append(note)
    note = "; ".join(part for part in extra_notes if part) or None

    if not name:
        warnings.append("no ingredient name found")
    if quantity is None and unit is None and not to_taste:
        warnings.append("no quantity or unit -- treated as 'some'")
    if quantity is not None and unit is None:
        # "2 uien" / "1 red onion": a bare count is the sane reading.
        unit = "piece"

    return ParsedIngredient(
        raw=raw,
        name=name,
        quantity=quantity,
        unit=unit,
        note=note,
        optional=optional,
        warnings=warnings,
    )


def parse_lines(lines) -> list[ParsedIngredient]:
    parsed = [parse_line(line) for line in lines]
    return [item for item in parsed if item.name or item.raw.strip()]


def canonical_name(name: str) -> str:
    """Key used to match against the ingredients table.

    Only case and whitespace are normalised. Descriptors ("pitted kalamata
    olives") are deliberately left alone -- stripping adjectives automatically
    merges things that shouldn't merge, and the review card's autocomplete is a
    better place for a human to make that call.
    """
    return re.sub(r"\s+", " ", name).strip().lower()
