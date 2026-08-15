"""The week board -- the main page -- and everything that mutates it."""
from datetime import date, timedelta

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)

from app import planner, queries, weeks

bp = Blueprint("plan", __name__)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        abort(404)


def _back_to_week(day: date):
    return redirect(url_for("plan.week", week=weeks.monday_of(day).isoformat()))


@bp.get("/")
def week():
    monday = weeks.parse_monday(request.args.get("week"))
    days = weeks.weekdays(monday)
    planned = queries.plan_days_between(days[0], days[-1])
    today = date.today()

    board = []
    for day in days:
        row = planned.get(day.isoformat())
        board.append(
            {
                "date": day,
                "iso": day.isoformat(),
                "weekday": weeks.WEEKDAY_NAMES[day.weekday()],
                "is_today": day == today,
                "state": row["state"] if row else "empty",
                "recipe_id": row["recipe_id"] if row else None,
                "recipe_title": row["recipe_title"] if row else None,
                "servings": (row["servings"] or row["recipe_servings"]) if row else None,
                "prep_minutes": row["prep_minutes"] if row else None,
                "cook_minutes": row["cook_minutes"] if row else None,
                "locked": bool(row["locked"]) if row else False,
                "note": row["note"] if row else None,
            }
        )

    empty_days = sum(1 for day in board if day["state"] == "empty")
    unlocked = sum(1 for day in board if day["state"] == "planned" and not day["locked"])

    return render_template(
        "plan.html",
        board=board,
        monday=monday,
        previous_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        is_current_week=monday == weeks.current_monday(),
        recipe_count=queries.recipe_count(),
        empty_days=empty_days,
        unlocked_count=unlocked,
    )


@bp.get("/plan/<day>/choose")
def choose(day):
    plan_date = _parse_day(day)
    filters = planner.Filters.from_request(request.args)
    monday = weeks.monday_of(plan_date)
    week_days = weeks.weekdays(monday)

    # Already-planned recipes are shown but marked, rather than hidden: cooking
    # the same thing twice in a week is a choice, not a mistake to prevent.
    already = queries.recipe_ids_in_week(week_days[0], week_days[-1])

    tags_by_kind = {}
    for tag in queries.tags_with_counts():
        tags_by_kind.setdefault(tag["kind"], []).append(tag)

    return render_template(
        "choose.html",
        plan_date=plan_date,
        weekday=weeks.WEEKDAY_NAMES[plan_date.weekday()],
        recipes=planner.candidates(filters),
        already_planned=already,
        filters=filters,
        tags_by_kind=tags_by_kind,
    )


@bp.post("/plan/<day>/set")
def set_day(day):
    plan_date = _parse_day(day)
    recipe_id = request.form.get("recipe_id", type=int)
    recipe = queries.get_recipe(recipe_id) if recipe_id else None
    if recipe is None:
        abort(404)

    queries.set_plan_day(plan_date, state="planned", recipe_id=recipe_id)
    flash(f"{weeks.WEEKDAY_NAMES[plan_date.weekday()]}: {recipe['title']}.", "success")
    return _back_to_week(plan_date)


@bp.post("/plan/<day>/random")
def random_day(day):
    plan_date = _parse_day(day)
    filters = planner.Filters.from_request(request.form)
    monday = weeks.monday_of(plan_date)
    week_days = weeks.weekdays(monday)

    exclude = queries.recipe_ids_in_week(week_days[0], week_days[-1])
    choice = planner.pick_for_day(filters, exclude_ids=exclude)

    if choice is None:
        # Say so rather than quietly falling back to the unfiltered bank -- a
        # "random" pick that ignores the filters you set is worse than none.
        flash(
            "Nothing matches those filters that isn't already planned this week."
            if filters.active else
            "No recipes available that aren't already planned this week.",
            "error",
        )
        return _back_to_week(plan_date)

    queries.set_plan_day(plan_date, state="planned", recipe_id=choice["id"])
    flash(f"{weeks.WEEKDAY_NAMES[plan_date.weekday()]}: {choice['title']}.", "success")
    return _back_to_week(plan_date)


@bp.post("/plan/<day>/skip")
def skip_day(day):
    plan_date = _parse_day(day)
    queries.set_plan_day(plan_date, state="skip", recipe_id=None)
    return _back_to_week(plan_date)


@bp.post("/plan/<day>/clear")
def clear_day(day):
    plan_date = _parse_day(day)
    queries.set_plan_day(plan_date, state="empty", recipe_id=None, locked=False)
    return _back_to_week(plan_date)


@bp.post("/plan/<day>/lock")
def toggle_lock(day):
    plan_date = _parse_day(day)
    existing = queries.plan_day(plan_date)
    if existing is None:
        abort(404)
    queries.set_plan_day(
        plan_date,
        state=existing["state"],
        recipe_id=existing["recipe_id"],
        servings=existing["servings"],
        locked=not existing["locked"],
    )
    return _back_to_week(plan_date)


@bp.post("/plan/<day>/details")
def update_details(day):
    plan_date = _parse_day(day)
    existing = queries.plan_day(plan_date)
    state = existing["state"] if existing else "empty"
    queries.set_plan_day(
        plan_date,
        state=state,
        recipe_id=existing["recipe_id"] if existing else None,
        servings=request.form.get("servings", type=int),
        note=request.form.get("note", "").strip() or "",
    )
    return _back_to_week(plan_date)


@bp.post("/plan/week/<monday>/fill")
def fill_week(monday):
    """Fill every empty day, without repeating anything already in the week."""
    start = weeks.monday_of(_parse_day(monday))
    days = weeks.weekdays(start)
    planned = queries.plan_days_between(days[0], days[-1])
    chosen = queries.recipe_ids_in_week(days[0], days[-1])

    filters = planner.Filters.from_request(request.form)
    filled = 0
    for day in days:
        row = planned.get(day.isoformat())
        if row is not None and row["state"] != "empty":
            continue
        choice = planner.pick_for_day(filters, exclude_ids=chosen)
        if choice is None:
            break
        queries.set_plan_day(day, state="planned", recipe_id=choice["id"])
        chosen.add(choice["id"])
        filled += 1

    if filled:
        flash(f"Filled {filled} day{'' if filled == 1 else 's'}.", "success")
    else:
        flash("Nothing left to fill with.", "error")
    return redirect(url_for("plan.week", week=start.isoformat()))


@bp.post("/plan/week/<monday>/reroll")
def reroll_week(monday):
    """Replace every unlocked planned day. Locked days and skips are untouched."""
    start = weeks.monday_of(_parse_day(monday))
    days = weeks.weekdays(start)
    planned = queries.plan_days_between(days[0], days[-1])
    filters = planner.Filters.from_request(request.form)

    keep = {
        row["recipe_id"]
        for row in planned.values()
        if row["recipe_id"] and row["locked"]
    }

    rerolled = 0
    for day in days:
        row = planned.get(day.isoformat())
        if row is None or row["state"] != "planned" or row["locked"]:
            continue
        choice = planner.pick_for_day(filters, exclude_ids=keep)
        if choice is None:
            break
        queries.set_plan_day(day, state="planned", recipe_id=choice["id"])
        keep.add(choice["id"])
        rerolled += 1

    if rerolled:
        flash(f"Rerolled {rerolled} day{'' if rerolled == 1 else 's'}.", "success")
    else:
        flash("Nothing to reroll — every planned day is locked.", "info")
    return redirect(url_for("plan.week", week=start.isoformat()))
