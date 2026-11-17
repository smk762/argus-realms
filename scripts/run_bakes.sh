#!/usr/bin/env bash
# Bake every area through argus-iris at background priority, until every chunk is done.
#
#   ATLAS_HOST=... ATLAS_MAPS=<fresh dir> ATLAS_MODELS=... ARGUS_GPU_BROKER_TOKEN=... \
#     bash scripts/run_bakes.sh [area ...]
#
# It detaches itself (no nohup or & needed), prints its pid and the log path ($ATLAS_MAPS/run_bakes.log), and returns.
# One runner per ATLAS_MAPS: a second launch while one runs is refused (a lock on $ATLAS_MAPS/.run_bakes.lock), so
# chunks are never baked twice. FOREGROUND=1 runs it in this terminal instead.
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
LOG="$ATLAS_MAPS/run_bakes.log"
LOCK="$ATLAS_MAPS/.run_bakes.lock"
mkdir -p "$ATLAS_MAPS"

# Launch: refuse while another runner holds the lock, else start detached and say where the log is.
if [ -z "${RUN_BAKES_CHILD:-}" ]; then
  if ! flock -n "$LOCK" true; then
    echo "a runner is already baking into $ATLAS_MAPS (pid $(cat "$LOCK.pid" 2>/dev/null || echo '?')); log: $LOG" >&2
    exit 1
  fi
  if [ -n "${FOREGROUND:-}" ]; then
    RUN_BAKES_CHILD=1 exec flock -n "$LOCK" bash "$0" "$@"
  fi
  RUN_BAKES_CHILD=1 setsid nohup flock -n "$LOCK" bash "$0" "$@" >>"$LOG" 2>&1 </dev/null &
  sleep 1
  if ! kill -0 $! 2>/dev/null; then echo "the runner exited at once; see $LOG" >&2; tail -3 "$LOG" >&2; exit 1; fi
  echo "baking into $ATLAS_MAPS, pid $(cat "$LOCK.pid" 2>/dev/null || echo $!); log: $LOG"
  echo "  follow: tail -f $LOG    stop after the current chunk: touch $ATLAS_MAPS/STOP"
  exit 0
fi
echo $$ >"$LOCK.pid"
trap 'rm -f "$LOCK.pid"' EXIT
log() { echo "$(date -u +%FT%TZ) $*"; }
# A STOP left by the last run would end this one before its first chunk.
[ -f "$ATLAS_MAPS/STOP" ] && { rm -f "$ATLAS_MAPS/STOP"; log "removed a STOP left by an earlier run"; }
AREAS=("$@")
[ ${#AREAS[@]} -gt 0 ] || AREAS=(cook-strait iceland reunion bonifacio seto salish cape)

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
  stopped && { log "STOP found: ending after $area; log: $LOG"; exit 0; }
done
log "all areas done: ${AREAS[*]}; log: $LOG"
