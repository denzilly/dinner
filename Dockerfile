FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py db.py run.py ./
COPY db ./db
COPY app ./app

# Pre-create data/ owned by the app user so the container can write the SQLite
# file even without the compose bind mount. When ./data is mounted it takes over
# this path -- keep the host directory owned by your own uid (1000 on this
# single-user host), same as marketsarchive's data/uploads volumes.
RUN useradd --create-home --uid 1000 dinner \
    && mkdir -p data \
    && chown -R dinner:dinner data

ENV DATABASE_PATH=/app/data/dinner.db

EXPOSE 8000
USER dinner

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=4).status == 200 else 1)"

# Migrations run to completion before gunicorn binds, so no request is ever
# served against a stale schema -- and no deploy has a "remember to migrate
# first" step.
CMD ["sh", "-c", "python db.py && exec gunicorn --bind 0.0.0.0:8000 --workers 2 --access-logfile - 'app:create_app()'"]
