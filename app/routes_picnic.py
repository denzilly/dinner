"""The proposed Picnic cart -- a separate page from the grocery list.

Phase 3's list is the simple one you take to the store to shop yourself. This
is the other job: turning the same week into a Picnic basket. Keeping them
apart is deliberate; product-picking and pack maths would wreck a page that
gets used one-handed in a supermarket.

Nothing here auto-selects a product. Picnic's search ranking cannot be trusted
(its top hit for "olijfolie" is an olive oil spray), so an ingredient is either
already confirmed by a human or it is waiting for one.

**This page makes no bulk API calls.** Re-resolving every mapped product on
load would be ~20 sequential round-trips before anything renders. A row's
search only ever runs for that one row, when it's expanded (`open` in the
query string) -- the real prices come back from Picnic's own cart after the
push anyway, which is more trustworthy than an estimate assembled here.
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


def _row(line, mapping):
    """One line's match state, in the shape the table row (and the row's own
    expanded panel) render from."""
    if mapping is None:
        return {"line": line, "mapping": None, "plan": None,
                "status": "needs_choice", "stale": False, "partial": False}

    if mapping["decision"] == "never":
        return {"line": line, "mapping": mapping, "plan": None,
                "status": "never", "stale": False, "partial": False}

    plan = picnic.plan_packs(
        line.totals, mapping["pack_covers_qty"], mapping["pack_covers_unit"]
    )
    return {
        "line": line, "mapping": mapping, "plan": plan, "status": "mapped",
        # A mapping in grams answers the mass part of "1 blik + 400 g
        # tomaten" and says nothing about the tin. Flag it rather than
        # quietly buying half of what the week needs.
        "partial": plan is not None and line.split,
        # The recipes changed under a mapping that no longer fits.
        "stale": plan is None,
    }


_STATUS_ORDER = {"needs_choice": 0, "mapped": 1, "never": 2}


def _plan(lines):
    """Sort the week's lines into the matchable table (unmatched first, then
    matched, then "not via Picnic") and the staples, which stay a separate
    opt-in list rather than joining the table."""
    mappings = queries.picnic_mappings(line.ingredient_id for line in lines)

    matchable, staples = [], []
    for line in lines:
        row = _row(line, mappings.get(line.ingredient_id))
        # Shown so you can eyeball whether you're low, never added by
        # default: olive oil is in half the recipes and bought quarterly.
        (staples if line.staple else matchable).append(row)

    matchable.sort(key=lambda item: _STATUS_ORDER[item["status"]])
    return matchable, staples


@bp.get("/groceries/picnic")
def show():
    monday = weeks.parse_monday(request.args.get("week"))
    items, staples = _plan(_week_lines(monday))

    # At most one row's search ever runs -- whichever one is expanded.
    open_id = request.args.get("open", type=int)
    query = request.args.get("q") or ""
    hits, error = [], None
    open_item = next(
        (item for item in items + staples if item["line"].ingredient_id == open_id),
        None,
    )
    if open_item is not None:
        query = query or open_item["line"].name
        try:
            hits = picnic.search(picnic.client(), query)
        except picnic.PicnicUnavailable as exc:
            error = str(exc)

    return render_template(
        "picnic.html",
        monday=monday,
        previous_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        is_current_week=monday == weeks.current_monday(),
        items=items,
        staples=staples,
        has_anything=bool(items or staples),
        open_id=open_id,
        query=query,
        hits=hits,
        error=error,
        units=sorted({unit for unit in picnic.UNITS if picnic.UNITS[unit][0] != "vague"}),
    )


@bp.post("/groceries/picnic/choose/<int:ingredient_id>")
def confirm(ingredient_id):
    monday = weeks.parse_monday(request.form.get("week"))
    back = url_for("picnic.show", week=monday.isoformat()) + f"#row-{ingredient_id}"

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
        reopen = url_for("picnic.show", week=monday.isoformat(),
                         open=ingredient_id) + f"#row-{ingredient_id}"
        return redirect(reopen)

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

    items, staples = _plan(_week_lines(monday))
    by_id = {str(item["line"].ingredient_id): item for item in items + staples}

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
