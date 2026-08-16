"""Token-gated ingest API.

One entry point for every source: the webapp's own "add from URL" box, an
iOS Share Sheet shortcut, a bookmarklet, and later Hermes. None of them know
anything about the schema -- they post a URL or a loose recipe object and the
normalisation happens here, in one place.

    POST /api/recipes/ingest
    Authorization: Bearer <INGEST_TOKEN>

    {"url": "https://..."}
    {"title": "...", "ingredients": ["500 g gehakt", "2 uien"], ...}

Everything lands as status='suggested'. Nothing enters the bank unreviewed,
regardless of who posted it.
"""
import hmac

from flask import Blueprint, jsonify, request, url_for

import config
from app import extract, parse, queries

bp = Blueprint("api", __name__, url_prefix="/api")


def _authorized() -> bool:
    if not config.INGEST_TOKEN:
        # Refuse rather than defaulting open: an unset token on a deployed
        # instance would otherwise mean anyone can write to the bank.
        return False
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented, config.INGEST_TOKEN)


def _ingest_payload(payload: dict) -> tuple[dict, int]:
    """Shared by the API and the webapp's own URL box."""
    url = (payload.get("url") or "").strip()

    if url:
        existing = queries.find_by_source_url(url)
        try:
            extracted = extract.from_url(url)
        except extract.FetchError as exc:
            return {"error": str(exc), "url": url}, 422
        # Re-posting a URL updates the pending suggestion rather than piling up
        # duplicates; an already-accepted recipe is left alone.
        recipe_id = existing["id"] if existing and existing["status"] == "suggested" else None
        if existing and existing["status"] == "active":
            return {
                "status": "already_in_bank",
                "id": existing["id"],
                "title": existing["title"],
            }, 200
    else:
        # A hand-built payload goes through the same parsers as an extracted
        # page. Passing these straight through was how 'servings': 'abc' reached
        # an INTEGER column as text -- the normalisation this module promises has
        # to cover both branches, not just the URL one.
        servings, servings_warnings = extract.parse_servings(payload.get("servings"))
        prep_minutes, prep_warnings = extract.parse_minutes(payload.get("prep_minutes"))
        cook_minutes, cook_warnings = extract.parse_minutes(payload.get("cook_minutes"))

        extracted = extract.ExtractedRecipe(
            title=(payload.get("title") or "").strip(),
            source_url=(payload.get("source_url") or "").strip() or None,
            source_name=(payload.get("source_name") or "").strip() or None,
            instructions=payload.get("instructions"),
            servings=servings,
            prep_minutes=prep_minutes,
            cook_minutes=cook_minutes,
            ingredient_lines=[str(line) for line in (payload.get("ingredients") or [])],
            suggested_tags=[str(tag) for tag in (payload.get("tags") or [])],
            extraction="manual",
            warnings=[
                *servings_warnings,
                *(f"prep time: {w}" for w in prep_warnings),
                *(f"cook time: {w}" for w in cook_warnings),
            ],
        )
        recipe_id = None

    if not extracted.title:
        return {"error": "A recipe needs a title."}, 422
    if not extracted.ingredient_lines:
        return {"error": "No ingredients found."}, 422

    parsed = parse.parse_lines(extracted.ingredient_lines)

    # An unknown yield is stored as the schema's default so the write can't fail,
    # which makes it indistinguishable from a stated yield of 4 once it lands.
    # Record that it was assumed, so the review card can ask for a real number
    # instead of quietly presenting the default as fact.
    needs = []
    if extracted.servings is None:
        needs.append("servings")
        extracted.warnings.append(
            f"no serving count found — assumed {queries.DEFAULT_SERVINGS}; "
            "set the real one before accepting"
        )

    warnings = [{"line": None, "warnings": extracted.warnings}] if extracted.warnings else []
    warnings += [
        {"line": item.raw, "warnings": item.warnings}
        for item in parsed
        if item.warnings
    ]

    recipe_id = queries.save_recipe(
        recipe_id=recipe_id,
        title=extracted.title,
        parsed_ingredients=parsed,
        instructions=extracted.instructions,
        source_url=extracted.source_url,
        source_name=extracted.source_name,
        servings=extracted.servings,
        prep_minutes=extracted.prep_minutes,
        cook_minutes=extracted.cook_minutes,
        status="suggested",
        extraction=extracted.extraction,
        extraction_warnings={
            "suggested_tags": extracted.suggested_tags,
            "lines": warnings,
            "needs": needs,
        },
    )

    return {
        "status": "suggested",
        "id": recipe_id,
        "title": extracted.title,
        "extraction": extracted.extraction,
        "ingredients": len(parsed),
        "warnings": sum(len(entry["warnings"]) for entry in warnings),
        "review_url": url_for("recipes.review", _external=True, _anchor=f"recipe-{recipe_id}"),
    }, 201


@bp.post("/recipes/ingest")
def ingest():
    if not _authorized():
        return jsonify({"error": "Bad or missing ingest token."}), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object."}), 400

    body, status = _ingest_payload(payload)
    return jsonify(body), status
