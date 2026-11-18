import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from orizonctl import hostinfo
from orizonctl.cli import main
from orizonctl.config import CONFIGURE_KEYS, INSTALL_KEYS, ConfigError, example_names, example_text, load_config
from orizonctl.state import State
from orizonctl.wizard import Wizard

HERE = Path(__file__).resolve().parent.parent
INSTALL_SH = HERE.parent.parent / "install.sh"

CPU_YAML = """
install:
  version: v1.2.0
  skip_nvidia: true
configure:
  version: v1.2.0
  registry: {prefix: sadramoghadam, login: false}
  models: {text: external, embedding: external, rerank: off}
  external:
    text: {url: http://llm.internal:8000, model: qwen}
    embedding: {url: http://emb.internal:8000, model: nomic-ai/nomic-embed-text-v2-moe}
  access: {mode: direct, public_url: http://10.0.0.5:3000}
"""


def test_sectioned_yaml(tmp_path):
    p = tmp_path / "orizon.yaml"
    p.write_text(CPU_YAML)
    cfg = load_config(p)
    assert cfg.install == {"version": "v1.2.0", "skip_nvidia": True}
    assert cfg.configure["access"]["public_url"] == "http://10.0.0.5:3000"
    assert cfg.configure["models"]["rerank"] == "off"  # YAML 1.1 reads a bare `off` as False; put back
    assert cfg.unknown == []


def test_bare_answers_json_still_works(tmp_path):
    p = tmp_path / "answers.json"
    p.write_text(json.dumps({"version": "v1.2.0", "models": {"text": "external", "embedding": "external", "rerank": "off"}}))
    cfg = load_config(p)
    assert cfg.install == {} and cfg.configure["version"] == "v1.2.0" and not cfg.unknown


def test_bare_answers_yaml(tmp_path):
    p = tmp_path / "a.yml"
    p.write_text("version: v1.2.0\nmcp: true\n")
    cfg = load_config(p)
    assert cfg.configure == {"version": "v1.2.0", "mcp": True}


def test_unknown_keys_are_reported(tmp_path):
    p = tmp_path / "orizon.yaml"
    p.write_text("install:\n  homee: /x\nconfigure:\n  acess: {mode: direct}\n  access: {domainn: x}\nextra: 1\n")
    cfg = load_config(p)
    assert set(cfg.unknown) == {"extra", "install.homee", "configure.acess", "configure.access.domainn"}


def test_bad_documents(tmp_path):
    p = tmp_path / "orizon.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(p)
    p.write_text("configure: [1, 2]\n")
    with pytest.raises(ConfigError, match="'configure' must be a mapping"):
        load_config(p)
    p.write_text("a: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)
    p = tmp_path / "x.json"
    p.write_text("{nope")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(p)
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert load_config(empty).configure == {} and load_config(empty).install == {}


def test_key_lists_match_the_installer_and_the_wizard():
    """INSTALL_KEYS mirrors install.sh's case statement; CONFIGURE_KEYS mirrors
    every `self.a(...)` lookup in wizard.py. A key added on one side without
    the other is a silent no-op for the operator."""
    sh = INSTALL_SH.read_text()
    for key, var in INSTALL_KEYS.items():
        assert f"{key})" in sh, f"install.sh does not read install.{key}"
        assert var in sh
    import re

    wiz = (HERE / "orizonctl" / "wizard.py").read_text()
    for m in re.finditer(r'self\.a\("([a-z_]+)"(?:, "([a-z_]+)")?', wiz):
        top, sub = m.group(1), m.group(2)
        assert top in CONFIGURE_KEYS, f"wizard reads configure.{top}, unknown to config.py"
        if sub and CONFIGURE_KEYS[top] is not None:
            assert sub in CONFIGURE_KEYS[top], f"wizard reads configure.{top}.{sub}, unknown to config.py"


# ---------------------------------------------------------------- examples


def test_examples_are_bundled():
    assert {"reference", "gpu-letsencrypt", "cpu-external", "custom-cert", "external-proxy"} <= set(example_names())


@pytest.mark.parametrize("name", ["reference", "gpu-letsencrypt", "cpu-external", "custom-cert", "external-proxy"])
def test_example_has_no_unknown_keys(name, tmp_path):
    p = tmp_path / f"{name}.yaml"
    p.write_text(example_text(name))
    cfg = load_config(p)
    assert cfg.unknown == [], cfg.unknown
    assert cfg.configure.get("version"), "every example pins a version"
    for k in cfg.install:
        assert k in INSTALL_KEYS


def _fill(node):
    """Replace placeholders and empty secrets so the wizard can run unattended."""
    for k, v in list(node.items()):
        if isinstance(v, dict):
            _fill(v)
        elif v == "CHANGE-ME" or (v == "" and k in ("token", "cloudflare_token", "api_key", "bot_token", "api_id", "api_hash", "hf_token")):
            node[k] = "filled"


@pytest.mark.parametrize("name", ["reference", "gpu-letsencrypt", "cpu-external", "custom-cert", "external-proxy"])
def test_example_runs_through_the_wizard(name, repo, tmp_path, monkeypatch):
    p = tmp_path / f"{name}.yaml"
    p.write_text(example_text(name))
    cfg = load_config(p)
    answers = cfg.configure
    _fill(answers)
    gpus = [hostinfo.Gpu(i, "NVIDIA H100", 81559, "560.35") for i in range(2)] if answers["models"]["text"] == "local" or answers["models"].get("embedding") == "local" else []
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: gpus)
    values, state = Wizard(repo, interactive=False, answers=answers, previous=State.load(repo)).run()
    assert state.version == answers["version"]
    assert state.access_mode == answers["access"]["mode"]
    assert values["ORIZON_IMAGE_PREFIX"] == answers["registry"]["prefix"]


@pytest.mark.parametrize("name", ["reference", "gpu-letsencrypt", "cpu-external", "custom-cert", "external-proxy"])
def test_install_sh_reads_the_examples_install_section(name, tmp_path):
    p = tmp_path / f"{name}.yaml"
    p.write_text(example_text(name))
    r = subprocess.run(["bash", str(INSTALL_SH), "--config", str(p), "--print-config"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "not an installer key" not in r.stderr
    cfg = load_config(p)
    got = dict(line.split(":", 1) for line in r.stdout.splitlines())
    got = {k.strip(): v.strip() for k, v in got.items()}
    for k, v in cfg.install.items():
        if k == "cli_source" and v == "":
            continue  # empty = the default (the checkout, here)
        want = ("1" if v else "0") if isinstance(v, bool) else str(v)
        assert got[k] == want, (k, got)


def test_install_sh_parser_quotes_comments_and_env_precedence(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text(
        "install:\n"
        "  home: \"/srv/x\"   # trailing comment\n"
        "  version: 'v9.9.9'\n"
        "  cli_source: 'git+https://x/y.git@{version}#subdirectory=orizonctl'\n"
        "  install_only: yes\n"
        "  bogus: 1\n"
        "configure:\n"
        "  version: v1\n"
        "  install_only: true\n"  # not in the install section; must be ignored
    )
    r = subprocess.run(["bash", str(INSTALL_SH), "--config", str(p), "--print-config"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "ORIZON_HOME": "/from/env"})
    assert r.returncode == 0, r.stderr
    out = dict((l.split(":", 1)[0].strip(), l.split(":", 1)[1].strip()) for l in r.stdout.splitlines())
    assert out["home"] == "/from/env"  # the environment wins over the file
    assert out["version"] == "v9.9.9"
    assert out["cli_source"] == "git+https://x/y.git@{version}#subdirectory=orizonctl"
    assert out["install_only"] == "1"
    assert "install.bogus" in r.stderr
    r2 = subprocess.run(["bash", str(INSTALL_SH), "--config", str(tmp_path / "missing.yaml"), "--print-config"], capture_output=True, text=True)
    assert r2.returncode == 2 and "not readable" in r2.stderr
    r3 = subprocess.run(["bash", str(INSTALL_SH), "--frobnicate"], capture_output=True, text=True)
    assert r3.returncode == 2 and "unknown argument" in r3.stderr


# --------------------------------------------------------------------- cli


def test_configure_with_yaml_config(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    p = tmp_path / "orizon.yaml"
    p.write_text(CPU_YAML)
    r = CliRunner().invoke(main, ["--home", str(tmp_path / "home"), "--app-dir", str(repo), "configure", "--non-interactive", "--config", str(p)])
    assert r.exit_code == 0, r.output
    from orizonctl.envfile import load

    env = load(repo / ".env")
    assert env["LLM_API_URL"] == "http://llm.internal:8000" and env["RERANK_MODEL"] == ""
    assert State.load(repo).rerank_mode == "off"


def test_configure_answers_alias_and_unknown_key_warning(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    p = tmp_path / "orizon.yaml"
    p.write_text(CPU_YAML.replace("access:", "acess:"))
    r = CliRunner().invoke(main, ["--home", str(tmp_path / "home"), "--app-dir", str(repo), "configure", "--non-interactive", "--answers", str(p)])
    assert "unknown key 'configure.acess'" in r.output
    assert r.exit_code == 0, r.output  # direct mode has a default public URL, so it still completes


def test_config_example_and_check(tmp_path):
    r = CliRunner().invoke(main, ["config", "example"])
    assert r.exit_code == 0 and "gpu-letsencrypt" in r.output and "reference" in r.output
    r = CliRunner().invoke(main, ["config", "example", "cpu-external"])
    assert r.exit_code == 0 and r.output == example_text("cpu-external")
    r = CliRunner().invoke(main, ["config", "example", "nope"])
    assert r.exit_code != 0 and "no example named" in r.output

    p = tmp_path / "orizon.yaml"
    p.write_text(example_text("gpu-letsencrypt"))
    r = CliRunner().invoke(main, ["config", "check", str(p)])
    assert r.exit_code == 0, r.output
    assert "no unknown keys" in r.output
    assert "CHANGE-ME" not in r.output  # secrets are masked
    assert "-> ORIZON_VERSION" in r.output

    p.write_text("configure:\n  acess: {mode: direct}\n")
    r = CliRunner().invoke(main, ["config", "check", str(p)])
    assert r.exit_code == 1 and "configure.acess" in r.output
