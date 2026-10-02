FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runtime state (TLE cache, ephemeris) lives on a volume, not in the image.
ENV GS_DATA_DIR=/data
RUN mkdir -p /data && useradd --create-home --uid 10001 gs && chown -R gs /data /app
USER gs

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
