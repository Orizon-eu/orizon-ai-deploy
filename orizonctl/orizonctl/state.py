"""What the wizard decided, minus every secret.

The `.env*` files are the configuration; this file only records the shape of
the deployment (which optional parts are on, how the stack is reached) so that
`up`, `doctor` and `update` know which services they are responsible for
without re-deriving it from env values. Stored at `<app>/.orizon/state.json`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = ".orizon"
STATE_FILE = "state.json"

ACCESS_MODES = ("direct", "letsencrypt", "custom-cert", "external-proxy", "direct-no-nginx")


@dataclass
class State:
    version: str = ""
    ref: str = ""
    image_prefix: str = "scr.syneto.eu/orizon-ai/orizon-ai"
    registry_host: str = "scr.syneto.eu"
    registry_login: bool = True
    gpu_count: int = 0
    text_mode: str = "local"  # local | external
    embedding_mode: str = "local"  # local | external
    rerank_mode: str = "local"  # local | external | off
    transcription: bool = False
    mcp: bool = False
    mcp_profiles: list = field(default_factory=list)
    access_mode: str = "direct"
    public_url: str = ""
    domain: str = ""
    download_models: bool = False
    configured_at: str = ""
    orizonctl_version: str = ""
    extra: dict = field(default_factory=dict)

    # ---------------------------------------------------------------- queries

    @property
    def uses_gpu(self) -> bool:
        return self.text_mode == "local" or self.embedding_mode == "local" or self.rerank_mode == "local" or self.transcription

    @property
    def keeps_models(self) -> bool:
        """Model weights live under MODELS_PATH on this host: a local server
        needs them, or the operator asked for them without a GPU (one to be
        added later)."""
        return self.uses_gpu or self.download_models

    @property
    def uses_nginx(self) -> bool:
        # Every mode but the rare one: nginx is the only way the backend's
        # public surfaces (/v1, /public, /webhooks) are reachable from another host.
        return self.access_mode != "direct-no-nginx"

    @property
    def https(self) -> bool:
        return self.public_url.startswith("https://")

    def model_services(self) -> list[str]:
        out = []
        if self.text_mode == "local":
            out.append("vllm-server")
        if self.embedding_mode == "local":
            out.append("embedding-vllm-server")
        if self.rerank_mode == "local":
            out.append("rerank-vllm-server")
        if self.transcription:
            out += ["transcription-vllm-server", "diarization-server"]
        return out

    def app_services(self) -> list[str]:
        out = ["frontend", "backend"]
        if self.transcription:
            out.append("transcription-bot")
        return out

    def edge_services(self) -> list[str]:
        if not self.uses_nginx:
            return []
        out = ["nginx"]
        if self.access_mode == "letsencrypt":
            out.append("certbot")
        return out

    def all_services(self) -> list[str]:
        return ["db"] + self.model_services() + self.app_services() + self.edge_services()

    def required_ports(self, values: dict[str, str]) -> dict[int, str]:
        """port -> purpose, for the preflight conflict check."""

        def p(key: str, default: int) -> int:
            try:
                return int(values.get(key) or default)
            except ValueError:
                return default

        ports = {
            p("POSTGRES_PORT", 55432): "postgres (db)",
            p("FRONTEND_PORT", 3000): "frontend",
            p("SERVER_PORT", 8005): "backend (loopback)",
        }
        if self.text_mode == "local":
            ports[p("VLLM_PORT", 8001)] = "vllm-server"
        if self.embedding_mode == "local":
            ports[p("EMBEDDING_VLLM_PORT", 8006)] = "embedding-vllm-server"
        if self.rerank_mode == "local":
            ports[p("RERANK_VLLM_PORT", 8004)] = "rerank-vllm-server"
        if self.transcription:
            ports[p("TRANSCRIPTION_VLLM_PORT", 8002)] = "transcription-vllm-server"
            ports[p("DIARIZATION_SERVER_PORT", 8008)] = "diarization-server"
        if self.uses_nginx:
            ports[p("NGINX_HTTP_PORT", 80)] = "nginx (http)"
            ports[p("NGINX_HTTPS_PORT", 443)] = "nginx (https)"
        if self.mcp:
            from .schema import MCP_PORTS

            for key, default in MCP_PORTS.items():
                ports[p(key, default)] = key.lower().replace("_port", "").replace("_", "-")
        return ports

    def bind_addresses(self, values: dict[str, str]) -> dict[int, str]:
        try:
            backend = int(values.get("SERVER_PORT") or 8005)
        except ValueError:
            backend = 8005
        return {backend: values.get("SERVER_BIND_ADDRESS") or "127.0.0.1"}

    # ------------------------------------------------------------ persistence

    @classmethod
    def path(cls, app_dir: Path) -> Path:
        return app_dir / STATE_DIR / STATE_FILE

    @classmethod
    def load(cls, app_dir: Path) -> "State | None":
        p = cls.path(app_dir)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, app_dir: Path) -> Path:
        from . import __version__

        self.configured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.orizonctl_version = __version__
        p = self.path(app_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return p
