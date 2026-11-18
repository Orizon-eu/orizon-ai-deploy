#!/usr/bin/env bash
#
# Daily log cleanup for the OrizonAI stack.
#
# Two independent jobs, because logs land in two places:
#
#   1. Docker container stdout -> /var/lib/docker/containers/<id>/<id>-json.log
#      The json-file driver has no size cap unless one is configured, so a single
#      chatty service can consume the whole disk. Containers created before the
#      `logging:` block existed in docker-compose.yml keep unbounded logs for
#      their entire lifetime, so they still need truncating here.
#
#   2. Application log files written into the bind-mounted logs/ directories.
#      These are never rotated by anything.
#
# Truncation uses `truncate -s 0`, which keeps the inode the Docker daemon holds
# open. Deleting the file instead would leave the daemon writing to an unlinked
# inode: space is never reclaimed and the log silently stops working.
#
# Usage:
#   ./cleanup-logs.sh              # clean using the defaults below
#   ./cleanup-logs.sh --dry-run    # report what would be cleaned, change nothing
#
# Overridable via the environment:
#   MAX_CONTAINER_LOG_MB   truncate container logs larger than this  (default 200)
#   APP_LOG_RETENTION_DAYS delete app log files older than this      (default 14)

set -uo pipefail

MAX_CONTAINER_LOG_MB="${MAX_CONTAINER_LOG_MB:-200}"
APP_LOG_RETENTION_DAYS="${APP_LOG_RETENTION_DAYS:-14}"
DOCKER_CONTAINERS_DIR="${DOCKER_CONTAINERS_DIR:-/var/lib/docker/containers}"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_LOG_DIRS=(
  "$REPO_DIR/backend-orizon-ai/logs"
  "$REPO_DIR/transcription-bot-orizon-ai/logs"
)

log() { echo "[cleanup-logs] $*"; }

human() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "${1}B"; }

# Resolve a container id to its compose service name for readable output.
container_name() {
  docker inspect --format '{{.Name}}' "$1" 2>/dev/null | sed 's|^/||' || echo "$1"
}

reclaimed=0

# --- 1. Container stdout logs ------------------------------------------------
if [[ -d "$DOCKER_CONTAINERS_DIR" ]]; then
  while IFS= read -r -d '' logfile; do
    size=$(stat -c %s "$logfile" 2>/dev/null) || continue
    cid=$(basename "$(dirname "$logfile")")
    name=$(container_name "$cid")

    if (( DRY_RUN )); then
      log "would truncate $name: $(human "$size")"
    else
      if truncate -s 0 "$logfile" 2>/dev/null; then
        log "truncated $name: reclaimed $(human "$size")"
        reclaimed=$(( reclaimed + size ))
      else
        log "WARNING: could not truncate $logfile (need root?)"
      fi
    fi
  done < <(find "$DOCKER_CONTAINERS_DIR" -name '*-json.log' \
             -size "+${MAX_CONTAINER_LOG_MB}M" -print0 2>/dev/null)
else
  log "skipping container logs: $DOCKER_CONTAINERS_DIR not present"
fi

# --- 2. Application log files ------------------------------------------------
for dir in "${APP_LOG_DIRS[@]}"; do
  [[ -d "$dir" ]] || continue

  while IFS= read -r -d '' f; do
    size=$(stat -c %s "$f" 2>/dev/null) || continue

    if (( DRY_RUN )); then
      log "would delete $(basename "$f"): $(human "$size"), $(( ($(date +%s) - $(stat -c %Y "$f")) / 86400 ))d old"
    else
      if rm -f "$f" 2>/dev/null; then
        reclaimed=$(( reclaimed + size ))
      else
        log "WARNING: could not delete $f"
      fi
    fi
  done < <(find "$dir" -type f \
             \( -name '*.log' -o -name '*.log.*' -o -name '*.yaml' \) \
             -mtime "+${APP_LOG_RETENTION_DAYS}" -print0 2>/dev/null)

  # Drop directories left empty once their files aged out (e.g. logs/chat_logs
  # subdirectories for chats that are long finished).
  (( DRY_RUN )) || find "$dir" -mindepth 1 -type d -empty -delete 2>/dev/null
done

if (( DRY_RUN )); then
  log "dry run complete, nothing changed"
else
  log "done, reclaimed $(human "$reclaimed")"
fi

if [[ -d "$DOCKER_CONTAINERS_DIR" ]]; then
  df -h "$DOCKER_CONTAINERS_DIR" 2>/dev/null | tail -1 \
    | awk '{print "[cleanup-logs] disk: "$5" used, "$4" available on "$6}'
fi

# Always succeed: an unreadable log or a missing docker dir must not mark the
# systemd unit failed, or the timer stops being trusted and nobody notices.
exit 0
