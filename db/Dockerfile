# Postgres 16 + PostGIS + h3-pg. The postgis/postgis images don't ship h3-pg, so build on the
# official image, which already has the PGDG apt repo configured.
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-postgis-3 postgresql-16-h3 \
    && rm -rf /var/lib/apt/lists/*
