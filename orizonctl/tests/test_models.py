import sys
import types

from orizonctl import models
from orizonctl.state import State

VALUES = {
    "DEFAULT_MODEL": "Qwen/Qwen3.6-35B-A3B-FP8",
    "TRANSCRIPTION_MODEL": "Qwen/Qwen3-ASR-1.7B",
    "DIARIZATION_MODEL": "pyannote/speaker-diarization-3.1",
    "DIARIZATION_MODEL_LOCAL_DIR": "pyannote-speaker-diarization-3.1",
}


def _by_repo(items):
    return {i.repo_id: i for i in items}


def test_transcription_brings_every_model_the_diarization_pipeline_loads():
    st = State(text_mode="local", embedding_mode="external", rerank_mode="off", transcription=True)
    items = _by_repo(models.catalog(st, VALUES))
    assert items["Qwen/Qwen3-ASR-1.7B"].local_dir == "Qwen3-ASR-1.7B"
    assert items["pyannote/speaker-diarization-3.1"].local_dir == "pyannote-speaker-diarization-3.1"
    for rid in models.DIARIZATION_DEPENDENCIES:
        it = items[rid]
        assert it.role == "diarization" and it.cache
        assert it.local_dir == "hub/models--" + rid.replace("/", "--")


def test_no_diarization_dependencies_without_transcription():
    st = State(text_mode="local", embedding_mode="external", rerank_mode="off", transcription=False)
    assert not any(i.cache for i in models.catalog(st, VALUES))


def test_default_catalog_on_a_host_without_gpu_includes_the_dependencies():
    repos = {i.repo_id for i in models.default_catalog(VALUES)}
    assert set(models.DIARIZATION_DEPENDENCIES) <= repos


def test_present_needs_a_snapshot_for_cache_entries(tmp_path):
    it = models.diarization_dependencies()[0]
    repo = tmp_path / it.local_dir
    (repo / "blobs").mkdir(parents=True)
    assert not models.present(tmp_path, it)
    (repo / "snapshots" / "abc").mkdir(parents=True)
    assert models.present(tmp_path, it)


def test_download_puts_dependencies_in_the_cache_and_models_in_folders(tmp_path, monkeypatch):
    calls = []

    def fake_snapshot_download(**kw):
        calls.append(kw)
        return "ok"

    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = fake_snapshot_download
    utils = types.ModuleType("huggingface_hub.utils")
    utils.GatedRepoError = type("GatedRepoError", (Exception,), {})
    utils.RepositoryNotFoundError = type("RepositoryNotFoundError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", utils)

    items = [models.ModelItem("pyannote/speaker-diarization-3.1", "pyannote-speaker-diarization-3.1", "diarization")] + models.diarization_dependencies()
    assert models.download(tmp_path, items, token="t")
    by_repo = {c["repo_id"]: c for c in calls}
    assert by_repo["pyannote/speaker-diarization-3.1"]["local_dir"] == str(tmp_path / "pyannote-speaker-diarization-3.1")
    for rid in models.DIARIZATION_DEPENDENCIES:
        assert by_repo[rid]["cache_dir"] == str(tmp_path / "hub")
        assert "local_dir" not in by_repo[rid]
