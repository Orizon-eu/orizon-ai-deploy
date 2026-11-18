import json
import os
import stat
from pathlib import Path

import pytest

from orizonctl import hostinfo
from orizonctl.envfile import load
from orizonctl.state import State
from orizonctl.wizard import Wizard, WizardError
from orizonctl.writer import write_env_files

GPU_ANSWERS = {
    "version": "v1.2.0",
    "models": {"text": "local", "embedding": "local", "rerank": "local", "transcription": True},
    "models_path": "/srv/orizon-models",
    "access": {"mode": "letsencrypt", "domain": "ai.example.com", "email": "ops@example.com", "cloudflare_token": "cf-token", "staging": True},
    "mcp": True,
    "transcription": {"bot_token": "123:abc", "api_id": "4242", "api_hash": "hash", "hf_token": "hf_x"},
    "download_models": False,
}

CPU_ANSWERS = {
    "version": "v1.2.0",
    "models": {"text": "external", "embedding": "external", "rerank": "off"},
    "external": {
        "text": {"url": "https://api.openai.com", "model": "gpt-4o", "api_key": "sk-test"},
        "embedding": {"url": "https://api.openai.com", "model": "text-embedding-3-small"},
    },
    "access": {"mode": "direct", "public_url": "http://10.0.0.5:3000"},
    "mcp": False,
}


@pytest.fixture
def fake_gpu(monkeypatch):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [hostinfo.Gpu(0, "NVIDIA H100", 81559, "560.35")])


@pytest.fixture
def no_gpu(monkeypatch):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])


def run_wizard(repo, answers):
    return Wizard(repo, interactive=False, answers=answers, previous=State.load(repo)).run()


def test_gpu_letsencrypt_full_stack(repo, fake_gpu):
    values, state = run_wizard(repo, GPU_ANSWERS)
    assert state.all_services() == ["db", "vllm-server", "embedding-vllm-server", "rerank-vllm-server", "transcription-vllm-server", "diarization-server", "frontend", "backend", "transcription-bot", "nginx", "certbot"]
    assert state.public_url == "https://ai.example.com"
    assert values["SECURE_COOKIES"] == "true"
    assert values["NGINX_TEMPLATE"] == "orizonai-http-only.conf.template"  # until setup-ssl.sh switches it
    assert values["ORIZON_MCP_OAUTH_REDIRECT_URI"] == "https://ai.example.com/mcp/auth/callback"
    assert json.loads(values["FRONTEND_URLS"])[0] == "https://ai.example.com"
    # GPU fractions were rescaled to fit one card
    total = sum(float(values[k]) for k in ("GPU_MEMORY_UTILIZATION", "EMBEDDING_GPU_MEMORY_UTILIZATION", "RERANK_GPU_MEMORY_UTILIZATION", "TRANSCRIPTION_BOT_GPU_MEMORY_UTILIZATION"))
    assert total < 1.0
    # secrets generated
    assert len(values["BETTER_AUTH_SECRET"]) == 64
    assert len(values["ORIZON_INTERNAL_API_KEY"]) == 64
    assert values["ORIZON_MCP_ENCRYPTION_KEY"].endswith("=")
    assert values["DATABASE_URL"] == f"postgresql://postgres:{values['POSTGRES_PASSWORD']}@db:5432/orizon"
    # local endpoints are written explicitly so a stale external URL can never linger
    assert values["LLM_API_URL"] == "http://vllm-server:8001"
    assert values["EMBEDDING_API_URL"] == "http://embedding-vllm-server:8006"
    assert values["RERANK_API_URL"] == "http://rerank-vllm-server:8004"
    assert values["TRANSCRIPTION_BOT_VLLM_API_URL"] == "http://transcription-vllm-server:8002"
    assert values["DIARIZATION_API_URL"] == "http://diarization-server:8008"
    assert values["POSTGRES_PASSWORD"] != "postgres"
    assert values["COMPOSE_PROFILES"] == "transcription"
    assert values["ORIZON_FEATURE_TRANSCRIPTION"] == "true"
    assert values["ORIZON_FEATURE_TELEGRAM"] == "true"
    assert values["DIARIZATION_GPU_DEVICE"] == "0"
    assert "TRANSCRIPTION_BOT_INTERNAL_GPU_DEVICE" not in values


def test_files_written_with_modes_and_routing(repo, fake_gpu):
    values, state = run_wizard(repo, GPU_ANSWERS)
    written = write_env_files(repo, values)
    names = sorted(p.name for p in written)
    assert names == [".env", ".env.mcp", ".env.nginx", ".env.secrets", ".env.vllm"]
    assert stat.S_IMODE(os.stat(repo / ".env.secrets").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(repo / ".env.nginx").st_mode) == 0o600

    env = load(repo / ".env")
    secrets = load(repo / ".env.secrets")
    vllm = load(repo / ".env.vllm")
    nginx = load(repo / ".env.nginx")

    assert env["ORIZON_VERSION"] == "v1.2.0"
    assert env["COMPOSE_PROJECT_NAME"] == "orizon-ai-app"
    assert "CLOUDFLARE_API_TOKEN" not in env and nginx["CLOUDFLARE_API_TOKEN"] == "cf-token"
    assert "ORIZON_INTERNAL_API_KEY" not in env and secrets["ORIZON_INTERNAL_API_KEY"] == values["ORIZON_INTERNAL_API_KEY"]
    assert secrets["BOT_TOKEN"] == "123:abc" and secrets["HUGGINGFACE_TOKEN"] == "hf_x"
    # the two statements about the model stay in step
    assert vllm["DEFAULT_MODEL"] == env["DEFAULT_MODEL"]
    assert vllm["DIARIZATION_MODEL"] == env["DIARIZATION_MODEL"] == "pyannote/speaker-diarization-3.1"
    assert vllm["DIARIZATION_MODEL_LOCAL_DIR"] == "speaker-diarization-3.1" and "DIARIZATION_MODEL_LOCAL_DIR" not in env
    assert env["MODELS_PATH"] == "/srv/orizon-models" and "MODELS_PATH" not in vllm and "HF_HOME" not in env
    assert vllm["MODELS_SERVE_PATH"] == env["MODELS_SERVE_PATH"]
    assert nginx["SERVER_PORT"] == env["SERVER_PORT"] and nginx["FRONTEND_PORT"] == env["FRONTEND_PORT"]
    for key in ("VLLM_PORT", "EMBEDDING_VLLM_PORT", "RERANK_VLLM_PORT", "TRANSCRIPTION_VLLM_PORT", "DIARIZATION_SERVER_PORT"):
        assert key not in env and key not in vllm, key
    assert values["LLM_API_URL"] == "http://vllm-server:8001"
    assert values["DIARIZATION_API_URL"] == "http://diarization-server:8008"
    # the template's header and sections survived
    text = (repo / ".env").read_text()
    assert text.startswith("# The app, and everything Compose reads at parse time.")
    assert "# ================= SERVICE PORTS =================" in text
    # a value that was only commented out in the template got uncommented, not appended
    assert "\nEMBEDDING_VLLM_GPU_DEVICE=0\n" in text
    assert "\nCOMPOSE_PROJECT_NAME=orizon-ai-app\n" in text


@pytest.mark.parametrize("profile", ["gpu", "cpu"])
def test_every_key_the_wizard_writes_has_a_line_in_its_template(repo, monkeypatch, profile):
    gpus = [hostinfo.Gpu(0, "NVIDIA H100", 81559, "560.35")] if profile == "gpu" else []
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: gpus)
    values, _ = run_wizard(repo, GPU_ANSWERS if profile == "gpu" else CPU_ANSWERS)
    write_env_files(repo, values)
    for name in (".env", ".env.secrets", ".env.vllm", ".env.nginx"):
        path = repo / name
        if path.exists():
            assert "set by orizonctl" not in path.read_text(), f"{name} has keys its template lacks"


def test_transcription_without_telegram(repo, fake_gpu):
    answers = {**GPU_ANSWERS, "transcription": {"hf_token": "hf_x", "telegram": False}}
    values, state = run_wizard(repo, answers)
    assert "diarization-server" in state.all_services() and "transcription-bot" in state.all_services()
    assert values["ORIZON_FEATURE_TRANSCRIPTION"] == "true"
    assert values["ORIZON_FEATURE_TELEGRAM"] == "false"
    assert "BOT_TOKEN" not in values and "API_ID" not in values and "API_HASH" not in values
    assert values["NEXT_PUBLIC_TELEGRAM_BOT_USERNAME"] == ""


def test_port_moved_out_of_env_vllm_keeps_its_value(repo, fake_gpu):
    (repo / ".env.vllm").write_text((repo / "env.example.vllm").read_text() + "VLLM_PORT=9001\nDIARIZATION_SERVER_PORT=9008\n")
    (repo / ".env").write_text((repo / "env.example").read_text().replace("\nVLLM_PORT=8001\n", "\n").replace("\nDIARIZATION_SERVER_PORT=8008\n", "\n"))
    values, _ = run_wizard(repo, GPU_ANSWERS)
    write_env_files(repo, values)
    env, vllm = load(repo / ".env"), load(repo / ".env.vllm")
    assert env["VLLM_PORT"] == "9001" and env["DIARIZATION_SERVER_PORT"] == "9008"
    assert "VLLM_PORT" not in vllm and "DIARIZATION_SERVER_PORT" not in vllm
    assert values["LLM_API_URL"] == "http://vllm-server:9001"
    assert values["DIARIZATION_API_URL"] == "http://diarization-server:9008"


def test_rerun_keeps_secrets_and_password(repo, fake_gpu):
    values1, _ = run_wizard(repo, GPU_ANSWERS)
    write_env_files(repo, values1)
    State().save(repo)
    values2, _ = run_wizard(repo, {**GPU_ANSWERS, "access": {**GPU_ANSWERS["access"], "staging": False}})
    for k in ("ORIZON_MCP_ENCRYPTION_KEY", "BETTER_AUTH_SECRET", "ORIZON_INTERNAL_API_KEY", "WEBHOOK_API_KEY", "POSTGRES_PASSWORD"):
        assert values2[k] == values1[k], k
    assert values2["CERTBOT_STAGING"] == "false"
    write_env_files(repo, values2)
    assert load(repo / ".env.nginx")["CERTBOT_STAGING"] == "false"
    assert (repo / ".env").read_text().count("set by orizonctl") <= 1


def test_cpu_only_external_direct(repo, no_gpu):
    values, state = run_wizard(repo, CPU_ANSWERS)
    assert state.all_services() == ["db", "frontend", "backend", "nginx"]
    assert not state.uses_gpu
    assert values["NGINX_TEMPLATE"] == "orizonai-http-only.conf.template"
    assert values["LLM_API_URL"] == "https://api.openai.com"
    assert values["LLM_DEPLOYMENT_MODE"] == "external"
    assert values["IMAGE_LLM_API_URL"] == "https://api.openai.com"
    assert values["RERANK_MODEL"] == ""
    assert values["SECURE_COOKIES"] == "false"
    # direct goes through nginx, so the URL's port follows nginx (80), not the frontend's
    assert values["BETTER_AUTH_URL"] == "http://10.0.0.5"
    assert "MODELS_PATH" not in values
    write_env_files(repo, values)
    secrets = load(repo / ".env.secrets")
    assert secrets["LLM_API_KEY"] == "sk-test" and secrets["IMAGE_API_KEY"] == "sk-test"
    env = load(repo / ".env")
    assert env["LLM_API_URL"] == "https://api.openai.com"
    assert env["COMPOSE_PROFILES"] == ""
    assert env["ORIZON_FEATURE_TRANSCRIPTION"] == "false"
    # .env.vllm is seeded from the template untouched so a later `up vllm-server` by hand finds a documented file
    assert (repo / ".env.vllm").exists()


def test_direct_on_a_custom_nginx_port(repo, no_gpu):
    answers = {**CPU_ANSWERS, "ports": {"nginx_http": 3000, "frontend": 3001}}
    values, state = run_wizard(repo, answers)
    assert values["NGINX_HTTP_PORT"] == "3000" and values["FRONTEND_PORT"] == "3001"
    assert state.required_ports(values)[3000] == "nginx (http)"
    assert values["BETTER_AUTH_URL"] == "http://10.0.0.5:3000"
    origins = json.loads(values["FRONTEND_URLS"])
    assert "http://localhost:3000" in origins and "http://localhost:3001" in origins


def test_direct_no_nginx(repo, no_gpu):
    answers = {**CPU_ANSWERS, "access": {"mode": "direct-no-nginx", "public_url": "http://10.0.0.5:3000"}}
    values, state = run_wizard(repo, answers)
    assert state.all_services() == ["db", "frontend", "backend"]
    assert not state.uses_nginx
    assert 80 not in state.required_ports(values) and 443 not in state.required_ports(values)
    assert values["BETTER_AUTH_URL"] == "http://10.0.0.5:3000"


def test_no_gpu_refuses_local_answer(repo, no_gpu):
    with pytest.raises(WizardError):
        run_wizard(repo, {**CPU_ANSWERS, "models": {"text": "local", "embedding": "external", "rerank": "off"}})


def test_url_with_v1_is_rejected(repo, no_gpu):
    bad = json.loads(json.dumps(CPU_ANSWERS))
    bad["external"]["text"]["url"] = "https://api.openai.com/v1"
    with pytest.raises(WizardError):
        run_wizard(repo, bad)


def test_non_interactive_requires_domain_for_letsencrypt(repo, fake_gpu):
    a = json.loads(json.dumps(GPU_ANSWERS))
    del a["access"]["domain"]
    with pytest.raises(WizardError):
        run_wizard(repo, a)


def test_old_layout_puts_everything_in_env(old_layout_repo, fake_gpu):
    values, _ = run_wizard(old_layout_repo, GPU_ANSWERS)
    written = write_env_files(old_layout_repo, values)
    assert [p.name for p in written] == [".env"]
    env = load(old_layout_repo / ".env")
    assert env["NGINX_DOMAIN"] == "ai.example.com"
    assert env["CLOUDFLARE_API_TOKEN"] == "cf-token"
    assert env["ORIZON_INTERNAL_API_KEY"] == values["ORIZON_INTERNAL_API_KEY"]
    assert env["GPU_MEMORY_UTILIZATION"] == values["GPU_MEMORY_UTILIZATION"]


def test_ports_and_state_roundtrip(repo, fake_gpu):
    a = json.loads(json.dumps(GPU_ANSWERS))
    a["ports"] = {"frontend": 8080, "backend": 9005}
    values, state = run_wizard(repo, a)
    assert values["FRONTEND_PORT"] == "8080" and values["SERVER_PORT"] == "9005"
    assert values["FASTAPI_URL"] == "http://backend:9005"
    a["ports"]["vllm"] = 9001
    values, state = run_wizard(repo, a)
    assert values["LLM_API_URL"] == "http://vllm-server:9001"
    ports = state.required_ports(values)
    assert ports[8080] == "frontend" and ports[9005].startswith("backend") and 80 in ports and 8107 in ports
    assert state.bind_addresses(values) == {9005: "127.0.0.1"}
    state.save(repo)
    back = State.load(repo)
    assert back is not None and back.all_services() == state.all_services() and back.mcp


def test_duplicate_ports_rejected(repo, fake_gpu):
    a = json.loads(json.dumps(GPU_ANSWERS))
    a["ports"] = {"frontend": 8005}
    with pytest.raises(WizardError):
        run_wizard(repo, a)


def test_rerun_keeps_a_connector_profile_beside_transcription(repo, fake_gpu):
    (repo / ".env").write_text((repo / "env.example").read_text().replace("\nCOMPOSE_PROFILES=transcription\n", "\nCOMPOSE_PROFILES=transcription,okasa\n"))
    values, state = run_wizard(repo, GPU_ANSWERS)
    assert values["COMPOSE_PROFILES"] == "transcription,okasa"
    assert state.mcp_profiles == ["okasa"]


def test_a_profile_kept_only_in_state_survives(repo, fake_gpu):
    State(mcp=True, mcp_profiles=["external"]).save(repo)
    values, state = run_wizard(repo, {**GPU_ANSWERS, "models": {**GPU_ANSWERS["models"], "transcription": False}})
    assert values["COMPOSE_PROFILES"] == "external"


def test_answers_file_sets_and_clears_connector_profiles(repo, no_gpu):
    values, state = run_wizard(repo, {**CPU_ANSWERS, "mcp": True, "mcp_profiles": ["okasa", "okasa", "external"]})
    assert values["COMPOSE_PROFILES"] == "okasa,external"
    write_env_files(repo, values)
    values, state = run_wizard(repo, {**CPU_ANSWERS, "mcp": True, "mcp_profiles": []})
    assert values["COMPOSE_PROFILES"] == "" and state.mcp_profiles == []


def test_unknown_connector_profile_is_refused(repo, no_gpu):
    with pytest.raises(WizardError, match="not a profile in docker-compose.mcp.yml: okasaa"):
        run_wizard(repo, {**CPU_ANSWERS, "mcp": True, "mcp_profiles": "okasaa"})


def test_connector_profiles_drop_with_the_mcp_stack(repo, no_gpu):
    (repo / ".env").write_text((repo / "env.example").read_text().replace("\nCOMPOSE_PROFILES=transcription\n", "\nCOMPOSE_PROFILES=okasa\n"))
    values, state = run_wizard(repo, CPU_ANSWERS)
    assert values["COMPOSE_PROFILES"] == "" and state.mcp_profiles == []


def test_no_gpu_can_still_download_default_weights(repo, no_gpu):
    from orizonctl.models import DIARIZATION_DEPENDENCIES, catalog
    from orizonctl.schema import DEFAULT_MODELS

    # default: nothing is downloaded on a GPU-less host
    values, state = run_wizard(repo, CPU_ANSWERS)
    assert state.uses_gpu is False
    assert state.download_models is False
    assert state.keeps_models is False
    assert "MODELS_PATH" not in values
    assert catalog(state, values) == []
    # an explicit `orizonctl models download` still knows what to fetch
    assert [i.repo_id for i in catalog(state, values, download=True)] == list(DEFAULT_MODELS.values()) + list(DIARIZATION_DEPENDENCIES)

    # asked for: the shipped defaults, under the given path
    values, state = run_wizard(repo, {**CPU_ANSWERS, "download_models": True, "models_path": "/data/models", "hf_token": "hf_y"})
    assert state.download_models is True
    assert state.keeps_models is True
    assert values["MODELS_PATH"] == "/data/models"
    assert values["HUGGINGFACE_TOKEN"] == "hf_y"
    items = catalog(state, values)
    assert [i.repo_id for i in items] == list(DEFAULT_MODELS.values()) + list(DIARIZATION_DEPENDENCIES)
    assert [i.local_dir for i in items] == [
        "Qwen3.6-35B-A3B-FP8",
        "nomic-embed-text-v2-moe",
        "Qwen3-Reranker-0.6B",
        "Qwen3-ASR-1.7B",
        "speaker-diarization-3.1",
        "hub/models--pyannote--segmentation-3.0",
        "hub/models--pyannote--wespeaker-voxceleb-resnet34-LM",
        "hub/models--pyannote--speaker-diarization-community-1",
    ]
