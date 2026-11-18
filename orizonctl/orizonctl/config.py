"""The deployment config file: one YAML (or JSON) document for the whole
install.

Two top-level sections. `install:` is what `install.sh` reads (where to
install, which release, whether to run the wizard); `configure:` is what the
wizard reads (the answers to its questions, one key per question). Either may
be left out. A document with neither key is taken as a bare answers mapping,
which is what the older `--answers file.json` accepted, so those files keep
working unchanged.

Bundled examples live next to this module under `examples/` and are printed by
`orizonctl config example`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import click

from . import ui


class ConfigError(click.ClickException):
    pass


# What install.sh understands under `install:`. The value is the environment
# variable each key maps to; the shell parser in install.sh mirrors this list.
INSTALL_KEYS: dict[str, str] = {
    "home": "ORIZON_HOME",
    "version": "ORIZON_VERSION",
    "cli_source": "ORIZON_CLI_SOURCE",
    "install_only": "ORIZON_INSTALL_ONLY",
    "interactive": "ORIZON_INTERACTIVE",
    "skip_docker": "ORIZON_SKIP_DOCKER",
    "skip_nvidia": "ORIZON_SKIP_NVIDIA",
    "no_pull": "ORIZON_NO_PULL",
}

# What the wizard reads under `configure:` (see wizard.py, the `self.a(...)`
# lookups). Unknown keys are reported, since a misspelt section silently
# falls back to the default and the error surfaces much later.
CONFIGURE_KEYS: dict[str, set[str] | None] = {
    "version": None,
    "registry": {"prefix", "login", "username", "token"},
    "models": {"text", "text_model", "embedding", "embedding_model", "rerank", "rerank_model", "transcription", "transcription_model"},
    "external": {"text", "embedding", "rerank"},
    "models_path": None,
    "gpu": {"tensor_parallel", "max_model_len", "embedding_device", "rerank_device", "transcription_device", "diarization_device", "fractions"},
    "access": {"mode", "domain", "email", "cloudflare_token", "staging", "cert_dir", "public_url"},
    "ports": {"frontend", "backend", "postgres", "vllm", "embedding_vllm", "rerank_vllm", "transcription_vllm", "diarization", "nginx_http", "nginx_https"},
    "database": {"password"},
    "secrets": {"better_auth_secret", "internal_api_key", "master_key", "webhook_api_key"},
    "telegram_bot_username": None,
    "mcp": None,
    "mcp_profiles": None,
    "smb_mount_dir": None,
    "transcription": {"hf_token", "telegram", "bot_token", "api_id", "api_hash"},
    "hf_token": None,
    "download_models": None,
}

SECTIONS = ("install", "configure")


@dataclass
class Config:
    path: Path
    install: dict[str, Any] = field(default_factory=dict)
    configure: dict[str, Any] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)

    def warn_unknown(self) -> None:
        for k in self.unknown:
            ui.warn(f"{self.path.name}: unknown key '{k}' (ignored; check the spelling against `orizonctl config example reference`)")


# ------------------------------------------------------------------ parsing


def _parse(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            return json.loads(text)
        except ValueError as e:
            raise ConfigError(f"{path}: not valid JSON: {e}")
    try:
        import yaml
    except ImportError:  # pragma: no cover - pyyaml is a declared dependency
        if suffix in (".yaml", ".yml"):
            raise ConfigError(f"{path}: reading YAML needs the pyyaml package (pip install pyyaml)")
        try:
            return json.loads(text)
        except ValueError as e:
            raise ConfigError(f"{path}: not valid JSON: {e}")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: not valid YAML: {e}")


def _unknown_keys(configure: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k, v in configure.items():
        if k not in CONFIGURE_KEYS:
            out.append(f"configure.{k}")
            continue
        sub = CONFIGURE_KEYS[k]
        if sub is not None and isinstance(v, dict):
            out.extend(f"configure.{k}.{s}" for s in v if s not in sub)
    return out


def load_config(path: Path) -> Config:
    """Read a YAML or JSON deployment config. The result's `configure` mapping
    is what the wizard takes as its answers."""
    data = _parse(path)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the document must be a mapping (key: value), got {type(data).__name__}")

    if any(k in data for k in SECTIONS):
        install = data.get("install") or {}
        configure = data.get("configure") or {}
        unknown = [k for k in data if k not in SECTIONS]
    else:
        # bare answers mapping (the pre-YAML `--answers` layout)
        install, configure, unknown = {}, data, []

    for name, sec in (("install", install), ("configure", configure)):
        if not isinstance(sec, dict):
            raise ConfigError(f"{path}: '{name}' must be a mapping, got {type(sec).__name__}")
    # YAML 1.1 reads a bare `off` (and on/yes/no) as a boolean, so `rerank: off`
    # arrives as False. Put the word back; "off" is a choice, not a flag.
    models = configure.get("models")
    if isinstance(models, dict) and models.get("rerank") is False:
        models["rerank"] = "off"
    unknown += [f"install.{k}" for k in install if k not in INSTALL_KEYS]
    unknown += _unknown_keys(configure)
    return Config(path=path, install=install, configure=configure, unknown=unknown)


# ---------------------------------------------------------------- examples


def _examples_root():
    return resources.files("orizonctl") / "examples"


def example_names() -> list[str]:
    root = _examples_root()
    if not root.is_dir():
        return []
    return sorted(p.name[: -len(".yaml")] for p in root.iterdir() if p.name.endswith(".yaml"))


def example_text(name: str) -> str:
    p = _examples_root() / f"{name}.yaml"
    if not p.is_file():
        raise ConfigError(f"no example named '{name}'; one of: {', '.join(example_names())}")
    return p.read_text(encoding="utf-8")
