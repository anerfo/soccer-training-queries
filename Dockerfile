FROM golang:1.26-bookworm AS pair-build

WORKDIR /src
COPY tools/pair/go.mod tools/pair/go.sum ./
RUN go mod download
COPY tools/pair/main.go tools/pair/index.html ./
RUN CGO_ENABLED=1 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/wa-pair .

FROM python:3.11-slim-bookworm

ARG UID=1000
ARG GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WA_SOCKET=/app/data/wa.sock \
    WA_DB=/app/data/wa_session.db \
    WA_AUTO_RESTART=1

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

COPY --chown=app:app wa.py polls.txt README.md ./
COPY --chown=app:app soccer ./soccer
COPY --chown=app:app templates ./templates
COPY --chown=app:app static ./static
COPY --from=pair-build /out/wa-pair /usr/local/bin/wa-pair
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/data \
    && chown app:app /app/data

USER app

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "wa.py", "serve", "--web-host", "0.0.0.0"]
