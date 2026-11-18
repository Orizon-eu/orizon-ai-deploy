"""Model weight downloads from HuggingFace into MODELS_PATH.

Each model lands at <MODELS_PATH>/<basename of the repo id>, which is what the
bundled vLLM entrypoints resolve `DEFAULT_MODEL` etc. to.

The diarization pipeline is a recipe: pyannote loads the models it names
(segmentation, speaker embedding, PLDA) by their HuggingFace ids, and the
diarization server runs offline. Those land in the HuggingFace cache under
<MODELS_PATH>/hub, which is the server's HF_HOME/hub.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ui
from .schema import DEFAULT_MODELS
from .state import State

# Rough on-disk size of the shipped defaults, for the disk check before a
# download. Anything else is "unknown".
APPROX_GB = {
    "Qwen/Qwen3.6-35B-A3B-FP8": 36,
    "openai/gpt-oss-20b": 14,
    "nomic-ai/nomic-embed-text-v2-moe": 1,
    "Qwen/Qwen3-Reranker-0.6B": 1.2,
    "Qwen/Qwen3-ASR-1.7B": 3.5,
    "pyannote/speaker-diarization-3.1": 0.1,
    "pyannote/segmentation-3.0": 0.01,
    "pyannote/wespeaker-voxceleb-resnet34-LM": 0.03,
    "pyannote/speaker-diarization-community-1": 0.04,
    "google/gemma-3-4b-it": 8.6,
}

CACHE_DIR = "hub"

DIARIZATION_DEPENDENCIES = (
    "pyannote/segmentation-3.0",
    "pyannote/wespeaker-voxceleb-resnet34-LM",
    "pyannote/speaker-diarization-community-1",
)


@dataclass
class ModelItem:
    repo_id: str
    local_dir: str
    role: str
    cache: bool = False

    @property
    def approx_gb(self) -> float | None:
        return APPROX_GB.get(self.repo_id)


def default_catalog(values: dict[str, str]) -> list[ModelItem]:
    """Every shipped default model: what a host without a GPU downloads when
    the operator wants the weights in place for a GPU added later."""
    out = []
    for role, rid in DEFAULT_MODELS.items():
        local_dir = rid.rsplit("/", 1)[-1]
        if role == "diarization":
            local_dir = values.get("DIARIZATION_MODEL_LOCAL_DIR") or "speaker-diarization-3.1"
        out.append(ModelItem(rid, local_dir, role))
        if role == "diarization":
            out += diarization_dependencies()
    return out


def cache_folder(repo_id: str) -> str:
    return f"{CACHE_DIR}/models--{repo_id.replace('/', '--')}"


def diarization_dependencies() -> list[ModelItem]:
    return [ModelItem(rid, cache_folder(rid), "diarization", cache=True) for rid in DIARIZATION_DEPENDENCIES]


def catalog(state: State, values: dict[str, str], *, download: bool = False) -> list[ModelItem]:
    """The models this deployment keeps on disk. With a local server: the
    configured ones. Without a GPU: the shipped defaults, when the operator
    said so at setup or `download` forces it; otherwise nothing."""
    if not state.uses_gpu:
        return default_catalog(values) if (download or state.download_models) else []
    items: list[ModelItem] = []

    def add(role: str, key: str, local_dir: str | None = None):
        rid = values.get(key, "")
        if rid:
            items.append(ModelItem(rid, local_dir or rid.rsplit("/", 1)[-1], role))

    if state.text_mode == "local":
        add("text", "DEFAULT_MODEL")
    if state.embedding_mode == "local":
        add("embedding", "LLM_EMBEDDING_MODEL")
    if state.rerank_mode == "local":
        add("rerank", "RERANK_MODEL")
    if state.transcription:
        add("transcription", "TRANSCRIPTION_MODEL")
        add("diarization", "DIARIZATION_MODEL", values.get("DIARIZATION_MODEL_LOCAL_DIR") or "speaker-diarization-3.1")
        items += diarization_dependencies()
    # de-duplicate (image model == text model, etc.)
    seen: set[str] = set()
    out = []
    for it in items:
        if it.repo_id not in seen:
            seen.add(it.repo_id)
            out.append(it)
    return out


def present(models_path: Path, item: ModelItem) -> bool:
    d = models_path / item.local_dir
    if item.cache:
        d = d / "snapshots"
    return d.is_dir() and any(d.iterdir())


def download(models_path: Path, items: list[ModelItem], token: str | None, force: bool = False) -> bool:
    try:
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError
    except ImportError:  # pragma: no cover
        ui.fail("huggingface_hub is not installed in this environment (pip install huggingface_hub)")
        return False

    models_path.mkdir(parents=True, exist_ok=True)
    all_ok = True
    for it in items:
        target = models_path / it.local_dir
        if present(models_path, it) and not force:
            ui.ok(f"{it.role:<14} {it.repo_id} already at {target}")
            continue
        size = f" (~{it.approx_gb:g} GB)" if it.approx_gb else ""
        ui.step(f"Downloading {it.repo_id}{size} -> {target}")
        try:
            if it.cache:
                snapshot_download(repo_id=it.repo_id, cache_dir=str(models_path / CACHE_DIR), token=token or None, max_workers=8)
            else:
                snapshot_download(repo_id=it.repo_id, local_dir=str(target), token=token or None, max_workers=8)
            ui.ok(f"{it.role:<14} {it.repo_id}")
        except GatedRepoError:
            all_ok = False
            ui.fail(f"{it.repo_id} is gated: accept its terms on huggingface.co and pass a token (HUGGINGFACE_TOKEN or --token)")
        except RepositoryNotFoundError:
            all_ok = False
            ui.fail(f"{it.repo_id}: repository not found (private without a token, or a typo)")
        except KeyboardInterrupt:
            ui.warn("download interrupted; re-run to resume (partial files are reused)")
            return False
        except Exception as e:  # network errors, disk full
            all_ok = False
            ui.fail(f"{it.repo_id}: {e}")
    return all_ok
