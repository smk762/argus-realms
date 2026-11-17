#!/usr/bin/env bash
# Bake every area through argus-iris at background priority, until every chunk is done.
#
#   ATLAS_HOST=... ATLAS_MAPS=<fresh dir> ATLAS_MODELS=... ARGUS_GPU_BROKER_TOKEN=... \
#     nohup setsid bash scripts/run_bakes.sh [area ...] > run_bakes.log 2>&1 &
#
# Run on the GPU host, from this repo's checkout. Each chunk is its own `docker run argus-atlas chunk`, so each takes
# its own lease: at `background` it waits in iris's queue behind every other tenant and runs on capacity nobody else
# wants (the broker must run `admission: enforce`; in `observe` iris grants every lease at once). A lease may wait up
# to ARGUS_GPU_LEASE_TIMEOUT_S (a week by default). When a plan stops (a lease timed out, ComfyUI was idle-stopped
# while the chunk waited, a bake failed), this waits RETRY_S and runs it again: the plans skip finished chunks, so
# every pass picks up where the last one stopped. `touch $ATLAS_MAPS/STOP` ends the run after the current chunk.
#
# Areas run in the order given (default: the three Atlas publishes, then the planned arena regions).
set -uo pipefail
: "${ATLAS_HOST:?}" "${ATLAS_MAPS:?}" "${ATLAS_MODELS:?}"
: "${ARGUS_GPU_BROKER_TOKEN:?the broker's caller token (the host's .env)}"

export ARGUS_GPU_COORDINATOR=broker
# The broker as the bake containers see it (the plans map host.docker.internal to the host), and as this host sees it.
export ARGUS_GPU_BROKER_URL="${ARGUS_GPU_BROKER_URL:-http://host.docker.internal:8120}"
export IRIS_URL="${IRIS_URL:-http://127.0.0.1:8120}"
export ATLAS_GPU_PRIORITY="${ATLAS_GPU_PRIORITY:-background}"
export ARGUS_GPU_LEASE_TIMEOUT_S="${ARGUS_GPU_LEASE_TIMEOUT_S:-604800}"
export ARGUS_GPU_BROKER_FALLBACK="${ARGUS_GPU_BROKER_FALLBACK:-lease}"
export ARGUS_GPU_BROKER_TOKEN
RETRY_S="${RETRY_S:-600}"

cd "$(dirname "$0")/.."
AREAS=("$@")
[ ${#AREAS[@]} -gt 0 ] || AREAS=(cook-strait iceland reunion bonifacio seto salish cape)

log() { echo "$(date -u +%FT%TZ) $*"; }
stopped() { [ -f "$ATLAS_MAPS/STOP" ]; }

admission=$(curl -sf -m 5 "$IRIS_URL/health" | sed -n 's/.*"mode":"\([^"]*\)".*/\1/p')
[ -n "$admission" ] || { log "iris is not answering on $IRIS_URL"; exit 1; }
[ "$admission" = "observe-only" ] && log "warning: iris is observe-only: every lease is granted at once, nothing queues"

for area in "${AREAS[@]}"; do
  plan="bakes/$area.sh"
  [ -f "$plan" ] || { log "no plan $plan"; exit 1; }
  pass=1
  until stopped; do
    # Ungated work is what a background run must not do: while iris is down, wait instead of falling back.
    until curl -sf -m 5 "$IRIS_URL/health" >/dev/null; do stopped && break 2; log "iris down, waiting"; sleep 60; done
    log "$area: pass $pass ($ATLAS_GPU_PRIORITY, leases wait up to ${ARGUS_GPU_LEASE_TIMEOUT_S}s)"
    if bash "$plan"; then log "$area: done"; break; fi
    log "$area: pass $pass stopped; again in ${RETRY_S}s"
    pass=$((pass + 1))
    sleep "$RETRY_S"
  done
  stopped && { log "STOP found: ending after $area"; exit 0; }
done
log "all areas done: ${AREAS[*]}"
