"""The proposed Picnic cart -- a separate page from the grocery list.

Phase 3's list is the simple one you take to the store to shop yourself. This
is the other job: turning the same week into a Picnic basket. Keeping them
apart is deliberate; product-picking and pack maths would wreck a page that
gets used one-handed in a supermarket.

Nothing here auto-selects a product. Picnic's search ranking cannot be trusted
(its top hit for "olijfolie" is an olive oil spray), so an ingredient is either
already confirmed by a human or it is waiting for one.

**This page makes no API calls.** Re-resolving every mapped product on load
would be ~20 sequential round-trips before anything renders. Searching happens
on the choose page, one ingredient at a time, and the real prices come back
from Picnic's own cart after the push -- which is more trustworthy than an
estimate assembled here anyway.
"""
from datetime import timedelta

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from app import grocery, picnic, queries, weeks

# Explicit paths rather than a url_prefix, matching routes_grocery.py -- and
# a prefix with a "/" rule would make the bare /groceries/picnic a redirect.
bp = Blueprint("picnic", __name__)


def _week_lines(monday):
    days = weeks.weekdays(monday)
    return grocery.build_lines(queries.week_ingredients(days[0], days[-1]))


def _plan(lines):
    """Sort the week's lines into the four states the page renders."""
    mappings = queries.picnic_mappings(line.ingredient_id for line in lines)

    proposed, undecided, never, staples = [], [], [], []

    for line in lines:
        mapping = mappings.get(line.ingredient_id)

        if line.staple:
            # Shown so you can eyeball whether you're low, never added by
            # default: olive oil is in half the recipes and bought quarterly.
            staples.append({"line": line, "mapping": mapping})
            continue

        if mapping is None:
            undecided.append(line)
            continue

        if mapping["decision"] == "never":
            never.append({"line": line, "mapping": mapping})
            continue

        plan = picnic.plan_packs(
            line.totals, mapping["pack_covers_qty"], mapping["pack_covers_unit"]
        )
        proposed.append({
            "line": line,
            "mapping": mapping,
            "plan": plan,
            # A mapping in grams answers the mass part of "1 blik + 400 g
            # tomaten" and says nothing about the tin. Flag it rather than
            # quietly buying half of what the week needs.
            "partial": plan is not None and line.split,
            # The recipes changed under a mapping that no longer fits.
            "stale": plan is None,
        })

    return proposed, undecided, never, staples


@bp.get("/groceries/picnic")
def show():
    monday = weeks.parse_monday(request.args.get("week"))
    proposed, undecided, never, staples = _plan(_week_lines(monday))

    return render_template(
        "picnic.html",
        monday=monday,
        previous_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        is_current_week=monday == weeks.current_monday(),
        proposed=proposed,
        undecided=undecided,
        never=never,
        staples=staples,
        has_anything=bool(proposed or undecided or never or staples),
    )


@bp.get("/groceries/picnic/choose/<int:ingredient_id>")
def choose(ingredient_id):
    """Alternatives for one ingredient. The only page that calls search()."""
    ingredient = queries.get_ingredient(ingredient_id)
    if ingredient is None:
        flash("No such ingredient.", "error")
        return redirect(url_for("picnic.show"))

    monday = weeks.parse_monday(request.args.get("week"))
    line = next(
        (l for l in _week_lines(monday) if l.ingredient_id == ingredient_id), None
    )

    hits, error = [], None
    try:
        hits = picnic.search(picnic.client(), request.args.get("q") or ingredient["name"])
    except picnic.PicnicUnavailable as exc:
        error = str(exc)

    return render_template(
        "picnic_choose.html",
        ingredient=ingredient,
        line=line,
        monday=monday,
        hits=hits,
        error=error,
        query=request.args.get("q") or ingredient["name"],
        current=queries.picnic_mappings([ingredient_id]).get(ingredient_id),
        units=sorted({unit for unit in picnic.UNITS if picnic.UNITS[unit][0] != "vague"}),
    )


@bp.post("/groceries/picnic/choose/<int:ingredient_id>")
def confirm(ingredient_id):
    monday = weeks.parse_monday(request.form.get("week"))
    back = url_for("picnic.show", week=monday.isoformat())

    if request.form.get("action") == "never":
        queries.set_picnic_never(ingredient_id)
        flash("Won't be offered via Picnic again.", "info")
        return redirect(back)

    if request.form.get("action") == "forget":
        queries.clear_picnic_mapping(ingredient_id)
        flash("Forgotten — it'll be asked again.", "info")
        return redirect(back)

    product_id = (request.form.get("product_id") or "").strip()
    unit = (request.form.get("pack_covers_unit") or "").strip()
    try:
        quantity = float((request.form.get("pack_covers_qty") or "").replace(",", "."))
    except ValueError:
        quantity = 0.0

    # The pack size is what every future week divides by, so a bad one is wrong
    # forever rather than once. Refuse it here instead of storing it.
    if not product_id or quantity <= 0 or unit not in picnic.UNITS:
        flash("Pick a product and say how much one pack covers.", "error")
        return redirect(url_for("picnic.choose", ingredient_id=ingredient_id,
                                week=monday.isoformat()))

    queries.set_picnic_mapping(
        ingredient_id,
        product_id=product_id,
        product_name=(request.form.get("product_name") or "").strip() or None,
        pack_covers_qty=quantity,
        pack_covers_unit=unit,
        picnic_unit_text=(request.form.get("picnic_unit_text") or "").strip() or None,
    )
    flash("Saved — it won't be asked again.", "success")
    return redirect(back)


@bp.post("/groceries/picnic/push")
def push():
    """Add the ticked lines to the real Picnic basket. Never checks out."""
    monday = weeks.parse_monday(request.form.get("week"))
    back = url_for("picnic.show", week=monday.isoformat())

    wanted = {value for value in request.form.getlist("include")}
    if not wanted:
        flash("Nothing ticked.", "error")
        return redirect(back)

    proposed, _, _, staples = _plan(_week_lines(monday))
    by_id = {str(item["line"].ingredient_id): item for item in proposed + staples}

    try:
        api = picnic.client()
    except picnic.PicnicUnavailable as exc:
        flash(str(exc), "error")
        return redirect(back)

    added, failed = 0, []
    for key in wanted:
        item = by_id.get(key)
        if item is None or not item["mapping"] or item["mapping"]["decision"] != "mapped":
            continue
        # A staple has no computed quantity -- one pack is the whole point of
        # ticking it.
        packs = item["plan"].packs if item.get("plan") else 1
        try:
            api.add_product(item["mapping"]["product_id"], count=packs)
            added += 1
        except Exception as exc:                     # noqa: BLE001
            # Most likely a product that no longer exists. Report which one:
            # "3 failed" without saying which is useless, same as bulk import.
            failed.append(f"{item['line'].name} — {type(exc).__name__}")

    if added:
        flash(f"Added {added} item{'' if added == 1 else 's'} to your Picnic basket. "
              "Review and check out in the Picnic app.", "success")
    for failure in failed:
        flash(f"Could not add {failure}", "error")

    return redirect(back)
