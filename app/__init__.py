"""Flask application factory.

The whole-site password gate is built in from the start rather than added after
deployment -- research_aggregator went the other way and it meant a retrofit
across every route. There is deliberately no second write-secret: one household,
one password. That extra layer was removed from research_aggregator as pure
friction, and there is no reason to reintroduce it here.
"""
import hmac
from datetime import timedelta

from flask import Flask, redirect, render_template, request, session, url_for

import config
from app import queries

# Reachable without a session. Everything else redirects to /login when
# SITE_PASSWORD is set.
PUBLIC_ENDPOINTS = {"static", "auth.login", "auth.logout", "health"}


def create_app() -> Flask:
    app = Flask(__name__)

    if config.SITE_PASSWORD and not config.SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY must be set whenever SITE_PASSWORD is set -- it signs "
            "the session cookie the gate depends on."
        )

    app.secret_key = config.SECRET_KEY or "insecure-development-key"
    app.permanent_session_lifetime = timedelta(days=config.SESSION_DAYS)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    app.teardown_appcontext(queries.close_db)

    from app import parse

    app.jinja_env.filters["amount"] = parse.format_quantity

    @app.before_request
    def require_password():
        if not config.SITE_PASSWORD:
            return None
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        # The ingest API authenticates with INGEST_TOKEN instead of the session
        # cookie, so it must bypass this gate -- otherwise Share Sheet and
        # Hermes clients get an HTML login page rather than a 200.
        if request.blueprint == "api":
            return None
        if session.get("authenticated"):
            return None
        return redirect(url_for("auth.login", next=request.full_path))

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    from app.routes_api import bp as api_bp
    from app.routes_auth import bp as auth_bp
    from app.routes_grocery import bp as grocery_bp
    from app.routes_picnic import bp as picnic_bp
    from app.routes_plan import bp as plan_bp
    from app.routes_recipes import bp as recipes_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(plan_bp)
    app.register_blueprint(recipes_bp)
    app.register_blueprint(grocery_bp)
    app.register_blueprint(picnic_bp)
    app.register_blueprint(api_bp)

    @app.context_processor
    def template_globals():
        from app import queries

        def pending_reviews():
            # Lazily called from the nav badge, so requests that never render
            # the header (the API) don't pay for the query.
            return queries.suggestion_count()

        return {
            "password_gate_enabled": bool(config.SITE_PASSWORD),
            "pending_reviews": pending_reviews,
        }

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("404.html"), 404

    return app


def password_matches(candidate: str) -> bool:
    """Constant-time comparison, so the response time doesn't leak the prefix."""
    if not config.SITE_PASSWORD:
        return True
    return hmac.compare_digest(candidate, config.SITE_PASSWORD)
