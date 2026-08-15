"""URL -> recipe, in three tiers.

1. schema.org Recipe JSON-LD. Present on most commercial recipe sites because
   Google's recipe rich results require it. Deterministic, free, cannot invent
   an ingredient. This is the path that should almost always fire.
2. Microdata/RDFa, for older sites without JSON-LD.
3. An LLM call, only when 1 and 2 find nothing (phase 5 -- not wired up yet).

All three return the same dict shape, so nothing downstream knows or cares
which one fired. `extraction` records it anyway, because a review card should
be able to say "this one came from a model, read it twice".

Fetching is guarded: this endpoint makes the server retrieve an arbitrary URL,
and the container shares a Docker network with every other service behind
Caddy. See `fetch()`.
"""
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "dinner/0.1 (personal meal planner; +https://dinner.btblog.dev)"
TIMEOUT_SECONDS = 20
MAX_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 5


class FetchError(Exception):
    """Anything that stopped us retrieving the page. Message is shown to the
    user, so it has to read like a sentence rather than a traceback."""


@dataclass
class ExtractedRecipe:
    title: str = ""
    source_url: str | None = None
    source_name: str | None = None
    instructions: str | None = None
    servings: int | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    image_url: str | None = None
    ingredient_lines: list[str] = field(default_factory=list)
    suggested_tags: list[str] = field(default_factory=list)
    extraction: str = "manual"
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _is_public_address(host: str) -> bool:
    """Resolve `host` and require every answer to be a public address.

    Checking all resolved addresses (not just the first) matters: a name that
    returns one public and one loopback record would otherwise slip through
    depending on resolution order.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve {host}.") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved
                or address.is_unspecified):
            return False
    return True


def _validate_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in ("http", "https"):
        raise FetchError("Only http and https URLs can be imported.")
    if not parsed.hostname:
        raise FetchError("That does not look like a URL.")
    if not _is_public_address(parsed.hostname):
        raise FetchError(
            "That address is on a private network, so it will not be fetched."
        )
    return urlunparse(parsed)


def fetch(url: str) -> tuple[str, str]:
    """Return (final_url, html).

    Redirects are followed by hand so every hop is re-validated -- a public URL
    that 302s to 169.254.169.254 is the classic way to turn a URL-fetching
    feature into a way to read the host's metadata service.
    """
    current = _validate_url(url)

    for _ in range(MAX_REDIRECTS):
        try:
            response = requests.get(
                current,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
                timeout=TIMEOUT_SECONDS,
                allow_redirects=False,
                stream=True,
            )
        except requests.exceptions.SSLError as exc:
            raise FetchError(
                f"The site's HTTPS certificate could not be verified ({exc.__class__.__name__}). "
                "Some sites serve an incomplete certificate chain; paste the recipe "
                "manually instead."
            ) from exc
        except requests.RequestException as exc:
            raise FetchError(f"Could not reach the site: {exc}") from exc

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise FetchError("The site redirected without saying where.")
            current = _validate_url(requests.compat.urljoin(current, location))
            continue

        if response.status_code != 200:
            response.close()
            raise FetchError(f"The site returned HTTP {response.status_code}.")

        content = b""
        for chunk in response.iter_content(64 * 1024):
            content += chunk
            if len(content) > MAX_BYTES:
                response.close()
                raise FetchError("That page is too large to import.")
        response.close()

        encoding = response.encoding or "utf-8"
        return current, content.decode(encoding, errors="replace")

    raise FetchError("Too many redirects.")


# --------------------------------------------------------------------------
# schema.org helpers
# --------------------------------------------------------------------------

def _find_recipe_node(node):
    """Walk arbitrarily nested JSON-LD for an @type of Recipe.

    Real pages wrap it in @graph, in arrays, or inside itemListElement, and
    @type itself may be a list. All of that shows up in the demo bank.
    """
    if isinstance(node, list):
        for item in node:
            found = _find_recipe_node(item)
            if found is not None:
                return found
    elif isinstance(node, dict):
        raw_type = node.get("@type")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if "Recipe" in types:
            return node
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
            if key in node:
                found = _find_recipe_node(node[key])
                if found is not None:
                    return found
    return None


DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.I,
)


def parse_duration_minutes(value) -> int | None:
    """ISO-8601 duration -> whole minutes.

    Both 'PT10M' and 'PT0H10M' occur in the demo bank, as does None.
    """
    if not value or not isinstance(value, str):
        return None
    match = DURATION_RE.match(value.strip())
    if not match:
        return None
    parts = match.groupdict()
    total = (
        int(parts["days"] or 0) * 24 * 60
        + int(parts["hours"] or 0) * 60
        + int(parts["minutes"] or 0)
        + float(parts["seconds"] or 0) / 60
    )
    return int(round(total)) or None


def parse_servings(value) -> tuple[int | None, list[str]]:
    """recipeYield -> a serving count.

    Seen in the wild: '4 servings', '10 to 14 servings', ['6 to 8 servings',
    '12 cups'], and bare '4'. For a range take the LOWER bound: servings is the
    divisor when scaling a recipe, so the smaller number yields more food --
    the same "rather have extra than run short" instinct the ingredient parser
    applies by taking the upper bound of an amount.
    """
    warnings: list[str] = []
    if value is None:
        return None, warnings
    if isinstance(value, list):
        value = value[0] if value else None
        warnings.append("recipe listed several yields; used the first")
    if isinstance(value, (int, float)):
        return int(value) or None, warnings
    if not isinstance(value, str):
        return None, warnings

    numbers = [int(n) for n in re.findall(r"\d+", value)]
    if not numbers:
        return None, warnings
    if len(numbers) > 1:
        warnings.append(f"yield '{value.strip()}' is a range; used {min(numbers)}")
    return min(numbers), warnings


def _instructions_to_text(value) -> str | None:
    """HowToStep / HowToSection / strings / nested lists -> plain text."""
    steps: list[str] = []

    def walk(node):
        if isinstance(node, str):
            # Separator=" " keeps words from fusing across tags ("Boil<b>water</b>"),
            # at the cost of a space before punctuation -- undone on the next line.
            text = BeautifulSoup(node, "html.parser").get_text(" ", strip=True)
            text = re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s+", " ", text)).strip()
            if text:
                steps.append(text)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if node.get("@type") == "HowToSection":
                walk(node.get("itemListElement"))
            else:
                walk(node.get("text") or node.get("name") or "")

    walk(value)
    return "\n\n".join(steps) or None


def _first_string(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            found = _first_string(item)
            if found:
                return found
    if isinstance(value, dict):
        return _first_string(value.get("url") or value.get("name"))
    return None


def _collect_tags(node: dict) -> list[str]:
    tags: list[str] = []
    for key in ("recipeCuisine", "recipeCategory"):
        value = node.get(key)
        if isinstance(value, str):
            tags.extend(part.strip() for part in value.split(","))
        elif isinstance(value, list):
            tags.extend(str(part).strip() for part in value)
    return [tag for tag in dict.fromkeys(tags) if tag]


def from_jsonld(node: dict, source_url: str | None = None) -> ExtractedRecipe:
    servings, warnings = parse_servings(node.get("recipeYield"))

    raw_ingredients = node.get("recipeIngredient") or node.get("ingredients") or []
    if isinstance(raw_ingredients, str):
        raw_ingredients = [raw_ingredients]

    publisher = node.get("publisher")
    source_name = None
    if isinstance(publisher, dict):
        source_name = publisher.get("name")
    if not source_name and source_url:
        source_name = urlparse(source_url).hostname

    return ExtractedRecipe(
        title=_first_string(node.get("name")) or "",
        source_url=source_url,
        source_name=source_name,
        instructions=_instructions_to_text(node.get("recipeInstructions")),
        servings=servings,
        prep_minutes=parse_duration_minutes(node.get("prepTime")),
        cook_minutes=parse_duration_minutes(node.get("cookTime")),
        image_url=_first_string(node.get("image")),
        ingredient_lines=[str(line).strip() for line in raw_ingredients if str(line).strip()],
        suggested_tags=_collect_tags(node),
        extraction="jsonld",
        warnings=warnings,
    )


def from_microdata(soup: BeautifulSoup, source_url: str | None = None) -> ExtractedRecipe | None:
    scope = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Recipe", re.I)})
    if scope is None:
        return None

    def prop(name):
        return scope.find_all(attrs={"itemprop": name})

    def text_of(element):
        if element.has_attr("content"):
            return element["content"].strip()
        return element.get_text(" ", strip=True)

    names = prop("name")
    ingredients = prop("recipeIngredient") or prop("ingredients")
    instructions = prop("recipeInstructions")
    yields = prop("recipeYield")

    if not ingredients:
        return None

    servings, warnings = parse_servings(text_of(yields[0]) if yields else None)
    prep = prop("prepTime")
    cook = prop("cookTime")

    return ExtractedRecipe(
        title=text_of(names[0]) if names else "",
        source_url=source_url,
        source_name=urlparse(source_url).hostname if source_url else None,
        instructions="\n\n".join(text_of(node) for node in instructions) or None,
        servings=servings,
        prep_minutes=parse_duration_minutes(prep[0].get("datetime") if prep else None),
        cook_minutes=parse_duration_minutes(cook[0].get("datetime") if cook else None),
        ingredient_lines=[text_of(node) for node in ingredients if text_of(node)],
        extraction="microdata",
        warnings=warnings,
    )


def from_html(html: str, source_url: str | None = None) -> ExtractedRecipe:
    soup = BeautifulSoup(html, "html.parser")

    for block in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(block.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        node = _find_recipe_node(data)
        if node:
            recipe = from_jsonld(node, source_url)
            if recipe.ingredient_lines:
                return recipe

    microdata = from_microdata(soup, source_url)
    if microdata:
        return microdata

    raise FetchError(
        "No recipe markup found on that page. Paste the recipe manually, or "
        "wait for the LLM fallback in phase 5."
    )


def from_url(url: str) -> ExtractedRecipe:
    final_url, html = fetch(url)
    recipe = from_html(html, final_url)
    if not recipe.title:
        recipe.warnings.append("no title found")
    if not recipe.ingredient_lines:
        recipe.warnings.append("no ingredients found")
    return recipe
