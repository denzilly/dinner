"""The recipe bank: import, review queue, list, and the manual form."""
import json

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)

from app import parse, queries
from app.routes_api import _ingest_payload

bp = Blueprint("recipes", __name__, url_prefix="/recipes")

TAG_KINDS = ["cuisine", "diet", "protein", "effort", "season", "free"]


def _suggestion_detail(row):
    """Everything the review card needs, unpacked from the stored JSON blob."""
    extra = json.loads(row["extraction_warnings"]) if row["extraction_warnings"] else {}
    warned_lines = {
        entry["line"]: entry["warnings"]
        for entry in extra.get("lines", [])
        if entry.get("line")
    }
    recipe_warnings = [
        warning
        for entry in extra.get("lines", [])
        if not entry.get("line")
        for warning in entry["warnings"]
    ]
    ingredients = queries.ingredients_for_recipe(row["id"])
    return {
        "row": row,
        "ingredients": ingredients,
        "warned_lines": warned_lines,
        "recipe_warnings": recipe_warnings,
        "suggested_tags": extra.get("suggested_tags", []),
        "warning_count": sum(len(w) for w in warned_lines.values()) + len(recipe_warnings),
    }


@bp.get("/")
def index():
    query = request.args.get("q", "").strip()
    tag_ids = [int(value) for value in request.args.getlist("tag") if value.isdigit()]
    max_minutes = request.args.get("max_minutes", type=int)
    sort = request.args.get("sort", "title")

    recipes = queries.search_recipes(
        query=query, tag_ids=tag_ids, max_minutes=max_minutes, sort=sort
    )

    tags_by_kind = {}
    for tag in queries.tags_with_counts():
        tags_by_kind.setdefault(tag["kind"], []).append(tag)

    return render_template(
        "recipes/index.html",
        recipes=recipes,
        query=query,
        active_tags=tag_ids,
        tags_by_kind=tags_by_kind,
        max_minutes=max_minutes,
        sort=sort,
        suggestion_count=queries.suggestion_count(),
    )


@bp.get("/review")
def review():
    return render_template(
        "recipes/review.html",
        suggestions=[_suggestion_detail(row) for row in queries.suggestions()],
    )


@bp.post("/import")
def import_urls():
    """Bulk import: one URL per line.

    Each URL is reported individually -- with a list of twenty, "3 failed" with
    no indication of which three is useless.
    """
    raw = request.form.get("urls", "")
    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not urls:
        flash("Paste at least one URL.", "error")
        return redirect(url_for("recipes.review"))

    added, skipped, failed = 0, 0, []
    for url in urls:
        body, status = _ingest_payload({"url": url})
        if status == 201:
            added += 1
        elif body.get("status") == "already_in_bank":
            skipped += 1
        else:
            failed.append(f"{url} — {body.get('error', 'unknown error')}")

    if added:
        flash(f"Imported {added} recipe{'' if added == 1 else 's'} for review.", "success")
    if skipped:
        flash(f"{skipped} already in the bank.", "info")
    for failure in failed:
        flash(failure, "error")

    return redirect(url_for("recipes.review"))


@bp.post("/<int:recipe_id>/accept")
def accept(recipe_id):
    recipe = queries.get_recipe(recipe_id)
    if recipe is None:
        abort(404)

    title = request.form.get("title", "").strip()
    servings = request.form.get("servings", type=int)

    tag_ids = [int(value) for value in request.form.getlist("tag_id") if value.isdigit()]
    for name in request.form.getlist("new_tag"):
        if name.strip():
            tag_ids.append(queries.upsert_tag(name.strip(), request.form.get("tag_kind", "free")))

    conn = queries.get_db()
    conn.execute(
        "UPDATE recipes SET title = COALESCE(NULLIF(?, ''), title), servings = COALESCE(?, servings) WHERE id = ?",
        (title, servings, recipe_id),
    )
    conn.commit()

    queries.set_recipe_tags(recipe_id, tag_ids)
    queries.set_status(recipe_id, "active")
    flash(f"Added “{title or recipe['title']}” to the bank.", "success")
    return redirect(url_for("recipes.review"))


@bp.post("/<int:recipe_id>/reject")
def reject(recipe_id):
    if queries.get_recipe(recipe_id) is None:
        abort(404)
    queries.set_status(recipe_id, "rejected")
    flash("Rejected — it won't come back.", "info")
    return redirect(url_for("recipes.review"))


@bp.post("/<int:recipe_id>/archive")
def archive(recipe_id):
    if queries.get_recipe(recipe_id) is None:
        abort(404)
    queries.set_status(recipe_id, "archived")
    flash("Archived. Past weeks keep their reference to it.", "info")
    return redirect(url_for("recipes.index"))


@bp.get("/<int:recipe_id>")
def detail(recipe_id):
    recipe = queries.get_recipe(recipe_id)
    if recipe is None:
        abort(404)
    return render_template(
        "recipes/detail.html",
        recipe=recipe,
        ingredients=queries.ingredients_for_recipe(recipe_id),
        tags=queries.tags_for_recipe(recipe_id),
    )


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<int:recipe_id>/edit", methods=["GET", "POST"])
def edit(recipe_id=None):
    recipe = queries.get_recipe(recipe_id) if recipe_id else None
    if recipe_id and recipe is None:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        block = request.form.get("ingredients", "")
        parsed = parse.parse_lines(block.splitlines())

        if not title:
            flash("A recipe needs a title.", "error")
        elif not parsed:
            flash("A recipe needs at least one ingredient.", "error")
        else:
            tag_ids = [int(v) for v in request.form.getlist("tag_id") if v.isdigit()]
            saved_id = queries.save_recipe(
                recipe_id=recipe_id,
                title=title,
                parsed_ingredients=parsed,
                instructions=request.form.get("instructions", "").strip() or None,
                source_url=request.form.get("source_url", "").strip() or None,
                notes=request.form.get("notes", "").strip() or None,
                servings=request.form.get("servings", type=int),
                prep_minutes=request.form.get("prep_minutes", type=int),
                cook_minutes=request.form.get("cook_minutes", type=int),
                status=recipe["status"] if recipe else "active",
                extraction=recipe["extraction"] if recipe else "manual",
            )
            queries.set_recipe_tags(saved_id, tag_ids)
            flash("Saved.", "success")
            return redirect(url_for("recipes.detail", recipe_id=saved_id))

    existing_lines = ""
    if recipe:
        existing_lines = "\n".join(
            _render_ingredient_line(row) for row in queries.ingredients_for_recipe(recipe_id)
        )

    tags_by_kind = {}
    for tag in queries.all_tags():
        tags_by_kind.setdefault(tag["kind"], []).append(tag)

    return render_template(
        "recipes/form.html",
        recipe=recipe,
        ingredient_block=existing_lines,
        tags_by_kind=tags_by_kind,
        selected_tags={tag["id"] for tag in queries.tags_for_recipe(recipe_id)} if recipe else set(),
        tag_kinds=TAG_KINDS,
    )


def _render_ingredient_line(row) -> str:
    """Rebuild an editable text line from stored rows.

    The form round-trips through the same parser it was saved with, so editing
    is text in and text out -- no second, subtly different input format.
    """
    parts = []
    if row["quantity"] is not None:
        # Rendered as a vulgar fraction, which the parser converts straight back
        # on save -- so the round trip is lossless and the textarea stays readable.
        parts.append(parse.format_quantity(row["quantity"]))
    if row["unit"] and row["unit"] != "piece":
        parts.append(row["unit"])
    parts.append(row["name"])
    line = " ".join(parts)
    if row["note"]:
        line += f", {row['note']}"
    if row["optional"]:
        line += " (optional)"
    return line


@bp.post("/tags")
def create_tag():
    name = request.form.get("name", "").strip()
    kind = request.form.get("kind", "free")
    if name:
        queries.upsert_tag(name, kind if kind in TAG_KINDS else "free")
        queries.get_db().commit()
        flash(f"Tag “{name}” added.", "success")
    return redirect(request.referrer or url_for("recipes.index"))
