"""Which file each variable the CLI writes belongs to, and the defaults that
are the CLI's to choose.

The authoritative documentation of every variable is the template it lives in
(`env.example*`); this module only records the routing the wizard needs. A key
absent here is left at whatever the template says.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class EnvFile(str, Enum):
    ENV = ".env"
    SECRETS = ".env.secrets"
    VLLM = ".env.vllm"
    MCP = ".env.mcp"
    NGINX = ".env.nginx"


TEMPLATES: dict[EnvFile, str] = {
    EnvFile.ENV: "env.example",
    EnvFile.SECRETS: "env.example.secrets",
    EnvFile.VLLM: "env.example.vllm",
    EnvFile.MCP: "env.example.mcp",
    EnvFile.NGINX: "env.example.nginx",
}

# Files whose contents must not be world-readable.
FILE_MODES: dict[EnvFile, int] = {
    EnvFile.ENV: 0o640,
    EnvFile.SECRETS: 0o600,
    EnvFile.VLLM: 0o644,
    EnvFile.MCP: 0o644,
    EnvFile.NGINX: 0o600,  # carries the Cloudflare token
}


@dataclass(frozen=True)
class Var:
    key: str
    file: EnvFile
    secret: bool = False


_VARS: list[Var] = [
    # .env — the app and everything Compose reads at parse time
    Var("COMPOSE_PROJECT_NAME", EnvFile.ENV),
    Var("COMPOSE_PROFILES", EnvFile.ENV),
    Var("ORIZON_FEATURE_TRANSCRIPTION", EnvFile.ENV),
    Var("ORIZON_FEATURE_TELEGRAM", EnvFile.ENV),
    Var("ORIZON_VERSION", EnvFile.ENV),
    Var("ORIZON_IMAGE_PREFIX", EnvFile.ENV),
    Var("NEXT_PUBLIC_TELEGRAM_BOT_USERNAME", EnvFile.ENV),
    Var("BETTER_AUTH_SECRET", EnvFile.ENV, secret=True),
    Var("NGINX_TEMPLATE", EnvFile.ENV),
    Var("SSL_CERT_DIR", EnvFile.ENV),
    Var("POSTGRES_PORT", EnvFile.ENV),
    Var("FRONTEND_PORT", EnvFile.ENV),
    Var("SERVER_PORT", EnvFile.ENV),
    Var("SERVER_BIND_ADDRESS", EnvFile.ENV),
    Var("NGINX_HTTP_PORT", EnvFile.ENV),
    Var("NGINX_HTTPS_PORT", EnvFile.ENV),
    Var("VLLM_PORT", EnvFile.ENV),
    Var("TRANSCRIPTION_VLLM_PORT", EnvFile.ENV),
    Var("RERANK_VLLM_PORT", EnvFile.ENV),
    Var("EMBEDDING_VLLM_PORT", EnvFile.ENV),
    Var("DIARIZATION_SERVER_PORT", EnvFile.ENV),
    Var("DATABASE_URL", EnvFile.ENV, secret=True),
    Var("POSTGRES_USER", EnvFile.ENV),
    Var("POSTGRES_PASSWORD", EnvFile.ENV, secret=True),
    Var("POSTGRES_DB", EnvFile.ENV),
    Var("FRONTEND_URLS", EnvFile.ENV),
    Var("FASTAPI_URL", EnvFile.ENV),
    Var("BETTER_AUTH_URL", EnvFile.ENV),
    Var("FRONTEND_API_URL", EnvFile.ENV),
    Var("SECURE_COOKIES", EnvFile.ENV),
    Var("LLM_API_URL", EnvFile.ENV),
    Var("IMAGE_LLM_API_URL", EnvFile.ENV),
    Var("EMBEDDING_API_URL", EnvFile.ENV),
    Var("RERANK_API_URL", EnvFile.ENV),
    Var("TRANSCRIPTION_BOT_VLLM_API_URL", EnvFile.ENV),
    Var("DIARIZATION_API_URL", EnvFile.ENV),
    Var("LLM_DEPLOYMENT_MODE", EnvFile.ENV),
    Var("IMAGE_LLM_MODE", EnvFile.ENV),
    Var("EMBEDDING_MODE", EnvFile.ENV),
    Var("RERANK_MODE", EnvFile.ENV),
    Var("DEFAULT_MODEL", EnvFile.ENV),
    Var("IMAGE_MODEL", EnvFile.ENV),
    Var("LLM_EMBEDDING_MODEL", EnvFile.ENV),
    Var("RERANK_MODEL", EnvFile.ENV),
    Var("TRANSCRIPTION_MODEL", EnvFile.ENV),
    Var("DIARIZATION_MODEL", EnvFile.ENV),
    Var("CUDA_VISIBLE_DEVICES", EnvFile.ENV),
    Var("BACKEND_GPU_DEVICE", EnvFile.ENV),
    Var("TRANSCRIPTION_BOT_GPU_DEVICE", EnvFile.ENV),
    Var("DIARIZATION_GPU_DEVICE", EnvFile.ENV),
    Var("EMBEDDING_VLLM_GPU_DEVICE", EnvFile.ENV),
    Var("RERANK_VLLM_GPU_DEVICE", EnvFile.ENV),
    Var("MODELS_PATH", EnvFile.ENV),
    Var("MODELS_SERVE_PATH", EnvFile.ENV),
    Var("ORIZON_MCP_OAUTH_REDIRECT_URI", EnvFile.ENV),
    Var("ORIZON_SMB_MOUNT_BASE_DIR", EnvFile.ENV),
    # .env.secrets — on-disk credentials only
    Var("ORIZON_MCP_ENCRYPTION_KEY", EnvFile.SECRETS, secret=True),
    Var("ORIZON_INTERNAL_API_KEY", EnvFile.SECRETS, secret=True),
    Var("WEBHOOK_API_KEY", EnvFile.SECRETS, secret=True),
    Var("HUGGINGFACE_TOKEN", EnvFile.SECRETS, secret=True),
    Var("BOT_TOKEN", EnvFile.SECRETS, secret=True),
    Var("API_ID", EnvFile.SECRETS, secret=True),
    Var("API_HASH", EnvFile.SECRETS, secret=True),
    Var("LLM_API_KEY", EnvFile.SECRETS, secret=True),
    Var("IMAGE_API_KEY", EnvFile.SECRETS, secret=True),
    Var("EMBEDDING_API_KEY", EnvFile.SECRETS, secret=True),
    Var("RERANK_API_KEY", EnvFile.SECRETS, secret=True),
    # .env.vllm — the bundled model servers
    Var("VLLM_DEFAULT_MODEL", EnvFile.VLLM),  # alias, see VLLM_ALIASES
    Var("VLLM_LLM_EMBEDDING_MODEL", EnvFile.VLLM),
    Var("VLLM_RERANK_MODEL", EnvFile.VLLM),
    Var("VLLM_TRANSCRIPTION_MODEL", EnvFile.VLLM),
    Var("VLLM_DIARIZATION_MODEL", EnvFile.VLLM),
    Var("DIARIZATION_MODEL_LOCAL_DIR", EnvFile.VLLM),
    Var("VLLM_MODELS_SERVE_PATH", EnvFile.VLLM),
    Var("GPU_MEMORY_UTILIZATION", EnvFile.VLLM),
    Var("EMBEDDING_GPU_MEMORY_UTILIZATION", EnvFile.VLLM),
    Var("RERANK_GPU_MEMORY_UTILIZATION", EnvFile.VLLM),
    Var("TRANSCRIPTION_BOT_GPU_MEMORY_UTILIZATION", EnvFile.VLLM),
    Var("TENSOR_PARALLEL_SIZE", EnvFile.VLLM),
    Var("VLLM_MAX_MODEL_LEN", EnvFile.VLLM),
    # .env.mcp — connector ports (kept at template defaults; the compose file
    # interpolates the same numbers)
    # .env.nginx — nginx, certbot and setup-ssl.sh
    Var("NGINX_DOMAIN", EnvFile.NGINX),
    Var("NGINX_SERVER_PORT", EnvFile.NGINX),  # alias
    Var("NGINX_FRONTEND_PORT", EnvFile.NGINX),  # alias
    Var("CERTBOT_EMAIL", EnvFile.NGINX),
    Var("CLOUDFLARE_API_TOKEN", EnvFile.NGINX, secret=True),
    Var("CERTBOT_STAGING", EnvFile.NGINX),
    Var("CERTBOT_FORCE_RECREATE", EnvFile.NGINX),
    Var("NGINX_CONTAINER_NAME", EnvFile.NGINX),
]

VARS: dict[str, Var] = {v.key: v for v in _VARS}

# The same NAME is declared in two files with two meanings (`.env` says what the
# app asks for, `.env.vllm` says what the bundled server loads; the nginx pair
# repeats the two published ports because Compose cannot read `.env.nginx` at
# parse time). The wizard keeps them in step through these prefixed aliases,
# which are un-prefixed when the file is written.
ALIASES: dict[str, str] = {
    "VLLM_DEFAULT_MODEL": "DEFAULT_MODEL",
    "VLLM_LLM_EMBEDDING_MODEL": "LLM_EMBEDDING_MODEL",
    "VLLM_RERANK_MODEL": "RERANK_MODEL",
    "VLLM_TRANSCRIPTION_MODEL": "TRANSCRIPTION_MODEL",
    "VLLM_DIARIZATION_MODEL": "DIARIZATION_MODEL",
    "VLLM_MODELS_SERVE_PATH": "MODELS_SERVE_PATH",
    "NGINX_SERVER_PORT": "SERVER_PORT",
    "NGINX_FRONTEND_PORT": "FRONTEND_PORT",
}

PREVIOUS_FILE: dict[str, EnvFile] = {
    "VLLM_PORT": EnvFile.VLLM,
    "TRANSCRIPTION_VLLM_PORT": EnvFile.VLLM,
    "IMAGE_VLLM_PORT": EnvFile.VLLM,
    "RERANK_VLLM_PORT": EnvFile.VLLM,
    "EMBEDDING_VLLM_PORT": EnvFile.VLLM,
    "DIARIZATION_SERVER_PORT": EnvFile.VLLM,
    "DIARIZATION_MODEL_LOCAL_DIR": EnvFile.ENV,
}

SECRET_KEYS = {v.key for v in _VARS if v.secret} | {"CLOUDFLARE_API_TOKEN"}


def is_secret_name(key: str) -> bool:
    """The same whole-word rule the admin panel uses to mask a value."""
    words = set(key.upper().split("_"))
    return key in SECRET_KEYS or bool(words & {"KEY", "SECRET", "PASSWORD", "PASS", "TOKEN", "CREDENTIAL", "DSN"})


def available_files(repo: Path) -> set[EnvFile]:
    """Which of the five files this checkout knows about, judged by template."""
    return {f for f, name in TEMPLATES.items() if (repo / name).exists()}


def route(var: Var, files: set[EnvFile]) -> EnvFile:
    """Where a key goes IN THIS CHECKOUT. A checkout predating the split (the
    old single-file layout) has no `env.example.secrets`, so a key that would
    live there goes into `.env`, which is the only file that compose reads."""
    if var.file in files:
        return var.file
    return EnvFile.ENV


def split_by_file(values: dict[str, str], files: set[EnvFile]) -> dict[EnvFile, dict[str, str]]:
    """Distribute wizard output over the files this checkout has, resolving
    aliases. On the old layout, a `.env.vllm`-only alias is dropped: the old
    compose hands `.env` to the model servers, so the un-prefixed key already
    reaches them."""
    out: dict[EnvFile, dict[str, str]] = {f: {} for f in files}
    for key, value in values.items():
        var = VARS.get(key)
        if var is None:
            out.setdefault(EnvFile.ENV, {})[key] = value
            continue
        target = route(var, files)
        real_key = ALIASES.get(key, key)
        if key in ALIASES and target != var.file:
            continue  # alias collapsed into .env's own copy of the key
        out.setdefault(target, {})[real_key] = value
    return out


# ------------------------------------------------------------------ defaults

DEFAULT_PORTS = {
    "POSTGRES_PORT": 55432,
    "FRONTEND_PORT": 3000,
    "SERVER_PORT": 8005,
    "VLLM_PORT": 8001,
    "TRANSCRIPTION_VLLM_PORT": 8002,
    "RERANK_VLLM_PORT": 8004,
    "EMBEDDING_VLLM_PORT": 8006,
    "DIARIZATION_SERVER_PORT": 8008,
    "NGINX_HTTP_PORT": 80,
    "NGINX_HTTPS_PORT": 443,
}

MODEL_SERVER_PORTS = frozenset({"VLLM_PORT", "TRANSCRIPTION_VLLM_PORT", "RERANK_VLLM_PORT", "EMBEDDING_VLLM_PORT", "DIARIZATION_SERVER_PORT"})

MCP_PORTS = {
    "MCP_HUBSPOT_PORT": 8100,
    "MCP_FRESHDESK_PORT": 8101,
    "MCP_FRESHCALLER_PORT": 8102,
    "MCP_SALESFORCE_PORT": 8103,
    "MCP_CENTRAL_PORT": 8104,
    "MCP_MICROSOFT_PORT": 8105,
    "MCP_GOOGLE_PORT": 8106,
    "MCP_SANDBOX_PORT": 8107,
    "MCP_ATLASSIAN_PORT": 8108,
    "MCP_GITHUB_PORT": 8109,
    "MCP_GITLAB_PORT": 8110,
    "MCP_IMAP_PORT": 8111,
}

DEFAULT_MODELS = {
    "text": "Qwen/Qwen3.6-35B-A3B-FP8",
    "embedding": "nomic-ai/nomic-embed-text-v2-moe",
    "rerank": "Qwen/Qwen3-Reranker-0.6B",
    "transcription": "Qwen/Qwen3-ASR-1.7B",
    "diarization": "pyannote/speaker-diarization-3.1",
}

# What the bundled servers take of one card, from env.example.vllm. They must
# sum to under 1 across whatever shares a GPU.
DEFAULT_GPU_FRACTIONS = {
    "text": 0.50,
    "embedding": 0.10,
    "rerank": 0.12,
    "transcription": 0.40,
}

NGINX_TEMPLATES = {
    "http-only": "orizonai-http-only.conf.template",
    "letsencrypt": "orizonai.conf.template",
    "custom-cert": "orizonai-custom-cert.conf.template",
}

PROJECT_NAME = "orizon-ai-app"
MCP_PROJECT_NAME = "orizon-ai-app-mcp"

DEFAULT_IMAGE_PREFIX = "scr.syneto.eu/orizon-ai/orizon-ai"
# The image every deployment has, used to prove registry access before a pull.
PROBE_IMAGE = "orizon-ai-backend"


def registry_host(prefix: str) -> str:
    """The registry a `docker login` targets for this prefix: the first path
    segment when it looks like a host (has a dot or a port, or is localhost),
    otherwise Docker Hub."""
    first = prefix.strip("/").split("/")[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return "docker.io"
