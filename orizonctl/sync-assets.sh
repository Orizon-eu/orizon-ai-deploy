#!/usr/bin/env bash
# Copy the deployment files a host needs out of the monorepo and into the CLI
# package, so a wheel built from deploy/orizonctl carries everything
# `orizonctl init` materialises under /opt/orizon/app. The private source tree
# never has to be on the customer's machine.
#
#   ./deploy/orizonctl/sync-assets.sh            # copy
#   ./deploy/orizonctl/sync-assets.sh --check    # exit 1 if the copy is stale
#
# tests/test_assets.py runs the --check, so a change to docker-compose.yml that
# is not synced fails the test suite rather than shipping a stale bundle.
#
# The env templates are orizonctl's own (BUNDLE_OWNED): edited in the bundle,
# never copied from the repo root, whose env.example* stay the developer copies.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DEST="$HERE/orizonctl/assets"

# One line per file, relative to the repo root. Directories are recreated.
ASSETS=(
  docker-compose.yml
  docker-compose.mcp.yml
  nginx/orizonai-http-only.conf.template
  nginx/orizonai.conf.template
  nginx/orizonai-custom-cert.conf.template
  nginx/orizon/http.conf
  nginx/orizon/routes.conf
  nginx/orizon/tls.conf
  setup-ssl.sh
  setup-sandbox-host.sh
  cleanup-logs.sh
  apparmor/rootlesskit
  certbot/Dockerfile
  certbot/renew.sh
  mcp-servers/ensure-sandbox-image.sh
  mcp-servers/Dockerfile.sandbox-runtime
)

BUNDLE_OWNED=(
  env.example
  env.example.secrets
  env.example.vllm
  env.example.mcp
  env.example.nginx
)

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

stale=0
for rel in "${ASSETS[@]}"; do
  src="$ROOT/$rel"; dst="$DEST/$rel"
  if [ ! -f "$src" ]; then
    echo "missing in repo: $rel" >&2
    stale=1
    continue
  fi
  if [ $CHECK -eq 1 ]; then
    if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"; then
      echo "stale: $rel"
      stale=1
    fi
  else
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
  fi
done

for rel in "${BUNDLE_OWNED[@]}"; do
  if [ ! -f "$DEST/$rel" ]; then
    echo "missing in bundle: $rel" >&2
    stale=1
  fi
done

# anything in the bundle that is no longer on the list
while IFS= read -r -d '' f; do
  rel="${f#"$DEST"/}"
  [ "$rel" = "MANIFEST" ] && continue
  found=0
  for a in "${ASSETS[@]}" "${BUNDLE_OWNED[@]}"; do [ "$a" = "$rel" ] && found=1 && break; done
  if [ $found -eq 0 ]; then
    if [ $CHECK -eq 1 ]; then echo "orphan in bundle: $rel"; stale=1; else rm -f "$f"; fi
  fi
done < <(find "$DEST" -type f -print0 2>/dev/null)

if [ $CHECK -eq 0 ]; then
  printf '%s\n' "${ASSETS[@]}" "${BUNDLE_OWNED[@]}" > "$DEST/MANIFEST"
  echo "synced ${#ASSETS[@]} files into $DEST"
elif [ $stale -eq 0 ]; then
  echo "bundle is in sync (${#ASSETS[@]} files)"
fi
exit $stale
