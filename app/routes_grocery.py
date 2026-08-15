"""The grocery list, and the ingredient aisle editor that makes it shoppable.

A permanent, first-class page -- not a stopgap until Picnic. It is the fallback
for every way that integration can fail, so it has to be good enough to use on
its own indefinitely.
"""
from datetime import timedelta

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from app import grocery, queries, weeks

bp = Blueprint("grocery", __name__)


@bp.get("/groceries")
def show():
    monday = weeks.parse_monday(request.args.get("week"))
    days = weeks.weekdays(monday)

    entries = queries.week_ingredients(days[0], days[-1])
    lines = grocery.build_lines(entries)

    grocery_list = queries.get_or_create_list(monday)
    stored = {row["label"]: row for row in queries.grocery_items(grocery_list["id"], manual=False)}
    manual = queries.grocery_items(grocery_list["id"], manual=True)

    # The stored rows carry the tick state; the freshly derived lines carry the
    # truth about amounts. Showing derived lines with stored ticks means the
    # page is never stale even if nobody pressed "regenerate".
    stale = {line.label for line in lines} != set(stored)

    def checked_for(line):
        row = stored.get(line.label)
        return bool(row["checked"]) if row else False

    def item_id_for(line):
        row = stored.get(line.label)
        return row["id"] if row else None

    grouped = grocery.group_by_aisle(lines)
    staples = [line for line in lines if line.staple]

    return render_template(
        "groceries.html",
        monday=monday,
        previous_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        is_current_week=monday == weeks.current_monday(),
        grouped=grouped,
        staples=staples,
        manual=manual,
        checked_for=checked_for,
        item_id_for=item_id_for,
        stale=stale,
        has_anything=bool(lines or manual),
        text_version=grocery.as_text(grouped, staples, manual),
    )


@bp.post("/groceries/<monday>/generate")
def generate(monday):
    start = weeks.parse_monday(monday)
    days = weeks.weekdays(start)

    lines = grocery.build_lines(queries.week_ingredients(days[0], days[-1]))
    grocery_list = queries.get_or_create_list(start)
    queries.replace_generated_items(grocery_list["id"], lines)

    flash(f"List rebuilt — {len(lines)} item{'' if len(lines) == 1 else 's'}.", "success")
    return redirect(url_for("grocery.show", week=start.isoformat()))


@bp.post("/groceries/<monday>/add")
def add_manual(monday):
    start = weeks.parse_monday(monday)
    label = request.form.get("label", "").strip()
    if not label:
        flash("Nothing to add.", "error")
    else:
        queries.add_manual_item(queries.get_or_create_list(start)["id"], label)
    return redirect(url_for("grocery.show", week=start.isoformat()))


@bp.post("/groceries/item/<int:item_id>/toggle")
def toggle(item_id):
    queries.toggle_item(item_id)
    return redirect(request.form.get("next") or url_for("grocery.show"))


@bp.post("/groceries/item/<int:item_id>/delete")
def delete(item_id):
    queries.delete_item(item_id)
    return redirect(request.form.get("next") or url_for("grocery.show"))


@bp.route("/ingredients", methods=["GET", "POST"])
def ingredients():
    """Aisle assignment. Without it every line lands in "overig" and the aisle
    grouping -- the thing that makes the list walkable -- does nothing."""
    if request.method == "POST":
        changed = 0
        for key, value in request.form.items():
            if not key.startswith("aisle-"):
                continue
            ingredient_id = key.removeprefix("aisle-")
            if ingredient_id.isdigit():
                queries.set_ingredient_aisle(int(ingredient_id), value.strip() or None)
                changed += 1
        flash(f"Updated {changed} ingredient{'' if changed == 1 else 's'}.", "success")
        return redirect(url_for("grocery.ingredients"))

    return render_template(
        "ingredients.html",
        ingredients=queries.all_ingredients(),
        aisles=grocery.AISLE_ORDER,
    )
