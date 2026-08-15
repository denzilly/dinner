"""Test fixtures: an isolated database per test, and a client that can post
to the ingest API.

config's values are read at call time by db.get_connection(), so pointing
DATABASE_PATH at a tmp file before create_app() is enough to keep tests off the
development database.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import db as db_module  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "INGEST_TOKEN", "test-token")
    monkeypatch.setattr(config, "SITE_PASSWORD", None)
    monkeypatch.setattr(config, "SECRET_KEY", "test-secret")

    db_module.migrate(verbose=False)

    from app import create_app

    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer test-token"}
