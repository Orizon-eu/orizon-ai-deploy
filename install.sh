#!/usr/bin/env bash
# =============================================================================
# Orizon AI — production installer for Ubuntu
#
#   curl -fsSL https://gitlab.syneto.eu/orizon-ai/orizon-ai-deploy/-/raw/main/install.sh | sudo bash
#
# The application source is private and never lands on the customer's machine.
# This script installs the public `orizonctl` CLI, which carries the compose
# files and templates a host needs, and pulls the images from the container
# registry with the credentials the wizard asks for.
#
# What it does, in order, and nothing more:
#   1. checks it is on Ubuntu (22.04+) with root
#   2. installs the small set of host packages it needs (git, curl, python3-venv, ...)
#   3. installs Docker Engine + Compose plugin from download.docker.com if absent
#   4. if an NVIDIA GPU is present: installs the NVIDIA Container Toolkit if absent
#      and wires it into Docker. It never installs the GPU driver itself (that
#      needs a reboot); it tells you the command.
#   5. installs `orizonctl` into $ORIZON_HOME/venv from $ORIZON_CLI_SOURCE, links it
#      at /usr/local/bin, records the source for later `orizonctl update`
#   6. `orizonctl init`: writes the deployment files into $ORIZON_HOME/app
#   7. on a terminal, hands over to `orizonctl setup` (which checks the host, asks
#      its questions, then runs the full preflight against the answers); with
#      ORIZON_INSTALL_ONLY=1 it prints a preflight and stops instead
#
# It asks nothing itself: when piped from curl there is no stdin to ask on. The
# interactive part is orizonctl, which is started on /dev/tty when there is one.
#
# Unattended: one YAML file carries both the installer's tunables (`install:`)
# and the wizard's answers (`configure:`):
#
#   curl -fsSL <url>/install.sh | sudo bash -s -- --config /root/orizon.yaml
#
# `orizonctl config example` prints the examples (reference, gpu-letsencrypt,
# cpu-external, custom-cert, external-proxy). With a config file the script
# ends in `orizonctl setup --non-interactive --config <file>` instead of the
# wizard, unless the file says `interactive: true`. The file may also be a URL.
#
# Tunables (environment variables; use `sudo -E` to pass them through). Each
# has a key under `install:` in the config file; the variable wins when both
# are set:
#   ORIZON_CONFIG=<file|url>       the config file (same as --config)
#   ORIZON_HOME=/opt/orizon        install root (app files, venv, backups)         home
#   ORIZON_VERSION=latest          release to install, e.g. v1.2.0 (a tag of the   version
#                                  deploy project)
#   ORIZON_CLI_SOURCE=<template>   pip spec for the CLI; {version} and {pyversion}  cli_source
#                                  are substituted. Default: the public deploy
#                                  project on GitLab, or, when this script is run
#                                  from a repository checkout, the deploy/orizonctl
#                                  directory next to it. A local path works too.
#   ORIZON_INSTALL_ONLY=1          do not start setup at the end                   install_only
#   ORIZON_INTERACTIVE=1           with a config file: still run the wizard on     interactive
#                                  the terminal, pre-filled from the file
#   ORIZON_NO_PULL=1               setup does not pull images (`setup --no-pull`)  no_pull
#   ORIZON_SKIP_DOCKER=1           never install Docker (fail if it is missing)    skip_docker
#   ORIZON_SKIP_NVIDIA=1           never touch the NVIDIA toolkit                  skip_nvidia
#   ORIZON_RECONFIGURE=1           on an existing deployment: run the setup        reconfigure
#                                  wizard again instead of only replacing assets
#
# Re-running is safe: every step checks before it changes anything.
#
# On a host that is already deployed ($ORIZON_HOME/app/.orizon/state.json
# exists) the run only replaces the deployment files: the CLI is reinstalled
# from the source recorded at first install (so a re-published tag's assets
# arrive), `orizonctl init` writes the bundled files, and the script stops.
# The .env files, the running containers and the images are not touched and
# nothing is asked; apply the new files with `sudo orizonctl up`.
# =============================================================================

{ # this brace group ensures the whole script is downloaded before it runs

set -euo pipefail

usage() {
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    sed -n '2,/^# ====*$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed '$d'
  else
    echo "usage: install.sh [--config <file|url>] [--print-config]   (environment: ORIZON_CONFIG, ORIZON_HOME, ORIZON_VERSION, ...)"
  fi
}

# -----------------------------------------------------------------------------
# 0. Arguments and the config file
# -----------------------------------------------------------------------------
# `curl ... | sudo bash -s -- --config FILE` hands the arguments through to a
# piped script; ORIZON_CONFIG does the same for `sudo -E`.
CONFIG_FILE="${ORIZON_CONFIG:-}"
PRINT_CONFIG=0
while [ $# -gt 0 ]; do
  case "$1" in
    -c|--config) [ $# -ge 2 ] || { echo "--config needs a file" >&2; exit 2; }; CONFIG_FILE="$2"; shift 2 ;;
    --config=*) CONFIG_FILE="${1#--config=}"; shift ;;
    --print-config) PRINT_CONFIG=1; shift ;;   # parse the file, show what would be used, stop
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

# The scalars directly under `install:` of a YAML config, as "key<TAB>value"
# lines. Deliberately small: a flat block of `key: value`, quotes and trailing
# comments stripped. Everything under `configure:` is orizonctl's business,
# and it parses the whole file properly (pyyaml) once it is installed.
yaml_install_section() {
  awk '
    /^[^[:space:]#]/ { in_sec = ($0 ~ /^install:[[:space:]]*(#.*)?$/); next }
    in_sec && /^[[:space:]]+[A-Za-z_][A-Za-z0-9_]*[[:space:]]*:/ {
      line = $0
      sub(/^[[:space:]]+/, "", line)
      key = line; sub(/[[:space:]]*:.*/, "", key)
      val = line; sub(/^[^:]*:[[:space:]]*/, "", val)
      if (val ~ /^"/)       { sub(/^"/, "", val); sub(/".*$/, "", val) }
      else if (val ~ /^'\''/)  { sub(/^'\''/, "", val); sub(/'\''.*$/, "", val) }
      else                  { sub(/[[:space:]]+#.*$/, "", val); sub(/^#.*$/, "", val); sub(/[[:space:]]+$/, "", val) }
      printf "%s\t%s\n", key, val
    }
  ' "$1"
}

# "true/yes/on/1" -> 1, everything else -> 0
yaml_bool() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) echo 1 ;;
    *) echo 0 ;;
  esac
}

CONFIG_TMP=""
if [ -n "$CONFIG_FILE" ]; then
  case "$CONFIG_FILE" in
    http://*|https://*)
      CONFIG_TMP="$(mktemp)"; chmod 600 "$CONFIG_TMP"
      curl -fsSL "$CONFIG_FILE" -o "$CONFIG_TMP" || { echo "cannot download config $CONFIG_FILE" >&2; exit 2; }
      CONFIG_URL="$CONFIG_FILE"; CONFIG_FILE="$CONFIG_TMP"
      ;;
  esac
  [ -r "$CONFIG_FILE" ] || { echo "config file not readable: $CONFIG_FILE" >&2; exit 2; }
  CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"
  # An explicit environment variable beats the file; the file beats the default.
  while IFS=$'\t' read -r key val; do
    case "$key" in
      home)         [ -n "${ORIZON_HOME:-}" ]         || ORIZON_HOME="$val" ;;
      version)      [ -n "${ORIZON_VERSION:-}" ]      || ORIZON_VERSION="$val" ;;
      cli_source)   [ -n "${ORIZON_CLI_SOURCE:-}" ]   || ORIZON_CLI_SOURCE="$val" ;;
      install_only) [ -n "${ORIZON_INSTALL_ONLY:-}" ] || ORIZON_INSTALL_ONLY="$(yaml_bool "$val")" ;;
      interactive)  [ -n "${ORIZON_INTERACTIVE:-}" ]  || ORIZON_INTERACTIVE="$(yaml_bool "$val")" ;;
      no_pull)      [ -n "${ORIZON_NO_PULL:-}" ]      || ORIZON_NO_PULL="$(yaml_bool "$val")" ;;
      skip_docker)  [ -n "${ORIZON_SKIP_DOCKER:-}" ]  || ORIZON_SKIP_DOCKER="$(yaml_bool "$val")" ;;
      skip_nvidia)  [ -n "${ORIZON_SKIP_NVIDIA:-}" ]  || ORIZON_SKIP_NVIDIA="$(yaml_bool "$val")" ;;
      reconfigure)  [ -n "${ORIZON_RECONFIGURE:-}" ]  || ORIZON_RECONFIGURE="$(yaml_bool "$val")" ;;
      *) echo "  warn install.${key}: not an installer key (ignored; see --help)" >&2 ;;
    esac
  done < <(yaml_install_section "$CONFIG_FILE")
fi

ORIZON_HOME="${ORIZON_HOME:-/opt/orizon}"
ORIZON_VERSION="${ORIZON_VERSION:-latest}"
EXISTING=0
if [ -f "$ORIZON_HOME/app/.orizon/state.json" ] && [ "${ORIZON_RECONFIGURE:-0}" != "1" ]; then
  EXISTING=1
fi
# Where the CLI comes from. Explicit ORIZON_CLI_SOURCE wins. Otherwise, when this
# script is run from a repository checkout (a developer or a private clone), the
# CLI sitting next to it at deploy/orizonctl is used, so no public project is
# needed. Piped from curl there is no file location, and the public deploy
# project is the default.
# The default is assigned via a helper variable on purpose: it contains
# "{version}", and the "}" in it would end a ${VAR:-default} expansion early.
DEFAULT_CLI_SOURCE='git+https://gitlab.syneto.eu/orizon-ai/orizon-ai-deploy.git@{version}#subdirectory=orizonctl'
CLI_FROM_CHECKOUT=0
if [ -z "${ORIZON_CLI_SOURCE:-}" ] && [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$_here/deploy/orizonctl/pyproject.toml" ]; then
    ORIZON_CLI_SOURCE="$_here/deploy/orizonctl"
    CLI_FROM_CHECKOUT=1
  fi
fi
# A re-run keeps installing from wherever the first install did (a private
# token URL, a mirror), not from the public default.
if [ -z "${ORIZON_CLI_SOURCE:-}" ] && [ -f "$ORIZON_HOME/config.json" ]; then
  ORIZON_CLI_SOURCE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("cli_source", ""))' "$ORIZON_HOME/config.json" 2>/dev/null || true)"
fi
ORIZON_CLI_SOURCE="${ORIZON_CLI_SOURCE:-$DEFAULT_CLI_SOURCE}"
APP_DIR="$ORIZON_HOME/app"
VENV_DIR="$ORIZON_HOME/venv"
BIN_LINK="/usr/local/bin/orizonctl"

if [ "$PRINT_CONFIG" = "1" ]; then
  printf 'config:        %s\n' "${CONFIG_URL:-${CONFIG_FILE:-(none)}}"
  printf 'home:          %s\n' "$ORIZON_HOME"
  printf 'version:       %s\n' "$ORIZON_VERSION"
  printf 'cli_source:    %s\n' "$ORIZON_CLI_SOURCE"
  printf 'install_only:  %s\n' "${ORIZON_INSTALL_ONLY:-0}"
  printf 'interactive:   %s\n' "${ORIZON_INTERACTIVE:-0}"
  printf 'no_pull:       %s\n' "${ORIZON_NO_PULL:-0}"
  printf 'skip_docker:   %s\n' "${ORIZON_SKIP_DOCKER:-0}"
  printf 'skip_nvidia:   %s\n' "${ORIZON_SKIP_NVIDIA:-0}"
  [ -n "$CONFIG_TMP" ] && rm -f "$CONFIG_TMP"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive

if [ -t 1 ]; then
  BLD=$'\033[1m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; BLU=$'\033[34m'; RST=$'\033[0m'
else
  BLD=""; GRN=""; YLW=""; RED=""; BLU=""; RST=""
fi
step() { printf '\n%s==> %s%s\n' "$BLD" "$*" "$RST"; }
ok()   { printf '%s  ok%s  %s\n' "$GRN" "$RST" "$*"; }
info() { printf '%s  ..%s  %s\n' "$BLU" "$RST" "$*"; }
warn() { printf '%s  warn%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '%s  ERROR%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# Resolve the pip spec for one version: {version} -> v1.2.0, {pyversion} -> 1.2.0.
# "latest" maps to the default branch of a git source (@main) or no pin otherwise.
resolve_spec() {
  local tmpl="$1" ver="$2" pyver
  if [ "$ver" = "latest" ]; then
    case "$tmpl" in
      git+*) ver="main"; pyver="" ;;
      *)     ver="";     pyver="" ;;
    esac
  else
    pyver="${ver#v}"
  fi
  local out="${tmpl//\{version\}/$ver}"
  out="${out//\{pyversion\}/$pyver}"
  # a PyPI-style "name=={pyversion}" with no version collapses to "name"
  out="${out%==}"
  printf '%s' "$out"
}

# -----------------------------------------------------------------------------
# 1. Where are we
# -----------------------------------------------------------------------------
step "Orizon AI installer"
info "home: $ORIZON_HOME   version: $ORIZON_VERSION"
[ -n "$CONFIG_FILE" ] && info "config: ${CONFIG_URL:-$CONFIG_FILE}"
if [ "$CLI_FROM_CHECKOUT" = "1" ]; then
  info "cli:  $ORIZON_CLI_SOURCE  (this checkout)"
else
  info "cli:  $ORIZON_CLI_SOURCE"
fi

[ "$(uname -s)" = "Linux" ] || die "this installer targets Ubuntu Linux (got $(uname -s))."
[ "$(id -u)" -eq 0 ] || die "run as root:  curl -fsSL <url> | sudo bash"

# shellcheck disable=SC1091
if [ -r /etc/os-release ]; then . /etc/os-release; else ID=unknown; VERSION_ID=0; PRETTY_NAME="unknown"; fi
case "${ID:-}" in
  ubuntu)
    major="${VERSION_ID%%.*}"
    if [ "${major:-0}" -lt 22 ]; then
      warn "Ubuntu ${VERSION_ID} is older than 22.04; continuing, but this is untested."
    fi
    ok "${PRETTY_NAME}"
    ;;
  *)
    warn "${PRETTY_NAME:-$ID} is not Ubuntu. The apt steps below assume Ubuntu repositories; continuing anyway."
    ;;
esac

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) ok "architecture: $ARCH" ;;
  aarch64|arm64) warn "architecture: $ARCH. The bundled vLLM images are amd64 only; plan on external model endpoints." ;;
  *) warn "architecture: $ARCH (untested)" ;;
esac

# A clock that is behind makes apt reject every signed Release file as "not
# valid yet" and TLS handshakes fail; common on VMs restored from a snapshot.
if command -v timedatectl >/dev/null 2>&1 && [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = "no" ]; then
  warn "system clock is not NTP-synchronised ($(date -u +%Y-%m-%dT%H:%MZ)). If apt or TLS fails below: sudo timedatectl set-ntp true && sudo systemctl restart systemd-timesyncd"
fi

# apt-get update with a readable failure instead of a silent exit under set -e
apt_update() {
  local log; log="$(mktemp)"
  if apt-get update -qq 2>"$log"; then rm -f "$log"; return 0; fi
  cat "$log" >&2
  if grep -q "not valid yet" "$log"; then
    rm -f "$log"
    die "apt rejects the repository metadata because this machine's clock is behind. Fix the time (sudo timedatectl set-ntp true; sudo systemctl restart systemd-timesyncd) and re-run."
  fi
  rm -f "$log"
  die "apt-get update failed; fix the repository problem above and re-run."
}

# -----------------------------------------------------------------------------
# 2. Host packages
# -----------------------------------------------------------------------------
step "Host packages"
need_pkgs=()
for p in ca-certificates curl git gnupg lsb-release python3 python3-venv python3-pip iproute2 pciutils lsof; do
  dpkg -s "$p" >/dev/null 2>&1 || need_pkgs+=("$p")
done
if [ ${#need_pkgs[@]} -gt 0 ]; then
  info "installing: ${need_pkgs[*]}"
  apt_update
  apt-get install -y -qq "${need_pkgs[@]}" >/dev/null
fi
# python3-venv is versioned on Ubuntu (python3.12-venv); make sure venv really works
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  pyver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  info "installing python${pyver}-venv"
  apt-get install -y -qq "python${pyver}-venv" >/dev/null || true
fi
ok "git $(git --version | awk '{print $3}'), $(python3 --version)"

# -----------------------------------------------------------------------------
# 3. Docker
# -----------------------------------------------------------------------------
step "Docker"
install_docker() {
  info "installing Docker Engine from download.docker.com"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-$(lsb_release -cs)}}"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt_update
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

if command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1; then
  ok "docker $(docker version --format '{{.Server.Version}}') already installed"
elif command -v docker >/dev/null 2>&1; then
  info "docker binary present but the daemon is not answering; starting it"
  systemctl enable --now docker || die "could not start the docker daemon (systemctl status docker)."
  docker version >/dev/null 2>&1 || die "docker daemon still not answering."
  ok "docker $(docker version --format '{{.Server.Version}}')"
elif [ "${ORIZON_SKIP_DOCKER:-0}" = "1" ]; then
  die "docker is not installed and ORIZON_SKIP_DOCKER=1."
else
  install_docker
  ok "docker $(docker version --format '{{.Server.Version}}') installed"
fi

if ! docker compose version >/dev/null 2>&1; then
  info "installing the docker compose plugin"
  apt-get install -y -qq docker-compose-plugin 2>/dev/null || die "docker compose plugin missing and could not be installed. Is the Docker apt repository configured?"
fi
ok "docker compose $(docker compose version --short)"

# let the invoking user run docker without sudo afterwards
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && ! id -nG "$SUDO_USER" | tr ' ' '\n' | grep -qx docker; then
  usermod -aG docker "$SUDO_USER" && info "added $SUDO_USER to the docker group (takes effect at next login)"
fi

# -----------------------------------------------------------------------------
# 4. NVIDIA
# -----------------------------------------------------------------------------
step "NVIDIA GPU"
GPU_PRESENT=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  GPU_PRESENT=1
  ok "driver $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1): $(nvidia-smi -L | sed 's/ (UUID.*//' | paste -sd ';' -)"
elif lspci 2>/dev/null | grep -qi nvidia; then
  warn "an NVIDIA device is on the PCI bus but no working driver was found."
  warn "install one and reboot, then re-run this installer:"
  warn "    sudo ubuntu-drivers install --gpgpu && sudo reboot"
  warn "(or the vendor .run / CUDA repository driver). Continuing without GPU: local models will not be offered;"
  warn "the wizard still asks whether to download the model weights now."
else
  info "no NVIDIA GPU (nvidia-smi): the wizard offers external model endpoints only, and asks whether to download the model weights anyway"
fi

if [ "$GPU_PRESENT" = "1" ] && [ "${ORIZON_SKIP_NVIDIA:-0}" != "1" ]; then
  if ! command -v nvidia-ctk >/dev/null 2>&1; then
    info "installing the NVIDIA Container Toolkit"
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt_update
    apt-get install -y -qq nvidia-container-toolkit
  fi
  ok "nvidia-ctk $(nvidia-ctk --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    info "registering the nvidia runtime with Docker"
    nvidia-ctk runtime configure --runtime=docker >/dev/null
    systemctl restart docker
  fi
  ok "docker nvidia runtime configured"
fi

# -----------------------------------------------------------------------------
# 5. orizonctl
# -----------------------------------------------------------------------------
step "orizonctl"
mkdir -p "$ORIZON_HOME"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR" || die "python3 -m venv failed (apt install python3-venv)."
fi
SPEC="$(resolve_spec "$ORIZON_CLI_SOURCE" "$ORIZON_VERSION")"
if [ -d "$SPEC" ]; then
  # A local source directory: pip builds in-tree, which would drop a root-owned
  # build/ into the checkout and, worse, a stale build/lib shadows later edits.
  # Build from a throwaway copy instead.
  _tmp="$(mktemp -d)"
  cp -r "$SPEC/." "$_tmp/"
  rm -rf "$_tmp/build" "$_tmp"/*.egg-info "$_tmp/.pytest_cache"
  find "$_tmp" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  PIP_TARGET="$_tmp"
else
  PIP_TARGET="$SPEC"
fi
info "pip install $SPEC"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade "$PIP_TARGET" \
  || die "pip install failed. The CLI source must be reachable from this host (and PyPI, for click and huggingface_hub). Override with ORIZON_CLI_SOURCE, e.g. a checkout's deploy/orizonctl directory."
# pip skips a package whose version is already installed, and the assets ride
# inside it: a re-published tag carries new files under the same version.
if [ "$EXISTING" = "1" ]; then
  "$VENV_DIR/bin/python" -m pip install --quiet --force-reinstall --no-deps "$PIP_TARGET" \
    || die "pip reinstall of orizonctl failed."
fi
[ -d "${_tmp:-}" ] && rm -rf "$_tmp"
ln -sf "$VENV_DIR/bin/orizonctl" "$BIN_LINK"
ok "$("$BIN_LINK" --version) at $BIN_LINK"

# Remember where the CLI came from so `orizonctl update --version vX` can fetch
# the matching release, and make ORIZON_HOME the default for every shell.
printf '{\n  "cli_source": "%s",\n  "installed_version": "%s",\n  "installed_at": "%s"\n}\n' \
  "$ORIZON_CLI_SOURCE" "$ORIZON_VERSION" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$ORIZON_HOME/config.json"
cat > /etc/profile.d/orizon.sh <<PROFILE
export ORIZON_HOME="$ORIZON_HOME"
PROFILE
export ORIZON_HOME

# -----------------------------------------------------------------------------
# 6. Deployment files
# -----------------------------------------------------------------------------
step "Deployment files -> $APP_DIR"
"$BIN_LINK" --home "$ORIZON_HOME" --app-dir "$APP_DIR" init

if [ "$EXISTING" = "1" ]; then
  [ -n "$CONFIG_TMP" ] && rm -f "$CONFIG_TMP"
  printf '\n%sDeployment files updated.%s Env files, containers and images were not touched.\n' "$BLD" "$RST"
  printf '    sudo orizonctl up                  # apply them (restarts what changed)\n'
  printf '    sudo orizonctl init --force        # also replace files you edited by hand (copies are kept)\n'
  printf '    ORIZON_RECONFIGURE=1 ... install.sh # run the setup wizard again instead\n\n'
  exit 0
fi

# -----------------------------------------------------------------------------
# 7. Hand over
# -----------------------------------------------------------------------------
# A downloaded config is kept for orizonctl under $ORIZON_HOME (0600): the
# operator needs the same file for `orizonctl setup` and `configure` later.
if [ -n "$CONFIG_TMP" ]; then
  install -m 0600 "$CONFIG_TMP" "$ORIZON_HOME/orizon.yaml"
  rm -f "$CONFIG_TMP"
  CONFIG_FILE="$ORIZON_HOME/orizon.yaml"
  info "config saved to $CONFIG_FILE"
fi
SETUP=("$BIN_LINK" --home "$ORIZON_HOME" --app-dir "$APP_DIR" setup)
[ "${ORIZON_NO_PULL:-0}" = "1" ] && SETUP+=(--no-pull)
[ -n "$CONFIG_FILE" ] && SETUP+=(--config "$CONFIG_FILE")

if [ "${ORIZON_INSTALL_ONLY:-0}" = "1" ]; then
  step "Preflight"
  "$BIN_LINK" --home "$ORIZON_HOME" --app-dir "$APP_DIR" preflight || true
  printf '\n%sInstalled.%s Next:\n' "$BLD" "$RST"
  if [ -n "$CONFIG_FILE" ]; then
    printf '    sudo orizonctl setup --non-interactive --config %s\n\n' "$CONFIG_FILE"
  else
    printf '    sudo orizonctl setup\n\n'
  fi
  exit 0
fi

if [ -n "$CONFIG_FILE" ] && [ "${ORIZON_INTERACTIVE:-0}" != "1" ]; then
  step "Setup (unattended, from $CONFIG_FILE)"
  exec "${SETUP[@]}" --non-interactive
fi

if [ -t 1 ] && [ -r /dev/tty ] && [ -w /dev/tty ]; then
  step "Configuration"
  info "starting the setup wizard on this terminal (Ctrl-C to stop; resume any time with: sudo orizonctl setup)"
  [ -n "$CONFIG_FILE" ] && info "questions answered in $CONFIG_FILE are not asked"
  exec "${SETUP[@]}" </dev/tty
else
  printf '\n%sInstalled.%s No terminal available for the wizard. Next:\n' "$BLD" "$RST"
  printf '    sudo orizonctl setup                                          # interactive\n'
  printf '    sudo orizonctl setup --non-interactive --config orizon.yaml   # unattended (orizonctl config example)\n\n'
fi

} # end of download guard
