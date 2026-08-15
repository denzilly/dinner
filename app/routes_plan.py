"""The week board -- the main page.

Phase 0 renders the week read-only. Choosing, skipping and rerolling recipes
arrive in phase 2, once the recipe bank exists to choose from.
"""
from datetime import date, timedelta

from flask import Blueprint, render_template, request

from app import queries, weeks

bp = Blueprint("plan", __name__)


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
                "weekday": weeks.WEEKDAY_NAMES[day.weekday()],
                "is_today": day == today,
                "state": row["state"] if row else "empty",
                "recipe_title": row["recipe_title"] if row else None,
                "note": row["note"] if row else None,
            }
        )

    return render_template(
        "plan.html",
        board=board,
        monday=monday,
        previous_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        is_current_week=monday == weeks.current_monday(),
        recipe_count=queries.recipe_count(),
    )
