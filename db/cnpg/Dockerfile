# The shared world's Postgres on Kubernetes: a CloudNativePG operand image plus PostGIS and h3-pg, the same extensions
# as db/Dockerfile (the compose database). CloudNativePG's own PostGIS images don't ship h3-pg. Its standard images
# carry the PGDG apt repository, and run as uid 26. Keep the base at the newest 16.x: the PostGIS package pulls the
# server up to the newest minor anyway, and the tag should say what runs.
FROM ghcr.io/cloudnative-pg/postgresql:16.16-standard-bookworm

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-postgis-3 postgresql-16-h3 \
    && rm -rf /var/lib/apt/lists/*
USER 26
