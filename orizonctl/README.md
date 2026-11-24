# orizonctl

The Orizon AI deployment CLI. Installed on the host by `install.sh` (repo root)
into `/opt/orizon/venv` and linked as `/usr/local/bin/orizonctl`.

```
orizonctl setup        # preflight -> configure -> pull -> up -> wait -> summary
orizonctl preflight    # host checks only
orizonctl configure    # (re)run the configuration wizard, writes the .env* files
orizonctl config example | check   # bundled YAML config examples; check yours for typos
orizonctl up | down | restart | status | logs | pull
orizonctl doctor       # post-deploy health report
orizonctl backup       # snapshot (db, env, deployment files, client data) into $ORIZON_HOME/backups
orizonctl update       # snapshot, pull the new images, new orizonctl + files, migrate env keys, up
orizonctl update backend --version v1.3.1   # one service on another image (--image REF, --unpin); the rest stays
orizonctl service add mcp-imap --port 8111  # a container the release's compose files lack; also remove | list
orizonctl self-update   # reinstall orizonctl + its bundled files at the running release (--ref main); no restart
orizonctl rollback     # back to a snapshot: files, database, images
orizonctl models download
orizonctl ssl issue | status
orizonctl env show | diff
orizonctl systemd install
```

Full guide: `docs/PRODUCTION_DEPLOYMENT.md`.

## Model weights

`orizonctl models download` fetches what the configuration serves locally into
`MODELS_PATH`, and `orizonctl models list` shows what is on disk. Every model
server runs offline, so nothing is fetched at start-up.

| Role | HuggingFace repo | Lands at |
|---|---|---|
| text | `DEFAULT_MODEL` (default `Qwen/Qwen3.6-35B-A3B-FP8`) | `MODELS_PATH/<repo name>` |
| embedding | `LLM_EMBEDDING_MODEL` (default `nomic-ai/nomic-embed-text-v2-moe`) | `MODELS_PATH/<repo name>` |
| rerank | `RERANK_MODEL` (default `Qwen/Qwen3-Reranker-0.6B`) | `MODELS_PATH/<repo name>` |
| transcription | `TRANSCRIPTION_MODEL` (default `Qwen/Qwen3-ASR-1.7B`) | `MODELS_PATH/<repo name>` |
| diarization | `DIARIZATION_MODEL` (default `pyannote/speaker-diarization-3.1`), gated | `MODELS_PATH/<DIARIZATION_MODEL_LOCAL_DIR>` |
| diarization | `pyannote/segmentation-3.0`, gated | `MODELS_PATH/hub` (HuggingFace cache) |
| diarization | `pyannote/wespeaker-voxceleb-resnet34-LM` | `MODELS_PATH/hub` (HuggingFace cache) |
| diarization | `pyannote/speaker-diarization-community-1` (PLDA), gated | `MODELS_PATH/hub` (HuggingFace cache) |

The diarization pipeline is a recipe that names its models by repo id; pyannote 4
also loads its PLDA from `speaker-diarization-community-1`. The diarization
server reads them from the HuggingFace cache (`HF_HOME/hub`, which is
`MODELS_PATH/hub` on the host). For the gated repos, accept the terms on
huggingface.co with the account that owns `HUGGINGFACE_TOKEN` before downloading.
`orizonctl models download --only diarization` fetches just these four.

## Testing on a workstation (macOS or Linux)

```bash
# 1. unit + integration tests (render, routing, wizard profiles, CLI)
cd deploy/orizonctl && python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest -q

# 2. the CLI against a scratch home, no repo access, as a customer host would see it
.venv/bin/pip wheel --no-deps -w /tmp/dist . && rm -rf build   # pip builds in-tree; a stale build/ shadows later edits
python3 -m venv /tmp/ov && /tmp/ov/bin/pip install /tmp/dist/orizonctl-*.whl
/tmp/ov/bin/orizonctl --home /tmp/orizon init
/tmp/ov/bin/orizonctl --home /tmp/orizon configure          # interactive wizard
/tmp/ov/bin/orizonctl --home /tmp/orizon env show
docker compose --project-directory /tmp/orizon/app config --quiet   # Compose accepts the result

# 3. install.sh on a clean Ubuntu (container borrowing Docker Desktop's daemon)
./deploy/dev/test-install-ubuntu.sh            # 24.04; pass 22.04 for the older LTS

# 4. a real `up` of the CPU-only profile under Docker Desktop (backend, frontend, db
#    images are multi-arch). Preflight fails on a non-Linux host, so skip it, and
#    give the SMB mount base a writable path via the config file (smb_mount_dir).
/tmp/ov/bin/orizonctl config example cpu-external > orizon.yaml     # then edit
/tmp/ov/bin/orizonctl --home /tmp/orizon configure --non-interactive --config orizon.yaml
/tmp/ov/bin/orizonctl --home /tmp/orizon up --skip-preflight
/tmp/ov/bin/orizonctl --home /tmp/orizon doctor
/tmp/ov/bin/orizonctl --home /tmp/orizon down
```

Development: `pip install -e '.[test]' && pytest`. After editing `docker-compose.yml`
or a template in the monorepo, run `./sync-assets.sh`; `tests/test_assets.py` fails
until the bundle matches. The config-file examples live in `orizonctl/examples/`
and are bundled with the package; `tests/test_config.py` runs each of them
through the wizard and through `install.sh --print-config`, and checks that
`config.py`'s key lists match what `wizard.py` and `install.sh` actually read.
