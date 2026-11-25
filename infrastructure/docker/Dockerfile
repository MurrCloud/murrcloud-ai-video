FROM postgres:17-bookworm AS pgtools

FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 murrvideo \
    && useradd --uid 10001 --gid murrvideo --no-create-home --shell /usr/sbin/nologin murrvideo \
    && install -d -o murrvideo -g murrvideo -m 0700 /var/lib/video /run/video-host \
    && install -d -m 0755 /app /opt/video

WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md requirements.lock /app/
COPY apps/backend /app/apps/backend
COPY workers /app/workers
COPY infrastructure/docker/worker-entrypoint.py infrastructure/docker/worker-health.py /opt/video/
COPY infrastructure/monitoring/admission.py /opt/video/admission.py
RUN pip install --require-hashes -r requirements.lock \
    && pip install --no-deps . \
    && pip check

USER 10001:10001
EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1", "--no-server-header", "--no-access-log"]

FROM base AS media
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001

FROM media AS test
USER root
COPY --from=pgtools /usr/lib/postgresql/17/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=pgtools /usr/lib/postgresql/17/bin/pg_restore /usr/local/bin/pg_restore
COPY --from=pgtools /usr/lib/*-linux-gnu/libpq.so.5* /usr/local/lib/
COPY requirements-dev.lock /app/
COPY tests /app/tests
COPY infrastructure/monitoring /app/infrastructure/monitoring
COPY infrastructure/docker/compose.vps.yml /app/infrastructure/docker/compose.vps.yml
COPY infrastructure/nginx /app/infrastructure/nginx
COPY infrastructure/systemd /app/infrastructure/systemd
COPY scripts /app/scripts
COPY docker-compose.yml docker-compose.prod.yml /app/
RUN apt-get update \
    && apt-get install -y --no-install-recommends age libpq5 libreadline8 liblz4-1 libzstd1 \
    && rm -rf /var/lib/apt/lists/* \
    && ldconfig \
    && pg_dump --version | grep ' 17\.' \
    && pg_restore --version | grep ' 17\.' \
    && pip install --require-hashes -r requirements-dev.lock && pip check
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests", "infrastructure/monitoring"]

# These images are opt-in. Tracked production rendering/narration gates stay false.
FROM media AS render
FROM media AS narration
