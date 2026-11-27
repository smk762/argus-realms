# The shared world's image: the API (`python -m atlas_realms.api --world ...`), the tick workers
# (`python -m atlas_realms.tick.runner --region ...`) and the migrate job (`python -m atlas_realms.regions prepare ...`).
# deploy/k8s/ runs it; `make k8s-images` builds it as atlas-realms:<git sha>. No tiles: they are served elsewhere.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.12.15-slim-bookworm
# hatch-vcs has no .git here, so the version comes in from the build (make k8s-images passes the git describe).
ARG VERSION=0.0.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} \
    ARGUS_BUILD=${VERSION}
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
# The package reads its data relative to the repo root (atlas_realms.regions, atlas_realms.api), so the repo's layout
# is kept and the install is editable.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src src
COPY web web
COPY data data
COPY arena/regions.json arena/regions.json
COPY arena/world arena/world
RUN uv pip install --system --no-cache -e ".[server]" \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin realms
USER 10001
EXPOSE 8110
CMD ["python", "-m", "atlas_realms.api", "--help"]
