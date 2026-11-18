#!/bin/bash
# ============================================================================
# setup-sandbox-host.sh — prepare a host to run the code sandbox (mcp-sandbox)
#
# Grants RootlessKit the one AppArmor permission it needs to build a user
# namespace, which is what the rootless Docker daemon in the sandbox-dind
# sidecar is started inside. Without it that container restart-loops on:
#
#   [rootlesskit:parent] error: failed to start the child:
#   fork/exec /proc/self/exe: operation not permitted
#
# SAFE TO RUN ON EVERY HOST. It decides for itself whether the fix applies and
# changes nothing where it does not — no AppArmor, no restriction, or the
# profile already loaded all exit 0 having written nothing. Idempotent, so
# running it on every deploy is fine.
#
# WHERE IT APPLIES. Ubuntu 23.10 and later set
# kernel.apparmor_restrict_unprivileged_userns=1, which transitions anything
# asking for an unprivileged user namespace into a profile that denies
# CAP_SYS_ADMIN — the capability RootlessKit needs to create that namespace.
# Ubuntu 22.04 and earlier have no such sysctl and need nothing. Non-AppArmor
# distributions (RHEL, Fedora, SUSE) have nowhere to put the profile and are
# not affected.
#
# The transition is decided by the HOST kernel from the binary's path, so it
# cannot be fixed from inside the container: `privileged: true` does not exempt
# it and neither does any `security_opt:`. It has to be a host profile.
#
# Usage:
#   ./setup-sandbox-host.sh            install if needed, then verify
#   ./setup-sandbox-host.sh --check    report only, change nothing
#
# Exit codes: 0 nothing to do or success, 1 failed to apply, 2 --check found
# the fix is needed but not installed (for provisioning that wants to fail).
# ============================================================================

set -euo pipefail

CHECK_ONLY=false
[ "${1:-}" = "--check" ] && CHECK_ONLY=true

PROFILE_SRC="$(dirname "$0")/apparmor/rootlesskit"
PROFILE_DST="/etc/apparmor.d/rootlesskit"
SYSCTL="kernel.apparmor_restrict_unprivileged_userns"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

say() { echo "[sandbox-host] $*"; }

# ---------------------------------------------------------------------------
# 1. Does this host need the fix at all?
# ---------------------------------------------------------------------------

if ! command -v apparmor_parser >/dev/null 2>&1; then
    say "No AppArmor on this host — nothing to do."
    exit 0
fi

RESTRICT="$(sysctl -n "$SYSCTL" 2>/dev/null || echo "")"
if [ "$RESTRICT" != "1" ]; then
    say "$SYSCTL is ${RESTRICT:-absent} — the restriction is not in force, nothing to do."
    exit 0
fi

say "$SYSCTL=1 — RootlessKit needs the profile on this host."

# ---------------------------------------------------------------------------
# 2. Is it already in place?
# ---------------------------------------------------------------------------

if [ ! -f "$PROFILE_SRC" ]; then
    say "ERROR: $PROFILE_SRC not found. Run this from the repository root."
    exit 1
fi

if [ -f "$PROFILE_DST" ] && cmp -s "$PROFILE_SRC" "$PROFILE_DST"; then
    say "Profile already installed and current."
    ALREADY=true
else
    ALREADY=false
    if [ "$CHECK_ONLY" = true ]; then
        say "Profile is MISSING or out of date at $PROFILE_DST."
        say "Run this script without --check to install it."
        exit 2
    fi
    say "Installing $PROFILE_DST ..."
    $SUDO cp "$PROFILE_SRC" "$PROFILE_DST"
    $SUDO apparmor_parser -r "$PROFILE_DST"
    say "Loaded. It is re-read from /etc/apparmor.d at every boot."
fi

# ---------------------------------------------------------------------------
# 3. Prove it works, rather than assume the parser succeeding means it took.
#
# The profile attaches by executable path, and the binary lives inside the
# sandbox-dind image — so whether it matches is a question about this host that
# only running it can answer. Run RootlessKit exactly as the entrypoint does:
# as the unprivileged `rootless` user, which is the case the restriction bites.
# ---------------------------------------------------------------------------

if ! command -v docker >/dev/null 2>&1; then
    say "docker not available here — skipping verification."
    exit 0
fi

COMPOSE="$(dirname "$0")/docker-compose.mcp.yml"
IMAGE="docker:27-dind-rootless"
if [ -f "$COMPOSE" ]; then
    FOUND="$(grep -oE 'docker:[0-9]+-dind-rootless' "$COMPOSE" | head -1 || true)"
    [ -n "$FOUND" ] && IMAGE="$FOUND"
fi

say "Verifying with $IMAGE ..."
if OUT="$(timeout 60 docker run --rm --privileged --user rootless "$IMAGE" \
            rootlesskit echo ok 2>&1)" && [ "${OUT##*$'\n'}" = "ok" ]; then
    say "OK — RootlessKit can create its user namespace."
    say "Start the sandbox:  docker compose -f docker-compose.mcp.yml up -d sandbox-dind"
    exit 0
fi

say "FAILED — RootlessKit still cannot create its user namespace:"
echo "$OUT" | tail -3 | sed 's/^/    /'
say ""
say "Check the host audit log for what denied it:"
say "  journalctl -k --since -2min | grep -i userns"
say ""
say "If the profile is not attaching, the fallback is to lift the restriction"
say "host-wide, which is blunter — it turns the protection off for every process:"
say "  echo '$SYSCTL=0' | sudo tee /etc/sysctl.d/99-rootlesskit.conf"
say "  sudo sysctl --system"
[ "$ALREADY" = true ] && exit 1
exit 1
