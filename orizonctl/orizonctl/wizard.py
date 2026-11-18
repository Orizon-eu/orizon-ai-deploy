"""The configuration wizard: a dozen questions in, five env files out.

Every question consults, in order: the config file's `configure:` section
(unattended installs), the value already in the operator's env files (re-runs
propose what is there), then the shipped default. Secrets already on disk are
KEPT, never regenerated: a new ORIZON_MCP_ENCRYPTION_KEY would make every
stored credential unreadable.
"""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import click
import yaml

from . import hostinfo as h
from . import ui
from .envfile import load
from .schema import (
    MODEL_SERVER_PORTS,
    ALIASES,
    DEFAULT_GPU_FRACTIONS,
    DEFAULT_IMAGE_PREFIX,
    PROBE_IMAGE,
    registry_host,
    DEFAULT_MODELS,
    DEFAULT_PORTS,
    NGINX_TEMPLATES,
    PROJECT_NAME,
    EnvFile,
    available_files,
)
from .state import ACCESS_MODES, State


class WizardError(click.ClickException):
    pass


# ---------------------------------------------------------------- generators


def gen_hex(n: int) -> str:
    return secrets.token_hex(n)


def gen_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def gen_password() -> str:
    # letters and digits only: safe inside a URL without percent-encoding
    return secrets.token_hex(16)


# --------------------------------------------------------------- current env


def current_values(repo: Path) -> dict[str, str]:
    """Everything in the operator's existing files, with the .env.vllm and
    .env.nginx copies of shared names folded under their aliases."""
    files = available_files(repo)
    merged: dict[str, str] = {}
    merged.update(load(repo / EnvFile.ENV.value))
    if EnvFile.SECRETS in files:
        merged.update(load(repo / EnvFile.SECRETS.value))
    reverse = {v: k for k, v in ALIASES.items()}
    if EnvFile.VLLM in files:
        for k, v in load(repo / EnvFile.VLLM.value).items():
            alias = reverse.get(k)
            if alias and alias.startswith("VLLM_"):
                merged[alias] = v
            elif k not in merged:
                merged[k] = v
    if EnvFile.NGINX in files:
        for k, v in load(repo / EnvFile.NGINX.value).items():
            alias = reverse.get(k)
            if alias and alias.startswith("NGINX_"):
                merged[alias] = v
            elif k not in merged:
                merged[k] = v
    return merged


def template_defaults(repo: Path) -> dict[str, str]:
    from .schema import TEMPLATES

    out: dict[str, str] = {}
    for f, name in TEMPLATES.items():
        p = repo / name
        if p.exists():
            for k, v in load(p).items():
                out.setdefault(k, v)
    return out


# ---------------------------------------------------------------- the wizard


class Wizard:
    def __init__(
        self,
        repo: Path,
        interactive: bool,
        answers: dict[str, Any] | None = None,
        previous: State | None = None,
    ):
        self.repo = repo
        self.interactive = interactive
        self.answers = answers or {}
        self.previous = previous
        self.current = current_values(repo)
        self.tdefaults = template_defaults(repo)
        self.values: dict[str, str] = {}
        self.state = State()
        self.gpus = h.nvidia_gpus()

    # ------------------------------------------------------------- plumbing

    def a(self, *path: str, default: Any = None) -> Any:
        """Look a value up in the answers file: a("access", "domain")."""
        node: Any = self.answers
        for p in path:
            if not isinstance(node, dict) or p not in node:
                return default
            node = node[p]
        return node

    def cur(self, key: str, fallback: str = "") -> str:
        v = self.current.get(key)
        if v is None or v == "":
            v = self.tdefaults.get(key, fallback)
        return v if v is not None else fallback

    def ask(
        self,
        prompt: str,
        *,
        answer: Any,
        default: Any = None,
        required: bool = False,
        secret: bool = False,
        type: Any = str,
        validate: Callable[[Any], str | None] | None = None,
        help: str = "",
    ) -> Any:
        """One question. `answer` is what the answers file said (None if
        nothing); it wins outright. Non-interactive with no answer and no
        default is an error when required."""
        if answer is not None and answer != "":
            value = type(answer) if type is not bool else bool(answer)
            if validate:
                err = validate(value)
                if err:
                    raise WizardError(f"{prompt}: {err} (from answers file)")
            return value
        if not self.interactive:
            if (default is None or default == "") and required:
                raise WizardError(f"non-interactive run needs an answer for: {prompt}")
            return type(default) if default not in (None, "") else default
        if help:
            ui.note(help)
        has_default = default not in (None, "")
        while True:
            if secret:
                shown = f"{prompt} [Enter keeps the current value]" if has_default else prompt
                value = click.prompt(shown, default=default if has_default else "", hide_input=True, show_default=False, type=str)
            elif has_default:
                value = click.prompt(prompt, default=default, type=type, show_default=True)
            elif required or type is not str:
                value = click.prompt(prompt, type=type)
            else:
                value = click.prompt(prompt, default="", show_default=False, type=str)
            if required and (value is None or value == ""):
                ui.warn("A value is required.")
                continue
            if validate and value not in (None, ""):
                err = validate(value)
                if err:
                    ui.warn(err)
                    continue
            return value

    def confirm(self, prompt: str, *, answer: Any, default: bool, help: str = "") -> bool:
        if answer is not None:
            return bool(answer)
        if not self.interactive:
            return default
        if help:
            ui.note(help)
        return click.confirm(prompt, default=default)

    def choice(self, prompt: str, options: list[tuple[str, str]], *, answer: Any, default: str, help: str = "") -> str:
        keys = [k for k, _ in options]
        if answer is not None:
            if answer not in keys:
                raise WizardError(f"{prompt}: '{answer}' is not one of {keys}")
            return answer
        if not self.interactive:
            return default
        if help:
            ui.note(help)
        for i, (k, label) in enumerate(options, 1):
            marker = click.style(" (default)", dim=True) if k == default else ""
            click.echo(f"    {i}) {label}{marker}")
        while True:
            raw = click.prompt(prompt, default=str(keys.index(default) + 1), show_default=True)
            if raw in keys:
                return raw
            try:
                idx = int(raw) - 1
                if 0 <= idx < len(keys):
                    return keys[idx]
            except ValueError:
                pass
            ui.warn(f"Enter 1-{len(keys)}.")

    def set(self, key: str, value: Any) -> None:
        self.values[key] = "" if value is None else str(value)

    # ------------------------------------------------------------- sections

    def run(self) -> tuple[dict[str, str], State]:
        ui.header("Orizon AI configuration")
        if self.current:
            ui.info("Existing configuration found; current values are offered as defaults.")
        self.section_version()
        self.section_registry()
        self.section_models()
        self.section_gpu()
        self.section_access()
        self.section_ports()
        self.section_database()
        self.section_secrets()
        self.section_mcp()
        self.section_transcription()
        self.section_downloads()
        self.finish()
        return self.values, self.state

    def section_version(self) -> None:
        ui.header("Release")
        version = self.ask(
            "Image tag to deploy (ORIZON_VERSION)",
            answer=self.a("version"),
            default=self.cur("ORIZON_VERSION", "v1.3.0"),
            required=True,
            validate=lambda v: None if str(v).startswith("v") else "tags look like v1.2.0",
            help="Every service is pulled from the registry at this tag. Release tags look like v1.2.0; a dev build like v1.2.0_dev works too.\nThe next section lists the tags the registry actually holds and lets you correct this.",
        )
        self.set("ORIZON_VERSION", version)
        self.set("COMPOSE_PROJECT_NAME", PROJECT_NAME)
        self.state.version = version
        self.state.gpu_count = len(self.gpus)

    def section_registry(self) -> None:
        from . import compose as _compose  # module attribute lookup, so tests can stub the docker calls

        ui.header("Container registry")
        prefix = self.ask(
            "Image prefix (registry host + project path, no trailing slash)",
            answer=self.a("registry", "prefix"),
            default=self.cur("ORIZON_IMAGE_PREFIX", DEFAULT_IMAGE_PREFIX) or DEFAULT_IMAGE_PREFIX,
            required=True,
            validate=_validate_prefix,
            help="Images are pulled as <prefix>/orizon-ai-backend:<version> etc. Host PLUS project: the host alone is not a valid image name.\nSyneto proxy cache: scr.syneto.eu/orizon-ai/orizon-ai (default; <host>/<proxy project>/<origin project>)   origin: harbor.syneto.eu/orizon-ai   Docker Hub: a namespace such as sadramoghadam\n`docker login` targets the host only (scr.syneto.eu); the project path matters for the pull, not the login.",
        )
        prefix = _normalize_prefix(prefix)
        self.set("ORIZON_IMAGE_PREFIX", prefix)
        host = registry_host(prefix)
        self.state.image_prefix = prefix
        self.state.registry_host = host

        prev_login = self.previous.registry_login if self.previous else (host != "docker.io")
        explicit_login = self.a("registry", "login")
        has_creds = bool(self.a("registry", "username"))
        stored = None
        if not self.interactive:
            # unattended: log in only when told to or given credentials; a
            # public registry or an existing login on the host needs nothing
            login = bool(explicit_login) if explicit_login is not None else has_creds
        else:
            from . import registry as _registry

            stored = _registry.docker_credentials(host) if host != "docker.io" else None
            if stored:
                # `docker login` already ran on this host (setup, or by hand):
                # the pull will use that credential, so only offer to replace it.
                login = self.confirm(
                    f"Enter a different login for {host}?",
                    answer=True if has_creds else explicit_login,
                    default=False,
                    help=f"A `docker login {host}` as {stored[0]} is already stored on this host and is what the pull uses; the tag check below proves whether it still works.",
                )
            else:
                login = self.confirm(
                    f"Does pulling from {host} need a login on this host?",
                    answer=True if has_creds else explicit_login,
                    default=prev_login,
                    help="A Harbor robot account (pull on the project), a GitLab deploy token (read_registry) or a Docker Hub access token.\nThe credential goes to `docker login` and lives in /root/.docker/config.json; orizonctl keeps no copy.",
                )
        self.state.registry_login = bool(login) or bool(self.interactive and stored)
        login_failed = False
        if login:
            username = self.ask("Registry username", answer=self.a("registry", "username"), default="", required=True)
            token = self.ask("Registry token / password", answer=self.a("registry", "token"), default="", required=True, secret=True)
            ok, msg = _compose.registry_login(host, username, token)
            login_failed = not ok
            if ok:
                ui.ok(f"docker login {host}: {msg}")
            elif self.interactive:
                ui.warn(f"docker login {host} failed: {msg}. A credential stored by an earlier login, if any, is untouched; checking with it below.")
            else:
                raise WizardError(f"docker login {host} failed: {msg}")
        # What does the registry actually hold for this prefix? Lets a wrong or
        # not-yet-pushed tag be corrected here rather than discovered at `up`.
        from . import registry as _registry

        tags = _registry.list_tags(prefix, PROBE_IMAGE)
        alt = _proxy_cache_alt(prefix, host)
        if not tags and alt:
            # A Harbor proxy-cache project exposes the upstream path under its
            # own name, so <host>/<project> is one segment short of the real
            # location. Tag listing does not depend on the tag being right, so
            # it catches a wrong prefix even when the version is mistyped.
            alt_tags = _registry.list_tags(alt, PROBE_IMAGE)
            if alt_tags:
                ui.warn(f"{prefix}/{PROBE_IMAGE} lists no tags, but {alt}/{PROBE_IMAGE} has {', '.join(alt_tags[-6:])} (Harbor proxy-cache layout).")
                if self._adopt_prefix(alt):
                    prefix, tags = alt, alt_tags
        if tags:
            shown = tags[-12:]
            ui.info(f"tags of {prefix}/{PROBE_IMAGE} in the registry: {', '.join(shown)}" + (" ..." if len(tags) > len(shown) else ""))
            if self.state.version not in tags:
                default = _registry.newest(tags) or self.state.version
                if self.interactive:
                    ui.warn(f"{self.state.version} is not among them.")
                    picked = self.ask("Tag to deploy", answer=None, default=default, required=True, validate=lambda v: None if str(v) in tags else f"not in the registry; one of: {', '.join(shown)}")
                    self.set("ORIZON_VERSION", picked)
                    self.state.version = str(picked)
                else:
                    ui.warn(f"{self.state.version} is not in the registry (newest there: {default}); `orizonctl up` will fail at the pull until it is pushed.")
        else:
            ui.info("could not list tags (no permission to list, or the repository does not exist yet); probing the one tag instead")
        ref = f"{prefix}/{PROBE_IMAGE}:{self.state.version}"
        ok, msg = _compose.image_accessible(ref)
        alt = _proxy_cache_alt(prefix, host)
        if not ok and "not found" in msg.lower() and alt:
            # Listing was not permitted (or found nothing): fall back to probing
            # the one tag at the proxy-cache shape before making the operator guess.
            ok_alt, _ = _compose.image_accessible(f"{alt}/{PROBE_IMAGE}:{self.state.version}")
            if ok_alt:
                ui.warn(f"{ref}: not found, but the image exists at {alt}/{PROBE_IMAGE} (Harbor proxy-cache layout).")
                if self._adopt_prefix(alt):
                    prefix = alt
                    ref = f"{prefix}/{PROBE_IMAGE}:{self.state.version}"
                    ok, msg = _compose.image_accessible(ref)
        (ui.ok if ok else ui.warn)(f"{ref}: {msg}" + ("" if ok else "  (check the prefix, the tag and the login before `orizonctl up`)"))
        if ok and login_failed:
            ui.info(f"the login just entered was rejected, but the credential already stored on this host for {host} works; `orizonctl up` will use it.")
        elif not ok and login_failed:
            ui.warn("`orizonctl up` will fail at the pull until `docker login` succeeds; re-run `orizonctl configure` or run it by hand.")

    def _adopt_prefix(self, alt: str) -> bool:
        """Switch the image prefix to `alt` after asking. Unattended runs are
        never silently rewritten: they get the hint and keep what was given."""
        if not self.interactive:
            ui.warn(f"That looks like a Harbor proxy-cache path: set the prefix to {alt}.")
            return False
        if not click.confirm(f"Use {alt} as the image prefix?", default=True):
            return False
        self.set("ORIZON_IMAGE_PREFIX", alt)
        self.state.image_prefix = alt
        return True

    def section_models(self) -> None:
        ui.header("Models")
        if self.gpus:
            for g in self.gpus:
                ui.kv(f"GPU {g.index}", f"{g.name}, {g.memory_mib // 1024} GiB, driver {g.driver}")
        else:
            ui.info("No NVIDIA GPU detected: every model has to come from an external OpenAI-compatible endpoint.")

        local_ok = bool(self.gpus)
        local_default = "local" if local_ok else "external"
        prev_text = self.previous.text_mode if self.previous else (self.cur("LLM_DEPLOYMENT_MODE", local_default) or local_default)
        prev_emb = self.previous.embedding_mode if self.previous else (self.cur("EMBEDDING_MODE", local_default) or local_default)
        prev_rerank = self.previous.rerank_mode if self.previous else ("off" if self.current and self.cur("RERANK_MODEL") == "" else (self.cur("RERANK_MODE", local_default) or local_default))
        if not local_ok:
            prev_text = "external"
            prev_emb = "external"
            prev_rerank = "off" if prev_rerank == "local" else prev_rerank

        endpoint_opts = [("local", "served by this host's GPU (bundled vLLM server)"), ("external", "an OpenAI-compatible endpoint elsewhere")]
        if not local_ok:
            endpoint_opts = endpoint_opts[1:]

        # text
        text_mode = self.choice("Chat / text model", endpoint_opts, answer=self.a("models", "text"), default=prev_text if prev_text in dict(endpoint_opts) else endpoint_opts[0][0])
        self.state.text_mode = text_mode
        if text_mode == "local":
            model = self.ask("Text model (HuggingFace id, weights under MODELS_PATH/<basename>)", answer=self.a("models", "text_model"), default=self.cur("DEFAULT_MODEL", DEFAULT_MODELS["text"]), required=True)
            self.set("DEFAULT_MODEL", model)
            self.set("VLLM_DEFAULT_MODEL", model)
            self.set("LLM_DEPLOYMENT_MODE", "local")
            self.set("IMAGE_MODEL", model)
            self.set("IMAGE_LLM_MODE", "local")
        else:
            url = self.ask(
                "Text endpoint base URL (no /v1)",
                answer=self.a("external", "text", "url"),
                default=self.cur("LLM_API_URL"),
                required=True,
                validate=_validate_url,
                help="Examples: https://api.openai.com   http://host.docker.internal:11434 (Ollama)\nA gateway whose compatibility root is not /v1 may be given with its path.",
            )
            model = self.ask("Model name the endpoint expects", answer=self.a("external", "text", "model"), default=self.cur("DEFAULT_MODEL", ""), required=True)
            key = self.ask("API key for that endpoint (empty: set it later in Settings > System > Credentials)", answer=self.a("external", "text", "api_key"), default=self.current.get("LLM_API_KEY", ""), secret=True)
            self.set("LLM_API_URL", url)
            self.set("DEFAULT_MODEL", model)
            self.set("LLM_DEPLOYMENT_MODE", "external")
            self.set("IMAGE_LLM_API_URL", url)
            self.set("IMAGE_MODEL", model)
            self.set("IMAGE_LLM_MODE", "external")
            if key:
                self.set("LLM_API_KEY", key)
                self.set("IMAGE_API_KEY", key)

        # embeddings
        emb_mode = self.choice("Embedding model (indexes every workspace)", endpoint_opts, answer=self.a("models", "embedding"), default=prev_emb if prev_emb in dict(endpoint_opts) else endpoint_opts[0][0])
        self.state.embedding_mode = emb_mode
        emb_model = self.ask("Embedding model id", answer=self.a("models", "embedding_model") or self.a("external", "embedding", "model"), default=self.cur("LLM_EMBEDDING_MODEL", DEFAULT_MODELS["embedding"]), required=True,
                             help="Changing this later invalidates every stored index.")
        self.set("LLM_EMBEDDING_MODEL", emb_model)
        self.set("EMBEDDING_MODE", emb_mode)
        if emb_mode == "local":
            self.set("VLLM_LLM_EMBEDDING_MODEL", emb_model)
        else:
            url = self.ask("Embedding endpoint base URL", answer=self.a("external", "embedding", "url"), default=self.cur("EMBEDDING_API_URL"), required=True, validate=_validate_url)
            key = self.ask("Embedding API key (empty: set it in the panel)", answer=self.a("external", "embedding", "api_key"), default=self.current.get("EMBEDDING_API_KEY", ""), secret=True)
            self.set("EMBEDDING_API_URL", url)
            if key:
                self.set("EMBEDDING_API_KEY", key)

        # rerank
        rerank_opts = endpoint_opts + [("off", "no reranker")]
        rerank_mode = self.choice("Reranker", rerank_opts, answer=self.a("models", "rerank"), default=prev_rerank if prev_rerank in dict(rerank_opts) else "off")
        self.state.rerank_mode = rerank_mode
        if rerank_mode == "off":
            self.set("RERANK_MODEL", "")
        else:
            model = self.ask("Rerank model id", answer=self.a("models", "rerank_model") or self.a("external", "rerank", "model"), default=self.cur("RERANK_MODEL", DEFAULT_MODELS["rerank"]) or DEFAULT_MODELS["rerank"], required=True)
            self.set("RERANK_MODEL", model)
            self.set("RERANK_MODE", rerank_mode)
            if rerank_mode == "local":
                self.set("VLLM_RERANK_MODEL", model)
            else:
                url = self.ask("Rerank endpoint base URL", answer=self.a("external", "rerank", "url"), default=self.cur("RERANK_API_URL"), required=True, validate=_validate_url)
                key = self.ask("Rerank API key (empty: set it in the panel)", answer=self.a("external", "rerank", "api_key"), default=self.current.get("RERANK_API_KEY", ""), secret=True)
                self.set("RERANK_API_URL", url)
                if key:
                    self.set("RERANK_API_KEY", key)

        # transcription (needs a GPU: its speech-to-text and diarization servers each reserve one)
        if local_ok:
            self.state.transcription = self.confirm(
                "Enable Orizon Transcribe (Qwen3-ASR and pyannote diarization on this GPU, optional Telegram bot)?",
                answer=self.a("models", "transcription"),
                default=self.previous.transcription if self.previous else False,
            )
            if self.state.transcription:
                model = self.ask("Transcription model id", answer=self.a("models", "transcription_model"), default=self.cur("TRANSCRIPTION_MODEL", DEFAULT_MODELS["transcription"]), required=True)
                self.set("TRANSCRIPTION_MODEL", model)
                self.set("VLLM_TRANSCRIPTION_MODEL", model)
                diarization = self.cur("VLLM_DIARIZATION_MODEL") or self.cur("DIARIZATION_MODEL", DEFAULT_MODELS["diarization"])
                self.set("DIARIZATION_MODEL", diarization)
                self.set("VLLM_DIARIZATION_MODEL", diarization)
                self.set("DIARIZATION_MODEL_LOCAL_DIR", self.cur("DIARIZATION_MODEL_LOCAL_DIR", "speaker-diarization-3.1"))
        else:
            self.state.transcription = False
        self.set("ORIZON_FEATURE_TRANSCRIPTION", "true" if self.state.transcription else "false")

        # where the weights live
        if self.state.uses_gpu:
            self.ask_models_path()

    def ask_models_path(self) -> None:
        models_path = self.ask(
            "Directory on this host holding the model weights (MODELS_PATH)",
            answer=self.a("models_path"),
            default=self.current.get("MODELS_PATH") or "/srv/orizon-models",
            required=True,
            validate=lambda v: None if str(v).startswith("/") else "use an absolute path",
            help="Created if missing. Each model is expected at <MODELS_PATH>/<basename of its id>.",
        )
        self.set("MODELS_PATH", models_path)
        serve = self.cur("MODELS_SERVE_PATH", "/home/vllm/models") or "/home/vllm/models"
        self.set("MODELS_SERVE_PATH", serve)
        self.set("VLLM_MODELS_SERVE_PATH", serve)

    def section_gpu(self) -> None:
        if not self.state.uses_gpu:
            return
        ui.header("GPU layout")
        n = len(self.gpus)
        ids = [str(g.index) for g in self.gpus]

        def valid_dev(v: Any) -> str | None:
            return None if str(v) in ids else f"choose one of {', '.join(ids)}"

        tp = 1
        if self.state.text_mode == "local":
            tp = self.ask(
                "Tensor parallel size for the text server (GPUs it spans)",
                answer=self.a("gpu", "tensor_parallel"),
                default=int(self.cur("TENSOR_PARALLEL_SIZE", "1") or 1),
                type=int,
                validate=lambda v: None if 1 <= int(v) <= n else f"between 1 and {n}",
                help="The text server is handed every GPU and uses the first TENSOR_PARALLEL_SIZE of them." if n > 1 else "",
            )
            self.set("TENSOR_PARALLEL_SIZE", tp)
            self.set("VLLM_MAX_MODEL_LEN", self.ask(
                "Text server context length (VLLM_MAX_MODEL_LEN)",
                answer=self.a("gpu", "max_model_len"),
                default=int(self.cur("VLLM_MAX_MODEL_LEN", "262144") or 262144),
                type=int,
                help="KV cache for this length must fit in the memory fraction below. Lower it on a 24 GiB card (e.g. 32768).",
            ))

        devices: dict[str, str] = {}
        for role, key in (("embedding", "EMBEDDING_VLLM_GPU_DEVICE"), ("rerank", "RERANK_VLLM_GPU_DEVICE"), ("transcription", "TRANSCRIPTION_BOT_GPU_DEVICE"), ("diarization", "DIARIZATION_GPU_DEVICE")):
            on = {"embedding": self.state.embedding_mode == "local", "rerank": self.state.rerank_mode == "local", "transcription": self.state.transcription, "diarization": self.state.transcription}[role]
            if not on:
                continue
            dev = "0"
            if n > 1:
                dev = self.ask(f"GPU index for the {role} server", answer=self.a("gpu", f"{role}_device"), default=self.cur(key, "0") or "0", validate=valid_dev)
            devices[role] = str(dev)
            self.set(key, dev)
        self.set("BACKEND_GPU_DEVICE", "0")
        self.set("CUDA_VISIBLE_DEVICES", ",".join(ids) if ids else "0")

        # memory fractions: everything sharing a card must sum to under 1
        frac_keys = {
            "text": "GPU_MEMORY_UTILIZATION",
            "embedding": "EMBEDDING_GPU_MEMORY_UTILIZATION",
            "rerank": "RERANK_GPU_MEMORY_UTILIZATION",
            "transcription": "TRANSCRIPTION_BOT_GPU_MEMORY_UTILIZATION",
        }
        active: dict[str, float] = {}
        for role, key in frac_keys.items():
            on = {"text": self.state.text_mode == "local", "embedding": self.state.embedding_mode == "local", "rerank": self.state.rerank_mode == "local", "transcription": self.state.transcription}[role]
            if not on:
                continue
            given = self.a("gpu", "fractions", role)
            try:
                active[role] = float(given) if given is not None else float(self.cur(key, str(DEFAULT_GPU_FRACTIONS[role])) or DEFAULT_GPU_FRACTIONS[role])
            except ValueError:
                active[role] = DEFAULT_GPU_FRACTIONS[role]

        # which card each role lands on: text on the first tp cards, others as chosen
        per_card: dict[str, list[str]] = {}
        if "text" in active:
            for i in ids[:tp]:
                per_card.setdefault(i, []).append("text")
        for role in ("embedding", "rerank", "transcription"):
            if role in active:
                per_card.setdefault(devices.get(role, "0"), []).append(role)

        for card, roles in per_card.items():
            total = sum(active[r] for r in roles)
            if total > 0.92 and self.a("gpu", "fractions") is None:
                scale = 0.90 / total
                for r in roles:
                    active[r] = round(active[r] * scale, 2)
                ui.warn(f"GPU {card}: the default memory fractions of {', '.join(roles)} sum to {total:.2f}; scaled to 0.90 total.")

        if self.interactive and self.a("gpu", "fractions") is None:
            click.echo("  GPU memory fraction per server (must sum to < 1 per card):")
            for role in active:
                active[role] = self.ask(f"    {role}", answer=None, default=active[role], type=float, validate=lambda v: None if 0.02 <= float(v) <= 0.98 else "between 0.02 and 0.98")
        for role, frac in active.items():
            self.set(frac_keys[role], f"{frac:.2f}")

        for card, roles in per_card.items():
            total = sum(active[r] for r in roles)
            line = f"GPU {card}: " + " + ".join(f"{r} {active[r]:.2f}" for r in roles) + f" = {total:.2f}"
            (ui.ok if total < 1 else ui.warn)(line)
        if self.state.transcription:
            pool = self.cur("DIARIZATION_PIPELINE_POOL_SIZE", "2") or "2"
            ui.note(f"GPU {devices.get('diarization', '0')}: the diarization server is outside these fractions. Each of its {pool} pipelines (DIARIZATION_PIPELINE_POOL_SIZE in .env.vllm) holds memory from startup, so leave room for them.")

    def section_access(self) -> None:
        ui.header("How users reach the app")
        options = [
            ("letsencrypt", "HTTPS via nginx, certificate from Let's Encrypt (Cloudflare DNS-01; no inbound 80/443 needed)"),
            ("custom-cert", "HTTPS via nginx with a certificate you already have"),
            ("external-proxy", "behind your own reverse proxy / TLS terminator (it forwards to nginx, port 80 by default)"),
            ("direct", "plain HTTP via nginx, port 80 by default (localhost / LAN / evaluation)"),
            ("direct-no-nginx", "plain HTTP straight to the frontend port, no nginx (rare: /v1, /public and webhooks only reachable from this host)"),
        ]
        prev = self.previous.access_mode if self.previous and self.previous.access_mode in ACCESS_MODES else None
        if prev is None:
            tmpl = self.cur("NGINX_TEMPLATE", "")
            prev = "custom-cert" if tmpl == NGINX_TEMPLATES["custom-cert"] else ("letsencrypt" if tmpl == NGINX_TEMPLATES["letsencrypt"] or self.current.get("CLOUDFLARE_API_TOKEN") else "direct")
        mode = self.choice("Access mode", options, answer=self.a("access", "mode"), default=prev)
        self.state.access_mode = mode

        if mode in ("letsencrypt", "custom-cert"):
            domain = self.ask(
                "Domain name (the A record pointing at this server)",
                answer=self.a("access", "domain"),
                default=self.cur("NGINX_DOMAIN", "") if self.cur("NGINX_DOMAIN", "") not in ("", "example.com") else "",
                required=True,
                validate=_validate_domain,
            )
            self.state.domain = domain
            public_url = f"https://{domain}"
            self.set("NGINX_DOMAIN", domain)
            self.set("NGINX_CONTAINER_NAME", "orizon-nginx")
            if mode == "letsencrypt":
                self.set("CERTBOT_EMAIL", self.ask("Email for Let's Encrypt expiry notices", answer=self.a("access", "email"), default=self.cur("CERTBOT_EMAIL", ""), required=True, validate=lambda v: None if "@" in str(v) else "not an email address"))
                self.set("CLOUDFLARE_API_TOKEN", self.ask(
                    "Cloudflare API token (Zone > DNS > Edit on this zone)",
                    answer=self.a("access", "cloudflare_token"),
                    default=self.current.get("CLOUDFLARE_API_TOKEN", ""),
                    required=True,
                    secret=True,
                    help="Cloudflare > My Profile > API Tokens > Create Token. See docs/HTTPS_NGINX_CLOUDFLARE.md.",
                ))
                staging = self.confirm(
                    "Use Let's Encrypt STAGING first (untrusted test certificate, no rate limits)?",
                    answer=self.a("access", "staging"),
                    default=(self.cur("CERTBOT_STAGING", "true") or "true").lower() == "true",
                    help="Recommended for a first run. Switch off and run `orizonctl ssl issue` once the flow is verified.",
                )
                self.set("CERTBOT_STAGING", "true" if staging else "false")
                self.set("CERTBOT_FORCE_RECREATE", "false")
                # setup-ssl.sh switches to the HTTPS template once a certificate exists;
                # keep that switch if it already happened.
                already_https = self.cur("NGINX_TEMPLATE", "") == NGINX_TEMPLATES["letsencrypt"]
                self.set("NGINX_TEMPLATE", NGINX_TEMPLATES["letsencrypt"] if already_https else NGINX_TEMPLATES["http-only"])
            else:
                cert_dir = self.ask(
                    "Directory containing fullchain.pem and privkey.pem",
                    answer=self.a("access", "cert_dir"),
                    default=self.cur("SSL_CERT_DIR", "./certs") or "./certs",
                    required=True,
                    help="Relative paths are relative to the app directory. Files are checked at `orizonctl up`.",
                )
                self.set("SSL_CERT_DIR", cert_dir)
                self.set("NGINX_TEMPLATE", NGINX_TEMPLATES["custom-cert"])
        elif mode == "external-proxy":
            public_url = self.ask(
                "Public URL users open (what your proxy serves)",
                answer=self.a("access", "public_url"),
                default=self.cur("BETTER_AUTH_URL", "") if self.cur("BETTER_AUTH_URL", "").startswith("https") else "",
                required=True,
                validate=_validate_url,
                help="Your proxy should forward to nginx on this host (port 80 unless changed under Ports) and pass X-Forwarded-Proto.",
            )
            self.state.domain = urlparse(public_url).hostname or ""
            self.set("NGINX_TEMPLATE", NGINX_TEMPLATES["http-only"])
        else:
            default_url = self.cur("BETTER_AUTH_URL", "")
            if not default_url or "localhost" in default_url:
                default_url = _origin("http", h.primary_ip(), self._user_port())
            public_url = self.ask(
                "Address users will open in the browser",
                answer=self.a("access", "public_url"),
                default=default_url,
                required=True,
                validate=_validate_url,
                help="Session cookies are bound to this origin; a different hostname or IP will not sign in.",
            )
            self.state.domain = urlparse(public_url).hostname or ""
            self.set("NGINX_TEMPLATE", NGINX_TEMPLATES["http-only"])

        self.state.public_url = public_url.rstrip("/")
        self._apply_public_url()

    def _apply_public_url(self) -> None:
        url = self.state.public_url
        fp = self._port("FRONTEND_PORT")
        np = self._port("NGINX_HTTP_PORT")
        # localhost through nginx and straight to the frontend port
        origins = [url, _origin("http", "localhost", np), _origin("http", "127.0.0.1", np), f"http://localhost:{fp}", f"http://127.0.0.1:{fp}"]
        seen: list[str] = []
        for o in origins:
            if o not in seen:
                seen.append(o)
        self.set("FRONTEND_URLS", json.dumps(seen, separators=(",", ":")))
        self.set("BETTER_AUTH_URL", url)
        self.set("SECURE_COOKIES", "true" if url.startswith("https://") else "false")
        self.set("ORIZON_MCP_OAUTH_REDIRECT_URI", f"{url}/mcp/auth/callback")

    def _port(self, key: str) -> int:
        return int(self.values.get(key) or self.current.get(key) or DEFAULT_PORTS[key])

    def _user_port(self) -> int:
        """The port a browser opens in the direct modes: nginx's, or the frontend's without nginx."""
        return self._port("NGINX_HTTP_PORT" if self.state.uses_nginx else "FRONTEND_PORT")

    def section_ports(self) -> None:
        ui.header("Ports")
        asked = {
            "FRONTEND_PORT": ("frontend (published on all interfaces)", "frontend"),
            "SERVER_PORT": ("backend (published on loopback only)", "backend"),
            "POSTGRES_PORT": ("postgres (published on all interfaces; firewall it)", "postgres"),
        }
        if self.state.text_mode == "local":
            asked["VLLM_PORT"] = ("text vLLM server", "vllm")
        if self.state.embedding_mode == "local":
            asked["EMBEDDING_VLLM_PORT"] = ("embedding vLLM server", "embedding_vllm")
        if self.state.rerank_mode == "local":
            asked["RERANK_VLLM_PORT"] = ("rerank vLLM server", "rerank_vllm")
        if self.state.transcription:
            asked["TRANSCRIPTION_VLLM_PORT"] = ("transcription vLLM server", "transcription_vllm")
            asked["DIARIZATION_SERVER_PORT"] = ("diarization server", "diarization")
        if self.state.uses_nginx:
            asked["NGINX_HTTP_PORT"] = ("nginx http (what users or your proxy open)", "nginx_http")
            asked["NGINX_HTTPS_PORT"] = ("nginx https", "nginx_https")

        change = self.confirm("Change any of the default ports?", answer=None if self.a("ports") is None else True, default=False, help=", ".join(f"{k.lower().replace('_port', '')}={self.cur(k, str(DEFAULT_PORTS[k]))}" for k in asked))
        ports: dict[str, int] = {}
        for key, (label, akey) in asked.items():
            default = int(self.cur(key, str(DEFAULT_PORTS[key])) or DEFAULT_PORTS[key])
            value = self.ask(f"Port for {label}", answer=self.a("ports", akey), default=default, type=int, validate=_validate_port) if change else default
            ports[key] = int(value)
            if key in MODEL_SERVER_PORTS and value == DEFAULT_PORTS[key] and not self.current.get(key):
                continue
            self.set(key, value)
        self.set("SERVER_BIND_ADDRESS", self.cur("SERVER_BIND_ADDRESS", "127.0.0.1") or "127.0.0.1")
        self.set("FASTAPI_URL", f"http://backend:{self.values['SERVER_PORT']}")
        self.set("NGINX_SERVER_PORT", self.values["SERVER_PORT"])
        self.set("NGINX_FRONTEND_PORT", self.values["FRONTEND_PORT"])
        self._apply_internal_urls()
        # in the direct modes the browser opens this host itself, so the URL's
        # port is whatever serves it, which may have changed since the URL section ran
        if self.state.access_mode in ("direct", "direct-no-nginx"):
            u = urlparse(self.state.public_url)
            if u.hostname:
                self.state.public_url = _origin(u.scheme, u.hostname, self._user_port())
        self._apply_public_url()

        if len(set(ports.values())) != len(ports):
            raise WizardError("two services were given the same port")

    def _apply_internal_urls(self) -> None:
        """A locally served endpoint gets its in-cluster URL written out, port
        included. Leaving the key alone is not enough: a deployment moving from
        an external endpoint back to a bundled server would keep the old URL in
        .env, and an empty value would override the backend's own default."""
        v = self.values
        if self.state.text_mode == "local":
            url = f"http://vllm-server:{v.get('VLLM_PORT', DEFAULT_PORTS['VLLM_PORT'])}"
            self.set("LLM_API_URL", url)
            self.set("IMAGE_LLM_API_URL", url)
        if self.state.embedding_mode == "local":
            self.set("EMBEDDING_API_URL", f"http://embedding-vllm-server:{v.get('EMBEDDING_VLLM_PORT', DEFAULT_PORTS['EMBEDDING_VLLM_PORT'])}")
        if self.state.rerank_mode == "local":
            self.set("RERANK_API_URL", f"http://rerank-vllm-server:{v.get('RERANK_VLLM_PORT', DEFAULT_PORTS['RERANK_VLLM_PORT'])}")
        if self.state.transcription:
            self.set("TRANSCRIPTION_BOT_VLLM_API_URL", f"http://transcription-vllm-server:{v.get('TRANSCRIPTION_VLLM_PORT', DEFAULT_PORTS['TRANSCRIPTION_VLLM_PORT'])}")
            self.set("DIARIZATION_API_URL", f"http://diarization-server:{v.get('DIARIZATION_SERVER_PORT', DEFAULT_PORTS['DIARIZATION_SERVER_PORT'])}")

    def section_database(self) -> None:
        ui.header("Database")
        user = self.cur("POSTGRES_USER", "postgres") or "postgres"
        db = self.cur("POSTGRES_DB", "orizon") or "orizon"
        existing_pw = self.current.get("POSTGRES_PASSWORD", "")
        pw = self.a("database", "password") or (existing_pw if existing_pw and existing_pw != "postgres" else "")
        if not pw:
            pw = gen_password()
            ui.ok("Generated a random database password.")
        else:
            ui.ok("Keeping the existing database password.")
        self.set("POSTGRES_USER", user)
        self.set("POSTGRES_DB", db)
        self.set("POSTGRES_PASSWORD", pw)
        self.set("DATABASE_URL", f"postgresql://{user}:{pw}@db:5432/{db}")

    def section_secrets(self) -> None:
        ui.header("Secrets")
        plan = [
            ("BETTER_AUTH_SECRET", "better_auth_secret", lambda: gen_hex(32), "signs session cookies (frontend build arg)"),
            ("ORIZON_INTERNAL_API_KEY", "internal_api_key", lambda: gen_hex(32), "gates the backend API"),
            ("ORIZON_MCP_ENCRYPTION_KEY", "master_key", gen_master_key, "master key for every stored credential"),
            ("WEBHOOK_API_KEY", "webhook_api_key", lambda: gen_hex(16), "bearer token for api/instance/status"),
        ]
        for key, akey, gen, what in plan:
            given = self.a("secrets", akey)
            existing = self.current.get(key, "")
            if given:
                self.set(key, given)
                ui.ok(f"{key}: from answers file ({what})")
            elif existing:
                self.set(key, existing)
                ui.ok(f"{key}: kept ({what})")
            else:
                self.set(key, gen())
                ui.ok(f"{key}: generated ({what})")
        ui.note("ORIZON_MCP_ENCRYPTION_KEY is never regenerated by this tool. Rotate it only with\nbackend-orizon-ai/scripts/rotate_encryption_key.py, which re-encrypts the stored rows.")

    def section_mcp(self) -> None:
        ui.header("Connectors")
        self.state.mcp = self.confirm(
            "Run the MCP connector stack (HubSpot, Freshdesk, Freshcaller, Salesforce, Central, Microsoft, Google, code sandbox)?",
            answer=self.a("mcp"),
            default=self.previous.mcp if self.previous else False,
            help="Adds docker-compose.mcp.yml. Which tenant and OAuth app each connector uses is configured in the admin panel, not here.",
        )
        self.state.mcp_profiles = self._mcp_profiles() if self.state.mcp else []
        self.set("COMPOSE_PROFILES", ",".join((["transcription"] if self.state.transcription else []) + self.state.mcp_profiles))
        # A Compose bind mount the backend uses for SMB sources. Not asked
        # interactively (the default is right on Linux); the answers file may
        # move it, e.g. on a workstation where /mnt is read-only.
        smb = self.a("smb_mount_dir") or self.cur("ORIZON_SMB_MOUNT_BASE_DIR", "/mnt/orizon") or "/mnt/orizon"
        self.set("ORIZON_SMB_MOUNT_BASE_DIR", str(smb))

    def _mcp_profiles(self) -> list[str]:
        """The compose profiles of docker-compose.mcp.yml this deployment starts.

        Customer-specific servers (e.g. okasa) are under a profile so no other
        deployment runs them. Defaults to what this host already runs, read from
        COMPOSE_PROFILES and the previous state, so a rerun never drops one.
        """
        declared = declared_profiles(self.repo / "docker-compose.mcp.yml")
        kept = split_profiles(self.current.get("COMPOSE_PROFILES", "")) + (self.previous.mcp_profiles if self.previous else [])
        default = [p for p in dict.fromkeys(kept) if p != "transcription" and (not declared or p in declared)]

        def validate(v: Any) -> str | None:
            unknown = [p for p in split_profiles(v) if declared and p not in declared]
            if unknown:
                return f"not a profile in docker-compose.mcp.yml: {', '.join(unknown)} (it has: {', '.join(declared)})"
            return None

        answer = self.a("mcp_profiles")
        if answer is not None:
            err = validate(answer)
            if err:
                raise WizardError(f"mcp_profiles: {err} (from answers file)")
            return split_profiles(answer)
        if not declared and not default:
            return []
        raw = self.ask(
            "Extra connector profiles to start, comma-separated (empty for none)",
            answer=None,
            default=",".join(default),
            validate=validate,
            help=(
                f"Servers that only some deployments use run under a compose profile ({', '.join(declared) or 'none declared'}). "
                "`external` starts all of them, a name starts that one. Each is then attached in Settings > External MCP."
            ),
        )
        return split_profiles(raw)

    def section_transcription(self) -> None:
        username = self.cur("NEXT_PUBLIC_TELEGRAM_BOT_USERNAME", "")
        username = "" if username == "your-telegram-bot" else username
        if not self.state.transcription:
            self.set("NEXT_PUBLIC_TELEGRAM_BOT_USERNAME", username)
            return
        ui.header("Orizon Transcribe")
        hf = self.ask("HuggingFace token (pyannote diarization is a gated model)", answer=self.a("transcription", "hf_token") or self.a("hf_token"), default=self.current.get("HUGGINGFACE_TOKEN", ""), secret=True)
        if hf:
            self.set("HUGGINGFACE_TOKEN", hf)
        telegram = self.confirm(
            "Connect the Telegram bot too?",
            answer=self.a("transcription", "telegram"),
            default=bool(self.a("transcription", "bot_token") or self.current.get("BOT_TOKEN")),
            help="The Transcriptions page works without it. The bot runs inside the transcription service and starts only when its three credentials are set.",
        )
        self.set("ORIZON_FEATURE_TELEGRAM", "true" if telegram else "false")
        if not telegram:
            self.set("NEXT_PUBLIC_TELEGRAM_BOT_USERNAME", username)
            return
        ui.note("The transcription service has no access to the credential store, so these stay on disk in .env.secrets.")
        self.set("BOT_TOKEN", self.ask("Telegram bot token (from @BotFather)", answer=self.a("transcription", "bot_token"), default=self.current.get("BOT_TOKEN", ""), required=True, secret=True))
        self.set("API_ID", self.ask("Telegram API id (my.telegram.org)", answer=self.a("transcription", "api_id"), default=self.current.get("API_ID", ""), required=True))
        self.set("API_HASH", self.ask("Telegram API hash", answer=self.a("transcription", "api_hash"), default=self.current.get("API_HASH", ""), required=True, secret=True))
        self.set("NEXT_PUBLIC_TELEGRAM_BOT_USERNAME", self.ask(
            "Telegram bot username, without the @ (users link their account to it)",
            answer=self.a("telegram_bot_username"),
            default=username,
        ) or "")

    def section_downloads(self) -> None:
        ui.header("Model weights")
        if self.state.uses_gpu:
            self.state.download_models = self.confirm(
                "Download the selected models from HuggingFace during setup?",
                answer=self.a("download_models"),
                default=self.previous.download_models if self.previous else True,
                help="Skipped for any model whose directory already exists under MODELS_PATH. Also available later: orizonctl models download",
            )
        else:
            # Nothing on this host serves a model, but the weights can still be
            # fetched now (a GPU to be added later, or a slow link to use tonight).
            ui.info("No NVIDIA GPU detected (nvidia-smi): no model server runs on this host.")
            self.state.download_models = self.confirm(
                "Download the default model weights anyway?",
                answer=self.a("download_models"),
                default=self.previous.download_models if self.previous else False,
                help="The shipped defaults (" + ", ".join(DEFAULT_MODELS.values()) + "; ~42 GB) land under MODELS_PATH. "
                "Skipped for any model whose directory already exists. Also available later: orizonctl models download",
            )
            if self.state.download_models and "MODELS_PATH" not in self.values:
                self.ask_models_path()
        if self.state.download_models and "HUGGINGFACE_TOKEN" not in self.values:
            hf = self.ask("HuggingFace token (empty for public models only)", answer=self.a("hf_token"), default=self.current.get("HUGGINGFACE_TOKEN", ""), secret=True)
            if hf:
                self.set("HUGGINGFACE_TOKEN", hf)

    def finish(self) -> None:
        ui.header("Summary")
        s = self.state
        ui.kv("Version", s.version)
        ui.kv("Text model", f"{self.values.get('DEFAULT_MODEL')} ({s.text_mode})")
        ui.kv("Embedding model", f"{self.values.get('LLM_EMBEDDING_MODEL')} ({s.embedding_mode})")
        ui.kv("Reranker", "off" if s.rerank_mode == "off" else f"{self.values.get('RERANK_MODEL')} ({s.rerank_mode})")
        ui.kv("Orizon Transcribe", ("on, Telegram " + ("on" if self.values.get("ORIZON_FEATURE_TELEGRAM") == "true" else "off")) if s.transcription else "off")
        ui.kv("MCP connectors", ("on" + (f", profiles: {', '.join(s.mcp_profiles)}" if s.mcp_profiles else "")) if s.mcp else "off")
        ui.kv("Access", f"{s.access_mode} -> {s.public_url}")
        if s.keeps_models:
            ui.kv("Models path", self.values.get("MODELS_PATH", ""))
        ui.kv("Model weights", "download during setup" if s.download_models else "not downloaded")
        ui.kv("Services", ", ".join(s.all_services()) + (" + MCP stack" if s.mcp else ""))


# --------------------------------------------------------------- validators


def split_profiles(v: Any) -> list[str]:
    """A COMPOSE_PROFILES value or an answers-file list, as unique names in order."""
    items = v if isinstance(v, (list, tuple)) else str(v or "").split(",")
    return list(dict.fromkeys(str(p).strip() for p in items if str(p).strip()))


def declared_profiles(compose_file: Path) -> list[str]:
    """The profiles the services of `compose_file` are under, sorted; empty when it
    is missing or unreadable, which turns the name check off rather than failing."""
    try:
        data = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    names = {str(p) for svc in (data.get("services") or {}).values() if isinstance(svc, dict) for p in svc.get("profiles") or []}
    return sorted(names)


def _validate_url(v: Any) -> str | None:
    u = urlparse(str(v))
    if u.scheme not in ("http", "https") or not u.netloc:
        return "expected http(s)://host[:port]"
    if u.path.rstrip("/").endswith("/v1"):
        return "leave off the trailing /v1; the backend appends it"
    return None


def _normalize_prefix(v: Any) -> str:
    """What an operator pastes from a browser or a `docker pull` line, reduced
    to <host>/<project>: scheme, surrounding whitespace and slashes dropped."""
    s = str(v).strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    return s.strip().strip("/")


def _proxy_cache_alt(prefix: str, host: str) -> str | None:
    """For <host>/<project> on a private registry, the Harbor proxy-cache shape
    <host>/<project>/<project>; None when the prefix is not that shape."""
    if host == "docker.io" or prefix.count("/") != 1:
        return None
    return f"{prefix}/{prefix.rsplit('/', 1)[-1]}"


def _validate_prefix(v: Any) -> str | None:
    s = _normalize_prefix(v)
    if not s or any(c.isspace() for c in s) or ":" in s.split("/", 1)[-1]:
        return f"{str(v).strip()!r}: expected <registry host>/<project path>, no spaces and no image or tag, e.g. scr.syneto.eu/orizon-ai/orizon-ai"
    first = s.split("/")[0]
    looks_like_host = "." in first or ":" in first or first == "localhost"
    if looks_like_host and "/" not in s:
        return f"{first} is a registry host; add the project/namespace, e.g. {first}/orizon-ai (a push to the bare host fails with 400)"
    return None


def _origin(scheme: str, host: str, port: int) -> str:
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + ("" if port == default else f":{port}")


def _validate_domain(v: Any) -> str | None:
    s = str(v).strip().lower()
    if "://" in s or "/" in s or " " in s or "." not in s:
        return "a bare hostname like ai.example.com"
    return None


def _validate_port(v: Any) -> str | None:
    try:
        n = int(v)
    except ValueError:
        return "not a number"
    return None if 1 <= n <= 65535 else "1-65535"


def load_answers(path: Path) -> dict[str, Any]:
    """The wizard's answers from a config file (YAML or JSON): the `configure:`
    section of a sectioned document, or the whole document of a bare one."""
    from .config import load_config

    cfg = load_config(path)
    cfg.warn_unknown()
    return cfg.configure
