"""Development entrypoint:  python run.py

Applies pending migrations first, so a fresh clone needs no setup step. In
production the container CMD runs `python db.py` before gunicorn instead --
same migration path, just not tied to the dev server.
"""
import db
from app import create_app

if __name__ == "__main__":
    db.migrate()
    create_app().run(host="127.0.0.1", port=5000, debug=True)
