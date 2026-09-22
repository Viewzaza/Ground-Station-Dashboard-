FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
# satnogs-auto-scheduler is the Libre Space Foundation's official scheduler and
# is published only on GitLab, so it installs from git rather than PyPI. git is
# a build-time need alone: it is installed, used and purged inside this one
# layer so it never reaches the running image. ca-certificates is named
# explicitly so --auto-remove treats it as wanted and leaves it behind — the
# backend needs it to talk HTTPS to SatNOGS at runtime.
#
# That package is AGPL-3.0; this repo is MIT. It is never imported — the
# station scheduler runs it as a separate process — which keeps the two an
# aggregation rather than a derived work. See README, "Licence".
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove git \
 && rm -rf /var/lib/apt/lists/*

COPY app ./app

# Runtime state (TLE cache, ephemeris) lives on a volume, not in the image.
ENV GS_DATA_DIR=/data
RUN mkdir -p /data && useradd --create-home --uid 10001 gs && chown -R gs /data /app
USER gs

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
