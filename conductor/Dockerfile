FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY conductor ./conductor
RUN pip install . && useradd --uid 10003 --create-home conductor

USER conductor
CMD ["python", "-m", "conductor.service"]
