FROM golang:1.26-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go index.html ./
RUN CGO_ENABLED=1 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/pair .

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/pair /usr/local/bin/pair
WORKDIR /app
EXPOSE 8090
ENTRYPOINT ["pair"]
CMD ["--db", "/app/data/wa_session_new.db", "--addr", "0.0.0.0:8090"]
