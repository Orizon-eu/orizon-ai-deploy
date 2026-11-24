# Production Deployment (Ubuntu)

One command installs the host prerequisites and the `orizonctl` CLI; the CLI
then asks a dozen questions, writes every env file the stack needs, downloads
the model weights and brings the containers up in the right order.

```bash
curl -fsSL https://gitlab.syneto.eu/orizon-ai/orizon-ai-deploy/-/raw/main/install.sh | sudo bash
```

## 0. Distribution model

Three things reach a customer host, and the application source is not one of them:

| What | Where it lives | Visibility | How it gets to the host |
|---|---|---|---|
| `install.sh` | public project `orizon-ai/orizon-ai-deploy` on GitLab | public | `curl` |
| `orizonctl` + the deployment files it carries (compose files, env templates, nginx templates, helper scripts) | same public project, `orizonctl/`; released as tags `vX.Y.Z` | public | `pip install git+…@vX.Y.Z#subdirectory=orizonctl` into `/opt/orizon/venv` |
| container images | Harbor: pushed to `harbor.syneto.eu/orizon-ai`, pulled through the proxy cache `scr.syneto.eu/orizon-ai/orizon-ai` | private | `docker pull`, after the `docker login` the wizard performs with a robot account |
| application source (this monorepo, four submodules) | `gitlab.syneto.eu/orizon-ai/*` | private | never |

The deployment files are copied out of this monorepo into the CLI package by
`deploy/orizonctl/sync-assets.sh`; a test fails when the copy is stale.
`orizonctl init` writes them to `/opt/orizon/app`, so the host ends up with the
same `docker-compose.yml` and `env.example*` a developer has, and nothing else.
Publishing a release is `deploy/publish-installer.sh vX.Y.Z` (section 9).

**Do we need a separate public project at all?** No. The CLI lives at
`deploy/orizonctl` in this monorepo, and pip can install it straight from
here with a read-only deploy token, so the alternative is to keep everything
private and hand each customer a token:

```bash
export ORIZON_CLI_SOURCE='git+https://gitlab+deploy-token-42:<token>@gitlab.syneto.eu/orizon-ai/orizon-ai-app.git@{version}#subdirectory=deploy/orizonctl'
curl -fsSL --header "PRIVATE-TOKEN: <token>" \
  'https://gitlab.syneto.eu/api/v4/projects/orizon-ai%2Forizon-ai-app/repository/files/install.sh/raw?ref=main' \
  | sudo -E bash
```

The trade-off: a token on every customer machine (rotate it when a customer
leaves) and the monorepo's docs and internal notes reachable with it, against
one more project to publish to. Cloning the root repository onto the host is
not needed in either case: the CLI bundles the deployment files, and the root
repository without its submodules holds nothing else a host uses. The separate
public project is the recommended default because it needs no credentials for
anything but the image registry.

The registry and project are one `.env` value, `ORIZON_IMAGE_PREFIX`: every
image is `${ORIZON_IMAGE_PREFIX}/<name>:${ORIZON_VERSION}`. It is always host
**plus** project path, never the bare host (Harbor rejects a push to it with
`400 Bad Request`). Two prefixes matter:

| | prefix | used by |
|---|---|---|
| pull (deployments) | `scr.syneto.eu/orizon-ai/orizon-ai` | `orizonctl`, the compose defaults. Project `orizon-ai` on `scr.syneto.eu` is a Harbor **proxy cache** of `harbor.syneto.eu`, and Harbor exposes proxied images as `<host>/<proxy project>/<upstream path>`, hence the repeated segment. Login: a robot account with pull on that project. |
| push (releases) | `harbor.syneto.eu/orizon-ai` | `build-images.sh` (its default). A proxy project is read-only; the script refuses to push to `scr.syneto.eu`. |

A bare word (`sadramoghadam`) is a Docker Hub namespace and still works for the
older public builds. The first pull of a tag through the proxy fetches it from
the origin and caches it; later pulls are served locally.

## 1. Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu Server 22.04 | Ubuntu Server 24.04 |
| CPU / RAM | 8 cores / 32 GB | 16 cores / 64 GB |
| Disk | 50 GB for images, logs and the database | + 100 GB on `MODELS_PATH` for local models |
| GPU | none (external model endpoints) | one NVIDIA card, 24 GB+ VRAM, driver installed |
| Network | outbound HTTPS to Docker Hub, PyPI, HuggingFace | inbound 80/443 only if you terminate TLS here |

The installer installs Docker and the NVIDIA Container Toolkit itself. It does
**not** install the GPU driver: that needs a reboot, so it prints the command
(`sudo ubuntu-drivers install --gpgpu && sudo reboot`) and stops offering local
models until a driver is present.

## 2. What `install.sh` does

Every step checks before it changes anything, so re-running is safe.

1. Verifies Ubuntu and root.
2. `apt install` of `git curl python3-venv iproute2 pciutils lsof` and friends.
3. Docker Engine + Compose plugin from `download.docker.com` when missing; adds the invoking user to the `docker` group.
4. With a GPU present: NVIDIA Container Toolkit from NVIDIA's apt repository, then `nvidia-ctk runtime configure --runtime=docker` and a Docker restart.
5. Creates `/opt/orizon/venv`, `pip install`s `orizonctl` from `ORIZON_CLI_SOURCE` at `ORIZON_VERSION`, links it at `/usr/local/bin/orizonctl`, and records the source in `/opt/orizon/config.json` so `orizonctl update` can fetch later releases the same way.
6. `orizonctl init`: writes the bundled deployment files into `/opt/orizon/app`.
7. Starts `orizonctl setup` on your terminal; with `--config FILE` it runs `orizonctl setup --non-interactive --config FILE` instead (section 5); with `ORIZON_INSTALL_ONLY=1` it prints a preflight and stops.

On a host that is already deployed (`/opt/orizon/app/.orizon/state.json` exists), re-running the installer only replaces the deployment files: it reinstalls `orizonctl` from the source recorded at first install, runs `orizonctl init`, and stops before step 7. Env files, containers and images are not touched and nothing is asked; `sudo orizonctl up` applies the new files. `ORIZON_RECONFIGURE=1` runs the setup wizard again instead.

Tunables are environment variables read by the script (`sudo -E` passes them
through), or the `install:` section of a config file (section 5):

```bash
ORIZON_VERSION=v1.2.0  curl -fsSL …/install.sh | sudo -E bash     # a specific release (default: latest = main)
ORIZON_HOME=/srv/orizon …                                          # install root
ORIZON_CLI_SOURCE='orizonctl=={pyversion}' …                       # if the CLI is ever published to PyPI / a package registry
ORIZON_CLI_SOURCE=/home/me/orizon-ai-app/deploy/orizonctl …        # from a private clone (developers)
ORIZON_INSTALL_ONLY=1 …                                            # install, do not start setup
ORIZON_SKIP_DOCKER=1 / ORIZON_SKIP_NVIDIA=1 …                      # never install Docker / the NVIDIA toolkit
curl -fsSL …/install.sh | sudo bash -s -- --config /root/orizon.yaml   # everything from one file, no prompts
```

| variable | `install:` key | |
|---|---|---|
| `ORIZON_CONFIG` | | the config file (same as `--config`); a path or a URL |
| `ORIZON_HOME` | `home` | install root, default `/opt/orizon` |
| `ORIZON_VERSION` | `version` | release, default `latest` |
| `ORIZON_CLI_SOURCE` | `cli_source` | pip spec template for the CLI |
| `ORIZON_INSTALL_ONLY` | `install_only` | install, print a preflight, stop |
| `ORIZON_INTERACTIVE` | `interactive` | with a config file: still run the wizard on the terminal, pre-filled |
| `ORIZON_NO_PULL` | `no_pull` | `setup --no-pull` |
| `ORIZON_SKIP_DOCKER` | `skip_docker` | |
| `ORIZON_SKIP_NVIDIA` | `skip_nvidia` | |

A variable set in the environment wins over the file. `install.sh --config
FILE --print-config` shows what the installer would use and stops (it does
not need root).

`ORIZON_CLI_SOURCE` is a pip requirement with `{version}` (tag, `v1.2.0`) and
`{pyversion}` (`1.2.0`) placeholders. The default is the public deploy
project at the tag; `latest` maps to its `main` branch.

## 3. `orizonctl setup`

`setup` is `preflight` → `configure` → model download → `up` → verification.
Each part is also its own command, so anything can be re-run on its own.

### 3.1 Preflight

```
[  ok  ] Operating system            Ubuntu 24.04.2 LTS
[  ok  ] Architecture                x86_64
[  ok  ] Memory                      125 GB
[  ok  ] Docker                      engine 28.3.2
[  ok  ] Docker Compose              v2.39.1
[  ok  ] NVIDIA driver               570.86; [0] NVIDIA H100 80GB HBM3 79 GiB
[  ok  ] NVIDIA container toolkit    1.17.8
[  ok  ] Docker nvidia runtime       configured
[  ok  ] /dev/fuse                   present (backend SMB mounts)
[  ok  ] App directory               /opt/orizon/app
[  ok  ] Registry access             scr.syneto.eu/orizon-ai/orizon-ai/orizon-ai-backend:v1.2.0_dev
[ FAIL ] Port 3000                   needed for frontend; in use by node (pid 4123) on *
        Stop that service or pick another port in `orizonctl configure`.
```

A failed Docker, Compose or app-directory check stops `up` outright. Any other
failure (memory, a port, the GPU, `/dev/fuse`, registry access) is a question
in an interactive session, "Continue anyway?", and a stop in a non-interactive
one unless `--skip-preflight` is given. Ports
already published by an `orizon-*` container do not count as conflicts, so
re-running over a live stack is fine. `orizonctl preflight --gpu-smoke` also
runs `nvidia-smi` inside a throwaway container, which is the one test that
proves Docker can hand out the GPU.

### 3.2 Configure

The wizard's questions, and what each one writes:

| Section | Asks | Writes |
|---|---|---|
| Release | image tag | `ORIZON_VERSION`, `COMPOSE_PROJECT_NAME=orizon-ai-app` |
| Container registry | image prefix (registry host + project path); whether a login is needed; username + token | `ORIZON_IMAGE_PREFIX`; runs `docker login <host>` (the credential stays in `/root/.docker/config.json`, never in an env file); probes `<prefix>/orizon-ai-backend:<version>` with `docker manifest inspect` |
| Models | text / embedding / rerank: **local** (bundled vLLM on this GPU) or **external** (OpenAI-compatible URL + model + optional key); Orizon Transcribe on/off (speech-to-text and diarization servers); `MODELS_PATH` | `DEFAULT_MODEL`, `LLM_API_URL`, `*_MODE`, the `.env.vllm` copies, `EMBEDDING_API_URL`, `RERANK_MODEL` (empty = off), `COMPOSE_PROFILES` (`transcription` when on, empty when off) and `ORIZON_FEATURE_TRANSCRIPTION` (`false` when off, so the tab is not offered) |
| GPU layout | tensor parallel size, context length, which card each secondary server and the diarization server use, memory fraction per vLLM server | `TENSOR_PARALLEL_SIZE`, `VLLM_MAX_MODEL_LEN`, `*_GPU_DEVICE`, `*_GPU_MEMORY_UTILIZATION` |
| Access | `letsencrypt` (nginx + Cloudflare DNS-01), `custom-cert` (nginx + your PEMs), `external-proxy` (your TLS terminator in front of nginx on :80), `direct` (plain HTTP via nginx on :80), `direct-no-nginx` (rare: plain HTTP on the frontend port, public API reachable from this host only); nginx runs in every mode but the last | `NGINX_DOMAIN`, `NGINX_TEMPLATE`, `CERTBOT_*`, `CLOUDFLARE_API_TOKEN`, `SSL_CERT_DIR`, `BETTER_AUTH_URL`, `FRONTEND_URLS`, `SECURE_COOKIES`, `ORIZON_MCP_OAUTH_REDIRECT_URI` |
| Ports | frontend, backend, postgres, each local model server (defaults kept unless you say otherwise) | the `*_PORT` keys in `.env` and `.env.nginx`, `FASTAPI_URL` and each local endpoint URL |
| Database | nothing: a random password is generated on the first run and kept afterwards | `POSTGRES_*`, `DATABASE_URL` |
| Secrets | nothing | `BETTER_AUTH_SECRET`, `ORIZON_INTERNAL_API_KEY`, `ORIZON_MCP_ENCRYPTION_KEY`, `WEBHOOK_API_KEY`: generated once, **kept on every later run** |
| Connectors | MCP stack on/off | `ORIZON_SMB_MOUNT_BASE_DIR`; adds `docker-compose.mcp.yml` to `up` |
| Transcription | HuggingFace token; whether to connect the Telegram bot, and if so its token, API id/hash and username | `HUGGINGFACE_TOKEN`, `BOT_TOKEN`, `API_ID`, `API_HASH` in `.env.secrets`; `ORIZON_FEATURE_TELEGRAM` and `NEXT_PUBLIC_TELEGRAM_BOT_USERNAME` in `.env` |
| Model weights | download now? Asked on a host without an NVIDIA GPU too (default no): the shipped default models are then fetched under `MODELS_PATH` for a GPU added later | nothing; drives the download step |

Every generated file starts from the shipped template (`env.example*`) or,
on a re-run, from your current file, and only the keys above are set. Comments
and everything you edited by hand survive. The files and their modes:

```
.env          0640   the app + what Compose reads at parse time
.env.secrets  0600   on-disk credentials only
.env.vllm     0644   the bundled model servers
.env.mcp      0644   the MCP connectors (template defaults)
.env.nginx    0600   nginx, certbot, setup-ssl.sh (carries the Cloudflare token)
```

The GPU memory fractions of every server sharing a card are rescaled to sum to
0.90 when the defaults do not fit (text 0.50 + embedding 0.10 + rerank 0.12 +
transcription 0.40 does not), and the resulting table is printed per card.

Runtime settings and most credentials are **not** asked here: they live in the
admin panel at **Settings > System**, encrypted in the database. The wizard
only offers to store an inference API key so a fresh install can answer its
first chat; move it into the panel later with
`backend-orizon-ai/scripts/import_secrets.py` (see `docs/RUNTIME_CONFIGURATION.md`).

### 3.3 Up

`orizonctl up` (part of `setup`):

1. creates the bind-mount directories, loads `fuse`, runs `setup-sandbox-host.sh` when the MCP stack is on;
2. `docker compose config` on the generated files, so a typo fails here rather than mid-start;
3. proves the registry is reachable and the login valid (`docker manifest inspect` on the backend image), then pulls the images at `ORIZON_VERSION`;
4. starts **db** and waits for its healthcheck;
5. starts the **model servers** (only those configured local: an external text endpoint does not drag `vllm-server` in);
6. starts **frontend** (which runs the Prisma migrations) and **backend**, then the transcription bot;
7. starts **nginx**; in Let's Encrypt mode runs `setup-ssl.sh`, which issues the certificate through Cloudflare, switches `NGINX_TEMPLATE` to HTTPS and starts **certbot**;
8. starts the **MCP stack** as a second compose project on the same network;
9. waits for every container's healthcheck, `/health` on the backend, the frontend and `/nginx-health`, and prints the URL.

Model servers get 30 minutes: a first boot reads tens of GB of weights from
disk. A container that exits or turns unhealthy stops the wait and its last
80 log lines are printed.

## 4. Day-2 operations

```bash
orizonctl status                 # both compose projects
orizonctl doctor                 # containers, endpoints, GPU in vllm, disk, cert expiry
orizonctl logs backend -f        # any service; --mcp for the connector stack
orizonctl restart nginx
orizonctl down                   # keeps volumes; --volumes deletes db + certs (asks twice)

orizonctl backup                 # snapshot: db dump, env files, deployment files, client data,
                                 # certificates -> /opt/orizon/backups/<ts>_manual_<version>/
orizonctl backup list | prune    # what is there; keep the newest N
orizonctl update --latest        # move to the newest release in the registry (section 4.1)
orizonctl update --version v1.3.0
orizonctl rollback               # back to the snapshot the last update took (section 4.2)
orizonctl init                   # re-write the deployment files from the installed orizonctl
orizonctl configure              # change anything; secrets and the DB password are kept
orizonctl env show               # effective config, secrets masked (--reveal to show)
orizonctl env diff               # keys a newer template added that your files lack
orizonctl models list|download   # weights under MODELS_PATH
orizonctl ssl issue|status       # Let's Encrypt (re)issue via setup-ssl.sh; served cert expiry
orizonctl systemd install        # daily log cleanup timer (cleanup-logs.sh)
orizonctl info                   # paths, git ref, what is configured
```

Anything `orizonctl` does can also be done with `docker compose` in
`/opt/orizon/app`: the project name is fixed to `orizon-ai-app` through
`COMPOSE_PROJECT_NAME` in `.env`, so the two agree.

### 4.1 Updating

```bash
sudo orizonctl update --latest                 # newest plain release tag in the registry (v1.3.0, not v1.3.0_dev)
sudo orizonctl update --version v1.3.0         # a specific release
sudo orizonctl update                          # same release: re-pull and restart
sudo orizonctl update --latest --non-interactive --set NEW_KEY=value   # unattended
```

In order, and it stops at the first failure with the previous state intact:

1. **Target.** `--version`, or the registry's newest release for `--latest` (the tags of `<prefix>/orizon-ai-backend` are listed with the stored `docker login`). Nothing to do when the host already runs it.
2. **Snapshot** into `/opt/orizon/backups/<ts>_update_<from>/`: `pg_dump -Fc` of the database, every `.env*` file plus `.orizon/state.json`, the deployment files of the running release (compose files, templates, nginx, scripts), an archive of the client data directories (`backend-orizon-ai/data`, `transcription-bot-orizon-ai/data`, `certs`; logs are not included) and, in Let's Encrypt mode, the certificate volume copied out of the running nginx. `--no-data` skips the data archive on a large deployment; `--no-backup` skips the whole thing and with it the rollback.
3. **Images** at the new tag are pulled while the old stack still runs: a wrong tag, a missing push or an expired registry login is found here, before anything on disk changes.
4. **orizonctl** is pip-upgraded to the same release of the deploy project and the rest of the update runs under the new binary (`--no-cli-upgrade` keeps the current one).
5. **Deployment files** of the new release are written; files you edited by hand are kept and reported (`--force-assets` replaces them, saving yours under `.orizon/local-edits/`).
6. **Env files** are carried across the new templates, file by file. The env files hold both values the wizard set and values you set by hand, so nothing is regenerated: each file is re-based on its new template with every value you had put back in place, which is why new keys show up in their documented spot with their comments. What you are asked:
   - a **new key** (in the new template, not in your file): its help text from the template is shown and a value is asked, the template's default offered. Unattended: `--set KEY=VALUE`, else the template default.
   - an **existing key whose default moved** between the two templates: keep yours (default), take the new default, or type a value. `--keep-existing` skips these questions.
   - **any other key** you name at the "change another key?" prompt, as `KEY` (shows the current value, asks) or `KEY=VALUE`.
   - a key the new template **no longer declares** is kept under the "set by orizonctl" marker and listed; keys that were never in a template (the wizard's routing keys, your additions) are kept silently.
   Your previous files are in the snapshot.
7. `ORIZON_VERSION` is bumped, the state saved, and `up` does its phased restart and verification. Database migrations run when the new frontend starts.

A failed step after the snapshot prints the rollback command (and asks on a terminal). The snapshot's manifest records `applied` when the update went through.

#### One service on another image

To redeploy a single component (a backend hotfix, a test build) without moving the release, name the service:

```bash
sudo orizonctl update backend --version v1.3.1-hotfix          # another tag of the same image
sudo orizonctl update backend --image scr.syneto.eu/orizon-ai/orizon-ai/orizon-ai-backend:test-7   # any image
sudo orizonctl update backend                                  # re-pull its current tag and restart it
sudo orizonctl update backend frontend --version v1.3.1        # several services, one tag
sudo orizonctl update backend --unpin                          # back to the release's image (ORIZON_VERSION)
```

Service names are the Compose ones (`orizonctl info` lists them; connectors such as `mcp-github` work too). What happens: a snapshot as above (`--no-backup`, `--no-data` apply), the image is pulled while the old container still runs, then only the named containers are recreated (`up -d --no-deps`) and waited on. `ORIZON_VERSION`, `orizonctl`, the deployment files and the env files are not touched.

The choice is recorded in `.orizon/state.json` and rendered into a Compose override file, `.orizon/overrides.yml` (`overrides.mcp.yml` for connectors), which every later `orizonctl` command loads after the main compose file, so `up`, `restart` and `pull` keep the service on its image. `orizonctl info` shows what is pinned. A release update (`update --version` / `--latest`) drops the pins and moves every service to the release; `--keep-pins` keeps them. Plain `docker compose` in `/opt/orizon/app` does not read the override file unless you pass it with `-f`.

#### A container the release does not have

`update SERVICE` only moves services the installed compose files define. To run one they do not define (a connector a later release ships, on a host that stays on this one; a customer-specific tool):

```bash
sudo orizonctl service add mcp-imap --port 8111                        # <prefix>/orizon-mcp-imap at ORIZON_VERSION
sudo orizonctl service add mcp-imap --port 8111 --version v1.4.0_dev   # another tag of that image
sudo orizonctl service add my-tool --image reg/team/tool:1 --port 9000 --env MODE=prod
sudo orizonctl service list
sudo orizonctl service remove mcp-imap
```

A name starting with `mcp-` joins the connector stack and is set up like the bundled connectors: `.env.mcp`, `MCP_PORT`, `MCP_CRED_VARS` (`--cred-vars`), the port health check, and the wiring the backend needs, i.e. `MCP_<X>_URL=http://mcp-<x>:<port>/mcp` in `.env` and its two entries in `MCP_ALLOWED_HOSTS` in `.env.mcp` (`--no-wire` skips both; `remove` takes back what `add` wrote). The backend reads its URL key at start, so follow with `orizonctl up`, or with `orizonctl update backend --version ...` when the connector needs a newer backend anyway. Anything else joins the main stack on `orizon-network` with the image, port and environment given. The port is not published on the host unless `--publish`.

The service is recorded in `.orizon/state.json` and rendered into the same override files, so it starts and stops with the stack, `orizonctl update NAME --image ...` moves it, and a rollback restores it. When a release update brings a compose file that defines the service itself, the added copy is dropped and the release's takes over.

#### orizonctl itself, without a new release

A fix to the CLI or to its bundled files, published again under the same tag (`publish-installer.sh` moves the tag) or sitting on a branch, reaches a host with:

```bash
sudo orizonctl self-update                 # the CLI source at the release this host runs (ORIZON_VERSION)
sudo orizonctl self-update --ref main      # a branch or another tag
```

It force-reinstalls the package into `/opt/orizon/venv` (a republished tag has the same package version, which a plain upgrade would skip), then the new copy writes its bundled deployment files into the app directory, keeping any you edited. Images, `ORIZON_VERSION`, env files and running containers are not touched; `orizonctl up` applies whatever changed in the files.

### 4.2 Rolling back

```bash
sudo orizonctl backup list                     # the snapshots, oldest first
sudo orizonctl rollback                        # the newest snapshot an update took
sudo orizonctl rollback 20260922_101500_update_v1.2.0
sudo orizonctl rollback <name> --db-only       # only the database, into the running db container
sudo orizonctl rollback <name> --no-db         # files, config and images only
```

`rollback` stops both stacks, puts the env files, state and deployment files back, extracts the client data archive over the current directories (files created after the snapshot are never deleted; files the snapshot holds are restored), starts `db` alone and restores the dump over it (`pg_restore --clean --if-exists`, other sessions terminated first), moves orizonctl back to the snapshot's release when it came from a different one (`--no-cli` keeps the current one), pulls the previous images if they are no longer on the host (`--no-pull` to skip), does the phased start, and copies the certificates back into the certbot container in Let's Encrypt mode. A snapshot taken by `orizonctl backup` (kind `manual`) restores the same way. Snapshots are directories; `backup prune --keep 5` deletes the oldest.

### Going from staging to a real certificate

1. `orizonctl configure`, answer **no** to the staging question (everything else keeps its value);
2. `orizonctl ssl issue`: `setup-ssl.sh` sees the staging certificate, deletes it and issues a production one.

### Restoring a database dump by hand

```bash
docker compose -p orizon-ai-app exec -T db \
  pg_restore -U postgres -d orizon --clean --if-exists < /opt/orizon/backups/<snapshot>/db.dump
```

## 5. Unattended installs: the config file

One YAML file carries the whole install. `install:` is read by `install.sh`
(section 2's tunables); `configure:` answers the wizard's questions, one key
per question, and is what `orizonctl setup` and `configure` read.

```bash
curl -fsSL https://gitlab.syneto.eu/orizon-ai/orizon-ai-deploy/-/raw/main/install.sh \
  | sudo bash -s -- --config /root/orizon.yaml          # install, then `orizonctl setup --non-interactive --config`
sudo orizonctl setup --non-interactive --config /root/orizon.yaml     # on a host where install.sh already ran
sudo orizonctl configure --config /root/orizon.yaml                   # re-configure; answered questions are not asked
sudo orizonctl config check /root/orizon.yaml                         # typos, and what each side will read
sudo orizonctl config example                                         # the bundled examples
sudo orizonctl config example gpu-letsencrypt > /root/orizon.yaml     # start from one
```

The examples ship inside `orizonctl` (`deploy/orizonctl/orizonctl/examples/`
in the repository):

| example | what it is |
|---|---|
| `reference` | every key, its default and a comment; the schema |
| `gpu-letsencrypt` | one GPU host, local text/embedding/rerank, HTTPS from Let's Encrypt via Cloudflare DNS-01, connectors on |
| `cpu-external` | no GPU, every model from an OpenAI-compatible endpoint, plain HTTP on the LAN |
| `custom-cert` | two GPUs, your own certificate, external reranker, transcription bot on |
| `external-proxy` | behind your own TLS terminator (forwarding to nginx on :80), local models |

The rules: a key left out falls back to the value already in the host's env
files, then to the default; a required value with no default (a domain in
Let's Encrypt mode, an external URL) is an error rather than a guess; secrets
left out are generated on the first run and kept afterwards. Unknown keys are
reported, since a misspelt section would otherwise fall back silently. The
file holds credentials: keep it `0600`. With `interactive: true` under
`install:`, the installer still opens the wizard on the terminal and the file
only pre-fills it. `--config` also takes the older bare JSON answers file (the
keys of `configure:` at the top level, no sections); `--answers` remains an
alias.

```yaml
install:
  version: v1.2.0                  # release to install; `latest` = the deploy project's main branch
  home: /opt/orizon
  install_only: false              # true: install Docker + orizonctl, print a preflight, stop
  interactive: false               # true: wizard on the terminal, pre-filled from this file

configure:
  version: v1.2.0                  # image tag (ORIZON_VERSION)
  registry:
    prefix: scr.syneto.eu/orizon-ai/orizon-ai
    username: "robot$orizon-ai-scr"
    token: "…"
  models:
    text: local                    # local | external
    text_model: Qwen/Qwen3.6-35B-A3B-FP8
    embedding: local
    rerank: local                  # local | external | off
    transcription: false
  models_path: /srv/orizon-models
  gpu:
    tensor_parallel: 1
    max_model_len: 65536
    embedding_device: "0"
    rerank_device: "0"
    fractions: {text: 0.70, embedding: 0.08, rerank: 0.10}
  access:
    mode: letsencrypt              # letsencrypt | custom-cert | external-proxy | direct
    domain: ai.example.com
    email: ops@example.com
    cloudflare_token: "…"
    staging: true
  ports: {frontend: 3000, backend: 8005, postgres: 55432, vllm: 8001}
  database: {password: "…"}
  secrets: {better_auth_secret: "…", internal_api_key: "…", master_key: "…", webhook_api_key: "…"}
  mcp: true
  telegram_bot_username: ""
  transcription: {bot_token: "…", api_id: "…", api_hash: "…", hf_token: "…"}
  hf_token: "…"
  download_models: true
```

External endpoints instead of local servers:

```yaml
configure:
  models: {text: external, embedding: external, rerank: off}
  external:
    text:      {url: https://api.openai.com, model: gpt-4o, api_key: "…"}
    embedding: {url: http://emb.internal:8000, model: nomic-ai/nomic-embed-text-v2-moe}
  access: {mode: external-proxy, public_url: https://ai.corp.example}
```

`registry`: `prefix` (default `scr.syneto.eu/orizon-ai/orizon-ai`, the Syneto proxy cache; a bare
word such as `sadramoghadam` is a Docker Hub namespace); give `username` and
`token` for the login (a Harbor robot account with pull on the project). Unattended
runs log in only when credentials are given or `login: true` is set.

Access modes: `letsencrypt` (needs `domain`, `email`, `cloudflare_token`),
`custom-cert` (needs `domain`, `cert_dir` with `fullchain.pem` + `privkey.pem`),
`external-proxy` (needs `public_url`), `direct` (`public_url`, default
`http://<host ip>`), `direct-no-nginx` (`public_url`, default
`http://<host ip>:<frontend port>`; rare, nginx is not started).

Full flow on a fresh machine, no prompts, in two steps instead of one:

```bash
ORIZON_INSTALL_ONLY=1 curl -fsSL .../install.sh | sudo -E bash
sudo orizonctl setup --non-interactive --config /root/orizon.yaml
```

## 6. Layout on the host

```
/opt/orizon/
  app/              the deployment files written by `orizonctl init` (compose files, templates,
                    scripts) plus the generated .env* files and the data/ and logs/ bind mounts
  app/.orizon/      state.json (which parts are on, access mode, version; no secrets),
                    assets.json (hash of every file init wrote, to detect your edits),
                    local-edits/<ts>/ (your copies, when --force-assets replaced them)
  venv/             orizonctl and its two dependencies (click, huggingface_hub)
  config.json       where orizonctl was installed from, for `update`
  orizon.yaml       only when install.sh was given a config URL: the downloaded file (0600)
  backups/<ts>_<kind>_<version>/   one snapshot: manifest.json, db.dump, env/, files.tar.gz,
                    data.tar.gz, letsencrypt/ (section 4.2); kind is `update` or `manual`
/usr/local/bin/orizonctl
/etc/profile.d/orizon.sh          exports ORIZON_HOME
/etc/apparmor.d/rootlesskit       only when the MCP stack is on (Ubuntu 23.10+)
/etc/systemd/system/orizon-log-cleanup.{service,timer}   after `orizonctl systemd install`
```

The CLI also works against a developer checkout of the monorepo:
`orizonctl --app-dir ~/orizon-ai-app …`, or run it inside that directory. Files
it did not write itself are never overwritten there.

## 7. Troubleshooting

| Symptom | Where to look |
|---|---|
| `Port 3000 … in use` in preflight | `ss -ltnp \| grep 3000`; change the port in `orizonctl configure` |
| `NVIDIA driver … nvidia-smi is missing` | driver not installed or not loaded after an update: `sudo ubuntu-drivers install --gpgpu && sudo reboot` |
| `Registry access … unauthorized` / `manifest unknown` | wrong `ORIZON_IMAGE_PREFIX` or tag, or the login expired: `orizonctl configure` redoes `docker login`; the robot account needs pull on the proxy project |
| `Docker nvidia runtime … runtimes: io.containerd.runc.v2, runc` | `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `vllm-server unhealthy` with CUDA OOM in the logs | lower `VLLM_MAX_MODEL_LEN` or the fractions in the GPU section of `orizonctl configure`, then `orizonctl up` |
| `sandbox-dind` restart-looping on `fork/exec /proc/self/exe` | `./setup-sandbox-host.sh` (run by `up` when MCP is on); see `apparmor/rootlesskit` |
| frontend crash-loops with Prisma `P3005` | a database created before migration history existed: see `db-migration.sh` on the deploy branch |
| sign-in loops back to the login page | `BETTER_AUTH_URL` does not match the URL in the browser: fix the access section in `orizonctl configure`, then `orizonctl up` (the frontend image bakes it in, so `up` recreates the container) |
| certificate still staging | `orizonctl configure` → staging **no** → `orizonctl ssl issue` |
| `Compose bind mount … node_modules … absent` warning | the app directory is a source checkout with dev mounts; production uses the bundled `docker-compose.yml` (no such mounts) and developers add them with `docker-compose.dev.yml` |

`orizonctl doctor` is the first thing to run: it checks the same list a person
would, in one screen.

## 8. Relationship to the older `deploy.sh`

`deploy.sh` on the `deploy` branch is the previous, single-script installer. It
cloned this repository onto the host, predates the five-file env split and the
settings panel, references services that no longer ship, and re-asks every
question on every run. `install.sh` + `orizonctl` replace it. `setup-ssl.sh`,
`setup-sandbox-host.sh`, `cleanup-logs.sh` and `db-migration.sh` are unchanged
and are called by the CLI where relevant.

## 9. Releasing (maintainers)

A release is one tag, `vX.Y.Z`, in three places: the image registry, this
monorepo, and the public deploy project.

```bash
# 1. images, from the monorepo at the release commit (ORIZON_IMAGE_PREFIX and
#    ORIZON_VERSION in .env decide where they go; pushes go to the ORIGIN,
#    deployments pull through the scr.syneto.eu proxy cache)
docker login harbor.syneto.eu
./build-images.sh --version vX.Y.Z --with-cuda

# 2. keep the CLI's bundled deployment files in step with the monorepo
./deploy/orizonctl/sync-assets.sh          # copies compose/templates/scripts into the package
(cd deploy/orizonctl && pytest -q)         # includes the "bundle is in sync" check
git commit -am "Release vX.Y.Z"

# 3. publish install.sh + orizonctl/ to the public project and tag it
git clone git@gitlab.syneto.eu:orizon-ai/orizon-ai-deploy.git ../orizon-ai-deploy   # once
./deploy/publish-installer.sh vX.Y.Z ../orizon-ai-deploy --push
```

`publish-installer.sh` refuses to run on a stale bundle, runs the tests, sets
the package version, `rsync`s `install.sh`, `deploy/orizonctl/` and this
document into the public clone, commits `Release vX.Y.Z` and tags it. Customers
then get it with `ORIZON_VERSION=vX.Y.Z … install.sh` or
`orizonctl update --version vX.Y.Z`.

What must never be in the public project: anything under `backend-orizon-ai/`,
`frontend-orizon-ai/`, `mcp-servers/` (other than the two sandbox files the
compose file bind-mounts), `transcription-bot-orizon-ai/`, or a filled-in
`.env*`. The bundle list in `sync-assets.sh` is the allowlist.
