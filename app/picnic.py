"""Picnic: pack arithmetic, and a thin wrapper over the API client.

Split deliberately. Everything above `--- API ---` is pure arithmetic over the
grocery list and is tested offline; everything below talks to Picnic and is
stubbed in tests, the same way `extract.from_url` is.

The design is in project.md under phase 6. The one idea worth repeating here:
a mapping stores how much of an ingredient **one pack covers, in the recipe's
units** -- not Picnic's. Picnic sells rookworst by weight while a recipe counts
sausages, and parse.py's units refuse to convert across dimensions on purpose.
A human answers "one pack covers one rookworst" once; after that every case is
the same division.
"""
import math
import re
from dataclasses import dataclass

import config
from app.parse import UNIT_ALIASES, UNITS, format_quantity

# Picnic returns prices as integer cents.
CENTS = 100


# --- pack arithmetic (pure) -----------------------------------------------

@dataclass
class PackPlan:
    """How many packs to buy, and the honest arithmetic behind it."""
    packs: int
    needed: float          # in pack_unit, what the week actually calls for
    covered: float         # in pack_unit, what those packs add up to
    unit: str
    rounded_up: bool

    @property
    def summary(self) -> str:
        """The annotation shown on the proposed list.

        Rounding up changes what you are charged, so it is stated rather than
        left to be inferred from the total.
        """
        covered = f"{format_quantity(self.covered)} {self.unit}"
        if not self.rounded_up:
            return f"{self.packs} x {format_quantity(self.pack_size)} {self.unit}"
        needed = f"{format_quantity(self.needed)} {self.unit}"
        return (f"{self.packs} x {format_quantity(self.pack_size)} {self.unit} "
                f"= {covered}, need {needed}")

    @property
    def pack_size(self) -> float:
        return self.covered / self.packs if self.packs else 0.0


def plan_packs(totals: dict, pack_covers_qty: float, pack_covers_unit: str) -> PackPlan | None:
    """How many packs cover this week's requirement for one ingredient.

    `totals` is a grocery Line's family -> base-unit totals. The pack's own unit
    decides which family is being bought: a mapping in grams answers the mass
    requirement and says nothing about "1 blik" on the same line, which is why
    the caller flags split lines rather than this silently covering one of them.

    Returns None when the ingredient has no requirement in the pack's family --
    that is a mapping that no longer fits the recipes, not an amount of zero.
    """
    if pack_covers_unit not in UNITS or pack_covers_qty <= 0:
        return None

    family, factor = UNITS[pack_covers_unit]
    base_needed = totals.get(family)
    if not base_needed:
        return None

    pack_base = pack_covers_qty * factor
    packs = math.ceil(base_needed / pack_base)
    needed = base_needed / factor
    covered = packs * pack_covers_qty

    return PackPlan(
        packs=packs,
        needed=needed,
        covered=covered,
        unit=pack_covers_unit,
        # Float division leaves 750/250 at 2.9999...; compare on the rendered
        # scale instead so an exact fit isn't reported as a round-up.
        rounded_up=round(covered - needed, 6) > 0,
    )


def parse_pack_quantity(text: str | None) -> tuple[float, str] | None:
    """Picnic's free-text quantity ("300 gram", "3 stuks") -> (qty, unit).

    Only used to pre-fill the confirmation form -- a human still confirms it,
    because this is exactly the sort of guess that should not go in unattended.
    parse.py's alias table already speaks Dutch (gram, kilo, stuk, stuks), so
    this needs no vocabulary of its own.
    """
    if not text:
        return None

    match = re.match(r"\s*([\d.,]+)\s*([a-zA-Z]+)", text.strip())
    if not match:
        return None

    number, word = match.groups()
    try:
        quantity = float(number.replace(",", "."))
    except ValueError:
        return None

    unit = UNIT_ALIASES.get(word.lower())
    if not unit or quantity <= 0:
        return None
    return quantity, unit


def price_per_unit(price_cents: int | None, text: str | None) -> str | None:
    """"€11.10 / kg" -- so cheapest and least-waste can be told apart.

    The spike found rundergehakt at €14.17/kg in a 300 g pack and €10.79/kg in
    a 1 kg one. That tradeoff is the shopper's to make, so it has to be visible.
    """
    parsed = parse_pack_quantity(text)
    if not price_cents or not parsed:
        return None

    quantity, unit = parsed
    family, factor = UNITS[unit]
    # Quote per kilo/litre for mass and volume, and per item for anything
    # counted -- "€0.36 per piece" is the useful form for onions.
    reference, label = (1000.0, "kg") if family == "mass" else \
                       (1000.0, "l") if family == "volume" else (1.0, unit)

    base = quantity * factor
    if base <= 0:
        return None
    return f"EUR {price_cents / CENTS * (reference / base):.2f} / {label}"


# --- API ------------------------------------------------------------------

class PicnicUnavailable(Exception):
    """Any reason we could not talk to Picnic. Message is shown to the user."""


def _token() -> str | None:
    from pathlib import Path

    path = Path(config.PICNIC_TOKEN_PATH)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def client():
    """An authenticated client, or PicnicUnavailable.

    Token only -- never username/password. Picnic requires SMS 2FA on login,
    which a web request cannot answer, so establishing the token is a separate
    interactive job (spikes/picnic_spike.py). A missing or expired token is a
    thing to report, not something to silently work around.
    """
    try:
        from python_picnic_api2 import PicnicAPI
    except ImportError as exc:                       # pragma: no cover
        raise PicnicUnavailable("The Picnic client library is not installed.") from exc

    token = _token()
    if not token:
        raise PicnicUnavailable(
            "No Picnic session yet. Run spikes/picnic_spike.py once to log in "
            "and store the token."
        )

    api = PicnicAPI(auth_token=token, country_code=config.PICNIC_COUNTRY)
    try:
        # logged_in() only checks that a token string exists; it never asks the
        # server, so an expired token would pass it and fail later mid-cart.
        api.get_user()
    except Exception as exc:                         # noqa: BLE001
        raise PicnicUnavailable(
            "The stored Picnic session was rejected -- re-run "
            "spikes/picnic_spike.py to refresh it."
        ) from exc
    return api


def search(api, term: str) -> list[dict]:
    """Normalised search hits. Picnic's shapes stop here."""
    try:
        result = api.search(term)
    except Exception as exc:                         # noqa: BLE001
        raise PicnicUnavailable(f"Picnic search for {term!r} failed.") from exc

    hits = []
    for item in getattr(result, "items", []) or []:
        unit_text = getattr(item, "unit_quantity", None)
        price = getattr(item, "display_price", None)
        hits.append({
            "product_id": item.id,
            "name": item.name,
            "unit_text": unit_text,
            "price_cents": price,
            "price": f"EUR {price / CENTS:.2f}" if price else None,
            "per_unit": price_per_unit(price, unit_text),
            "parsed": parse_pack_quantity(unit_text),
        })
    return hits
