"""Choosing recipes for a day.

Random picking is deliberately not uniform over the whole bank. A uniform draw
happily suggests last Tuesday's dinner again, which is the one thing a weekly
planner exists to avoid. Instead the pool is sorted stalest-first and the draw
happens among the stale half -- still unpredictable, but it works through the
bank rather than orbiting a few favourites.
"""
import random
from dataclasses import dataclass

from app import queries

# Never narrow the draw below this many candidates, or a small bank turns
# deterministic and "random" always returns the same recipe.
MINIMUM_POOL = 5


@dataclass
class Filters:
    query: str = ""
    tag_ids: tuple = ()
    max_minutes: int | None = None

    @classmethod
    def from_request(cls, args) -> "Filters":
        return cls(
            query=args.get("q", "").strip(),
            tag_ids=tuple(int(v) for v in args.getlist("tag") if v.isdigit()),
            max_minutes=args.get("max_minutes", type=int),
        )

    def as_params(self) -> list[tuple[str, str]]:
        """Flatten back into query-string pairs, so a filtered choose page can
        hand the same filters to the random endpoint."""
        params = []
        if self.query:
            params.append(("q", self.query))
        for tag_id in self.tag_ids:
            params.append(("tag", str(tag_id)))
        if self.max_minutes:
            params.append(("max_minutes", str(self.max_minutes)))
        return params

    @property
    def active(self) -> bool:
        return bool(self.query or self.tag_ids or self.max_minutes)


def candidates(filters: Filters, exclude_ids=()) -> list:
    """Recipes matching the filters, stalest first, minus anything excluded."""
    rows = queries.search_recipes(
        query=filters.query,
        tag_ids=filters.tag_ids,
        max_minutes=filters.max_minutes,
        sort="unused",          # last_planned_on IS NULL first, then oldest
    )
    excluded = set(exclude_ids)
    return [row for row in rows if row["id"] not in excluded]


def pick(pool: list, rng: random.Random | None = None):
    """Draw one recipe, biased toward the least recently cooked."""
    if not pool:
        return None
    rng = rng or random
    cutoff = max(MINIMUM_POOL, len(pool) // 2)
    return rng.choice(pool[:cutoff])


def pick_for_day(filters: Filters, exclude_ids=(), rng=None):
    return pick(candidates(filters, exclude_ids), rng)
