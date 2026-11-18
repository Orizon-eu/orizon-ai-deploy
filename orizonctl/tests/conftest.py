import shutil
from pathlib import Path

import pytest

from orizonctl import compose, registry

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE = Path(__file__).resolve().parents[1] / "orizonctl" / "assets"
TEMPLATES = ["env.example", "env.example.secrets", "env.example.vllm", "env.example.mcp", "env.example.nginx"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An app directory as orizonctl lays it out: the bundled templates plus the compose files."""
    for name in TEMPLATES + ["docker-compose.yml", "docker-compose.mcp.yml"]:
        src = (BUNDLE if name in TEMPLATES else REPO_ROOT) / name
        if src.exists():
            shutil.copy(src, tmp_path / name)
    (tmp_path / "nginx").mkdir()
    for t in ("orizonai-http-only.conf.template", "orizonai.conf.template", "orizonai-custom-cert.conf.template"):
        (tmp_path / "nginx" / t).write_text("# nginx template\n")
    return tmp_path


@pytest.fixture
def old_layout_repo(tmp_path: Path) -> Path:
    """A checkout from before the env split: one env.example, nothing else."""
    shutil.copy(REPO_ROOT / "env.example", tmp_path / "env.example")
    shutil.copy(REPO_ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    return tmp_path


@pytest.fixture(autouse=True)
def no_registry_calls(monkeypatch):
    """The wizard probes the registry; tests must never reach the network."""
    monkeypatch.setattr(compose, "image_accessible", lambda ref, runtime="docker": (True, "stubbed"))
    monkeypatch.setattr(compose, "registry_login", lambda host, user, token, runtime="docker": (True, "stubbed"))
    monkeypatch.setattr(registry, "list_tags", lambda prefix, image, creds=None, timeout=20: [])
