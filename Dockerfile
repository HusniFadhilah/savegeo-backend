FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev gdal-bin libgdal-dev \
    && (apt-get purge -y --auto-remove python3-msgpack python3-setuptools || true) \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}"

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

RUN python -m pip install --no-cache-dir --upgrade \
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
    /opt/venv/bin/pip* \
    /opt/venv/lib/python3.11/site-packages/pip* \
    /opt/venv/lib/python3.11/site-packages/setuptools* \
    /opt/venv/lib/python3.11/site-packages/wheel*

# Keep compilers and development headers out of the production image. The
# scientific wheels still need these small runtime libraries at import time.
FROM python:3.11-slim-bookworm AS runtime

WORKDIR /app

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 libstdc++6 libpq5 \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# The Python slim base can carry older packaging metadata. Upgrade these
# tools in the final layer so the image contains only fixed releases.
RUN python -m pip install --no-cache-dir --upgrade \
    "jaraco.context>=6.1.0" \
    "wheel>=0.46.2" \
    && rm -rf /root/.cache/pip

COPY --from=builder /opt/venv /opt/venv

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

ENV PORT=8086
EXPOSE 8086

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
