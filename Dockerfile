FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev gdal-bin libgdal-dev \
    && (apt-get purge -y --auto-remove python3-msgpack python3-setuptools || true) \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

RUN python -m venv "${VIRTUAL_ENV}" \
    && python -m pip install --no-cache-dir --upgrade \
    pip \
    "setuptools>=78.1.1" \
    "wheel>=0.46.2" \
    && python -m pip install --no-cache-dir . \
    && python -m pip install --no-cache-dir --upgrade \
    "jaraco.context>=6.1.0" \
    "msgpack>=1.2.1" \
    "setuptools>=78.1.1" \
    "wheel>=0.46.2" \
    && rm -rf \
    /tmp/* \
    /root/.cache/pip \
    /usr/local/lib/python3.11/site-packages \
    /usr/local/lib/python3.11/dist-packages

# Keep compilers and development headers out of the production image. Start
# from a plain Debian runtime so its system Python metadata cannot reintroduce
# stale packaging versions; copy only the patched interpreter from the builder.
FROM debian:bookworm-slim AS runtime

WORKDIR /app

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libbz2-1.0 libexpat1 libffi8 libgomp1 liblzma5 \
    libpq5 libsqlite3-0 libssl3 libstdc++6 libuuid1 zlib1g \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
COPY --from=builder /opt/venv /opt/venv

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

ENV PORT=8086
EXPOSE 8086

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
