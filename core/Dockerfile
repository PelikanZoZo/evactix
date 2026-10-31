FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY evactix ./evactix
RUN pip install . \
 && useradd --uid 10001 --create-home app \
 && mkdir -p /data/raw && chown -R app:app /data

USER app
CMD ["python", "-m", "evactix.runner"]
