FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev gdal-bin libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

RUN pip install --no-cache-dir --upgrade \
    pip \
    "setuptools>=78.1.1" \
    "wheel>=0.46.2" \
    && pip install --no-cache-dir . \
    && pip install --no-cache-dir --upgrade \
    "jaraco.context>=6.1.0" \
    "msgpack>=1.2.1" \
    "setuptools>=78.1.1" \
    "wheel>=0.46.2"

ENV PORT=8086
EXPOSE 8086

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
