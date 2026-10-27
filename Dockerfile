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

RUN python -m venv --without-pip "${VIRTUAL_ENV}" \
    && /usr/local/bin/python -m pip install --no-cache-dir \
    --target "${VIRTUAL_ENV}/lib/python3.11/site-packages" ".[cams]" \
    && /usr/local/bin/python -m pip install --no-cache-dir --upgrade \
    --target "${VIRTUAL_ENV}/lib/python3.11/site-packages" \
    "msgpack>=1.2.1" \
    && rm -rf \
    /tmp/* \
    /root/.cache/pip \
    /usr/local/lib/python3.11/site-packages \
    /usr/local/lib/python3.11/dist-packages \
    /usr/local/lib/python3.11/ensurepip \
    && tar -C /usr/local \
    --exclude='lib/python3.11/site-packages' \
    --exclude='lib/python3.11/dist-packages' \
    --exclude='lib/python3.11/ensurepip' \
    --exclude='**/*msgpack*' \
    --exclude='**/*setuptools*' \
    -cf /tmp/python-runtime.tar .

# Keep optional CPU inference packages separate from API dependencies.
FROM python:3.11-slim-bookworm AS samgeo-builder
RUN python -m venv /opt/samgeo \
    && /opt/samgeo/bin/python -m pip install --no-cache-dir \
       torch==2.13.0 torchvision==0.28.0 \
       --index-url https://download.pytorch.org/whl/cpu \
    && /opt/samgeo/bin/python -m pip install --no-cache-dir \
       "Pillow>=13.0.0" segment-geospatial==1.4.2 transformers==5.19.0 \
    && /opt/samgeo/bin/python -m pip uninstall -y pip setuptools

# Granite/TerraTorch has a newer NumPy and geospatial stack than the API.
# Keep it in its own interpreter so it cannot upgrade or shadow API packages.
FROM python:3.12-slim-bookworm AS granite-builder
RUN python -m venv /opt/granite \
    && /opt/granite/bin/python -m pip install --no-cache-dir --no-deps \
       --index-url https://download.pytorch.org/whl/cpu \
       torch==2.10.0 torchvision==0.25.0 \
    && /opt/granite/bin/python -m pip install --no-cache-dir \
       terratorch==1.2.13 lightning>=2.6.6 filelock>=4.2.1 \
       fsspec>=2026.6.0 geopandas>=1.1.2 \
    && /opt/granite/bin/python -m pip uninstall -y pip setuptools
RUN /opt/granite/bin/python -c "from terratorch.cli_tools import LightningInferenceModel; import torch, torchvision; print(torch.__version__, torchvision.__version__)"
RUN rm -rf \
    /usr/local/lib/python3.12/site-packages \
    /usr/local/lib/python3.12/dist-packages \
    /usr/local/lib/python3.12/ensurepip \
    && tar -C /usr/local \
    --exclude='lib/python3.12/site-packages' \
    --exclude='lib/python3.12/dist-packages' \
    --exclude='lib/python3.12/ensurepip' \
    --exclude='bin/python' \
    --exclude='bin/python3' \
    -cf /tmp/python312-runtime.tar .

# Keep compilers and development headers out of the production image. Start
# from a plain Debian runtime so its system Python metadata cannot reintroduce
# stale packaging versions; copy only the patched interpreter from the builder.
FROM debian:bookworm-slim AS runtime

WORKDIR /app

ARG SAVEGEO_PRODUCT_VERSION
ARG SAVEGEO_COMMIT_SHA
ARG SAVEGEO_BUILD_NUMBER
LABEL org.opencontainers.image.title="SAVEGEO backend" \
      org.opencontainers.image.version="$SAVEGEO_PRODUCT_VERSION" \
      org.opencontainers.image.revision="$SAVEGEO_COMMIT_SHA" \
      org.opencontainers.image.source="https://github.com/HusniFadhilah/savegeo-backend"

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV GRANITE_PYTHON=/opt/granite/bin/python
ENV SAVEGEO_BUILD_NUMBER="${SAVEGEO_BUILD_NUMBER}" \
    SAVEGEO_COMMIT_SHA="${SAVEGEO_COMMIT_SHA}" \
    SAVEGEO_PRODUCT_VERSION="${SAVEGEO_PRODUCT_VERSION}" \
    SAVEGEO_DEPLOYMENT_METADATA_FILE="/app/var/deployment-metadata.json"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libbz2-1.0 libexpat1 libffi8 libgomp1 liblzma5 \
    libpq5 libsqlite3-0 libssl3 libstdc++6 libuuid1 zlib1g \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /tmp/python-runtime.tar /tmp/python-runtime.tar
COPY --from=granite-builder /tmp/python312-runtime.tar /tmp/python312-runtime.tar
RUN tar -C /usr/local -xf /tmp/python-runtime.tar \
    && tar -C /usr/local -xf /tmp/python312-runtime.tar \
    && rm -f /tmp/python-runtime.tar \
    && rm -f /tmp/python312-runtime.tar
COPY --from=builder /opt/venv /opt/venv
COPY --from=samgeo-builder /opt/samgeo /opt/samgeo
COPY --from=granite-builder /opt/granite /opt/granite
ENV SAMGEO_PYTHON=/opt/samgeo/bin/python
RUN /opt/samgeo/bin/python -c "from samgeo import SamGeo; from transformers import AutoProcessor, AutoModelForZeroShotObjectDetection; from torchvision.ops import nms"

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

ENV PORT=8086
EXPOSE 8086

CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
