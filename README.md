# Orizon AI deployment

The public half of the Orizon AI production delivery: the installer and the
`orizonctl` deployment CLI. The application source stays private; what a host
needs (compose files, env templates, nginx templates, helper scripts) ships
inside `orizonctl`, and the container images come from the registry the
installer asks you for.

## Install on a new machine (Ubuntu 22.04+)

```bash
wget -qO- https://raw.githubusercontent.com/Orizon-eu/orizon-ai-deploy/main/install.sh | sudo bash
```

or with curl:

```bash
curl -fsSL https://raw.githubusercontent.com/Orizon-eu/orizon-ai-deploy/main/install.sh | sudo bash
```

The script installs Docker, the NVIDIA Container Toolkit when a GPU is present,
and `orizonctl` into `/opt/orizon/venv`, then hands over to `orizonctl setup`,
which checks the host, asks its questions and brings the stack up.

### Pin a release

Releases are git tags, `vX.Y.Z`, the same tag the images carry. The installer
fetches `orizonctl/` at that tag:

```bash
ORIZON_VERSION=v1.3.0 wget -qO- https://raw.githubusercontent.com/Orizon-eu/orizon-ai-deploy/main/install.sh | sudo -E bash
```

Without `ORIZON_VERSION` the installer takes `main`, which always holds the
latest release.

### Unattended install

One YAML file carries the installer's tunables and the wizard's answers:

```bash
sudo orizonctl config example                 # the bundled examples
wget -qO- https://raw.githubusercontent.com/Orizon-eu/orizon-ai-deploy/main/install.sh \
  | sudo bash -s -- --config /root/orizon.yaml
```

## Day-2

```
orizonctl status | logs | doctor
orizonctl update --version vX.Y.Z    # snapshot, pull, new orizonctl + files, migrate, up
orizonctl rollback                   # back to a snapshot
orizonctl backup
```

Full guide: [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md). CLI details:
[orizonctl/README.md](orizonctl/README.md).

## Layout

| Path | What |
|---|---|
| `install.sh` | the `wget \| sudo bash` entry point |
| `orizonctl/` | the CLI package (pip-installable: `pip install 'git+https://github.com/Orizon-eu/orizon-ai-deploy.git@v1.3.0#subdirectory=orizonctl'`) |
| `orizonctl/orizonctl/assets/` | the deployment files `orizonctl init` writes to `/opt/orizon/app` |
| `PRODUCTION_DEPLOYMENT.md` | the deployment guide |

This repository is published from the private monorepo by its release script;
changes land here as `Release vX.Y.Z` commits plus a tag.
