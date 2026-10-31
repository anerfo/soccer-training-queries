FROM python:3.11-slim-bookworm

ARG UID=1000
ARG GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WA_SOCKET=/app/data/wa.sock \
    WA_DB=/app/data/wa_session.db

# libmagic is required by neonize's python-magic dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${GID}" app \
    && useradd --uid "${UID}" --gid app --create-home app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools \
    && pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app wa.py web.py auth.py calendar_sync.py calendar_data.py calendar_scheduler.py polls.txt README.md ./
COPY --chown=app:app templates ./templates
COPY --chown=app:app static ./static
RUN mkdir -p /app/data && chown app:app /app/data

USER app

EXPOSE 8080

CMD ["python", "wa.py", "serve", "--web-host", "0.0.0.0"]
