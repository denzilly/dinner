"""Login and logout for the whole-site password gate."""
from urllib.parse import urlparse

from flask import Blueprint, redirect, render_template, request, session, url_for

bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str:
    """Only ever redirect to a path on this site.

    Without this check, /login?next=https://elsewhere turns the login form into
    an open redirect -- a useful ingredient in a phishing chain.
    """
    if not target:
        return url_for("plan.week")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return url_for("plan.week")
    if not target.startswith("/"):
        return url_for("plan.week")
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    from app import password_matches

    error = None
    next_target = _safe_next(request.values.get("next"))

    if request.method == "POST":
        if password_matches(request.form.get("password", "")):
            session.permanent = True
            session["authenticated"] = True
            return redirect(next_target)
        error = "That password is not right."

    return render_template("login.html", error=error, next_target=next_target)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
