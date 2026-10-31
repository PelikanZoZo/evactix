FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY ana ./ana
RUN pip install . \
 && useradd --uid 10002 --create-home ana \
 && mkdir -p /state && chown ana:ana /state

USER ana
CMD ["python", "-m", "ana.main"]
