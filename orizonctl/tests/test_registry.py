from click.testing import CliRunner

from orizonctl import compose, hostinfo
from orizonctl.cli import main, resolve_cli_spec
from orizonctl.schema import registry_host
from orizonctl.state import State
from orizonctl.wizard import Wizard, WizardError


def test_registry_host_detection():
    assert registry_host("sadramoghadam") == "docker.io"
    assert registry_host("registry.gitlab.syneto.eu/orizon-ai") == "registry.gitlab.syneto.eu"
    assert registry_host("localhost:5000/orizon") == "localhost:5000"
    assert registry_host("myregistry:443/ns/sub") == "myregistry:443"


def test_cli_spec_template():
    t = "git+https://gitlab.syneto.eu/orizon-ai/orizon-ai-deploy.git@{version}#subdirectory=orizonctl"
    assert resolve_cli_spec(t, "v1.3.0") == "git+https://gitlab.syneto.eu/orizon-ai/orizon-ai-deploy.git@v1.3.0#subdirectory=orizonctl"
    assert resolve_cli_spec("orizonctl=={pyversion}", "v1.3.0") == "orizonctl==1.3.0"


BASE = {
    "version": "v1.2.0",
    "models": {"text": "external", "embedding": "external", "rerank": "off"},
    "external": {"text": {"url": "http://llm:8000", "model": "m"}, "embedding": {"url": "http://emb:8000", "model": "e"}},
    "access": {"mode": "direct", "public_url": "http://10.0.0.1:3000"},
}


def test_wizard_registry_login_and_prefix(repo, monkeypatch):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    calls = {}
    monkeypatch.setattr(compose, "registry_login", lambda host, u, t, runtime="docker": (calls.setdefault("login", (host, u, t)), (True, "ok"))[1])
    monkeypatch.setattr(compose, "image_accessible", lambda ref, runtime="docker": (calls.setdefault("probe", ref), (True, "ok"))[1])
    answers = {**BASE, "registry": {"prefix": "registry.gitlab.syneto.eu/orizon-ai/", "username": "deploy-token", "token": "glpat-xyz"}}
    values, state = Wizard(repo, interactive=False, answers=answers).run()
    assert values["ORIZON_IMAGE_PREFIX"] == "registry.gitlab.syneto.eu/orizon-ai"  # trailing slash stripped
    assert state.registry_host == "registry.gitlab.syneto.eu" and state.registry_login
    assert calls["login"] == ("registry.gitlab.syneto.eu", "deploy-token", "glpat-xyz")
    assert calls["probe"] == "registry.gitlab.syneto.eu/orizon-ai/orizon-ai-backend:v1.2.0"
    assert "glpat-xyz" not in "".join(values.values())  # never written to an env file


def test_wizard_registry_defaults_to_syneto_harbor(repo, monkeypatch):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    values, state = Wizard(repo, interactive=False, answers=BASE).run()
    assert values["ORIZON_IMAGE_PREFIX"] == "scr.syneto.eu/orizon-ai/orizon-ai"
    assert state.registry_host == "scr.syneto.eu"
    assert not state.registry_login  # unattended and no credentials given: assume an existing login or a public project


def test_docker_hub_namespace_still_works(repo, monkeypatch):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    values, state = Wizard(repo, interactive=False, answers={**BASE, "registry": {"prefix": "sadramoghadam"}}).run()
    assert values["ORIZON_IMAGE_PREFIX"] == "sadramoghadam" and state.registry_host == "docker.io"


def test_non_interactive_login_failure_is_an_error(repo, monkeypatch):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    monkeypatch.setattr(compose, "registry_login", lambda *a, **k: (False, "unauthorized"))
    import pytest

    with pytest.raises(WizardError):
        Wizard(repo, interactive=False, answers={**BASE, "registry": {"prefix": "registry.example.com/x", "username": "u", "token": "t"}}).run()


def test_init_then_configure_from_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    home = tmp_path / "home"
    r = CliRunner().invoke(main, ["--home", str(home), "init"])
    assert r.exit_code == 0, r.output
    assert (home / "app" / "docker-compose.yml").exists() and (home / "app" / "nginx" / "orizonai.conf.template").exists()
    import json

    a = tmp_path / "a.json"
    a.write_text(json.dumps(BASE))
    r = CliRunner().invoke(main, ["--home", str(home), "configure", "--non-interactive", "--answers", str(a)])
    assert r.exit_code == 0, r.output
    assert (home / "app" / ".env").exists()
    st = State.load(home / "app")
    assert st and st.image_prefix == "scr.syneto.eu/orizon-ai/orizon-ai"


def test_commands_need_init_first(tmp_path):
    r = CliRunner().invoke(main, ["--home", str(tmp_path), "status"])
    assert r.exit_code != 0 and "orizonctl init" in r.output


def test_bare_registry_host_is_rejected(repo, monkeypatch):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    import pytest

    with pytest.raises(WizardError, match="add the project"):
        Wizard(repo, interactive=False, answers={**BASE, "registry": {"prefix": "harbor.syneto.eu", "login": False}}).run()
    values, state = Wizard(repo, interactive=False, answers={**BASE, "registry": {"prefix": "harbor.syneto.eu/orizon-ai", "login": False}}).run()
    assert values["ORIZON_IMAGE_PREFIX"] == "harbor.syneto.eu/orizon-ai" and state.registry_host == "harbor.syneto.eu"


def test_tag_ordering_prefers_release_over_dev_suffix():
    from orizonctl.registry import newest, version_key

    tags = ["v1.2.0_dev", "v1.10.0", "v1.2.0", "latest", "v1.9.3"]
    assert sorted(tags, key=version_key) == ["latest", "v1.2.0_dev", "v1.2.0", "v1.9.3", "v1.10.0"]
    assert newest(tags) == "v1.10.0"
    assert newest(["v1.2.0_dev", "v1.2.0"]) == "v1.2.0"


def test_non_interactive_keeps_requested_tag_but_warns_when_absent(repo, monkeypatch, capsys):
    from orizonctl import registry

    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    monkeypatch.setattr(registry, "list_tags", lambda prefix, image, creds=None, timeout=20: ["v1.2.0_dev", "v1.1.9"])
    values, state = Wizard(repo, interactive=False, answers={**BASE, "version": "v1.2.0"}).run()
    assert values["ORIZON_VERSION"] == "v1.2.0"
    assert "not in the registry" in capsys.readouterr().err


def test_dev_tag_is_accepted(repo, monkeypatch):
    from orizonctl import registry

    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    monkeypatch.setattr(registry, "list_tags", lambda prefix, image, creds=None, timeout=20: ["v1.2.0_dev"])
    values, state = Wizard(repo, interactive=False, answers={**BASE, "version": "v1.2.0_dev", "registry": {"prefix": "scr.syneto.eu/orizon-ai", "login": False}}).run()
    assert values["ORIZON_VERSION"] == "v1.2.0_dev" and state.version == "v1.2.0_dev"


def test_docker_credentials_from_config(tmp_path):
    import base64, json

    from orizonctl.registry import docker_credentials

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"auths": {"scr.syneto.eu": {"auth": base64.b64encode(b"robot$x:secret").decode()}}}))
    assert docker_credentials("scr.syneto.eu", cfg) == ("robot$x", "secret")
    assert docker_credentials("other.example", cfg) is None


def test_registry_client_splits_proxy_cache_path():
    from orizonctl.registry import _split

    assert _split("scr.syneto.eu/orizon-ai/orizon-ai", "orizon-ai-backend") == ("scr.syneto.eu", "orizon-ai/orizon-ai/orizon-ai-backend")
    assert _split("harbor.syneto.eu/orizon-ai", "orizon-ai-backend") == ("harbor.syneto.eu", "orizon-ai/orizon-ai-backend")
    assert _split("sadramoghadam", "orizon-ai-backend") == ("registry-1.docker.io", "sadramoghadam/orizon-ai-backend")


def test_proxy_cache_prefix_is_suggested_when_one_segment_short(repo, monkeypatch, capsys):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    good = "scr.syneto.eu/orizon-ai/orizon-ai/orizon-ai-backend:v1.2.0_dev"
    monkeypatch.setattr(compose, "image_accessible", lambda ref, runtime="docker": (True, "manifest found") if ref == good else (False, "unknown: repository orizon-ai/orizon-ai-backend not found"))
    values, state = Wizard(repo, interactive=False, answers={**BASE, "version": "v1.2.0_dev", "registry": {"prefix": "scr.syneto.eu/orizon-ai", "login": False}}).run()
    assert values["ORIZON_IMAGE_PREFIX"] == "scr.syneto.eu/orizon-ai"  # unattended: never silently rewritten
    assert "set the prefix to scr.syneto.eu/orizon-ai/orizon-ai" in capsys.readouterr().err


def test_prefix_pasted_as_url_is_normalized(repo, monkeypatch):
    from orizonctl.wizard import _validate_prefix

    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    assert _validate_prefix("https://scr.syneto.eu/orizon-ai/orizon-ai/") is None
    assert "no spaces" in _validate_prefix("scr.syneto.eu/orizon ai")
    assert "no image or tag" in _validate_prefix("scr.syneto.eu/orizon-ai/orizon-ai-backend:v1.2.0")
    assert _validate_prefix("localhost:5000/orizon") is None  # a port is not a tag
    values, state = Wizard(repo, interactive=False, answers={**BASE, "registry": {"prefix": "https://scr.syneto.eu/orizon-ai/orizon-ai/", "login": False}}).run()
    assert values["ORIZON_IMAGE_PREFIX"] == "scr.syneto.eu/orizon-ai/orizon-ai" and state.registry_host == "scr.syneto.eu"


def test_proxy_cache_prefix_is_suggested_from_tag_listing_despite_wrong_tag(repo, monkeypatch, capsys):
    from orizonctl import registry

    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    listed = []
    monkeypatch.setattr(registry, "list_tags", lambda prefix, image, creds=None, timeout=20: (listed.append(prefix), ["v1.2.0_dev"] if prefix == "scr.syneto.eu/orizon-ai/orizon-ai" else [])[1])
    monkeypatch.setattr(compose, "image_accessible", lambda ref, runtime="docker": (False, "unknown: repository not found"))
    values, _ = Wizard(repo, interactive=False, answers={**BASE, "version": "v.1.2.0_dev", "registry": {"prefix": "scr.syneto.eu/orizon-ai", "login": False}}).run()
    assert listed == ["scr.syneto.eu/orizon-ai", "scr.syneto.eu/orizon-ai/orizon-ai"]
    assert values["ORIZON_IMAGE_PREFIX"] == "scr.syneto.eu/orizon-ai"  # unattended: hint only
    err = capsys.readouterr().err
    assert "scr.syneto.eu/orizon-ai/orizon-ai/orizon-ai-backend has v1.2.0_dev" in err
    assert "set the prefix to scr.syneto.eu/orizon-ai/orizon-ai" in err


def test_stored_docker_login_is_kept_instead_of_re_asked(repo, monkeypatch):
    import click

    from orizonctl import registry

    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    monkeypatch.setattr(registry, "docker_credentials", lambda host, config=None: ("robot$orizon-ai-scr", "secret") if host == "scr.syneto.eu" else None)
    asked = []
    monkeypatch.setattr(click, "prompt", lambda text, default=None, **kw: default)
    monkeypatch.setattr(click, "confirm", lambda text, default=False, **kw: (asked.append((text, default)), default)[1])
    logins = []
    monkeypatch.setattr(compose, "registry_login", lambda host, u, t, runtime="docker": (logins.append(u), (True, "ok"))[1])
    wiz = Wizard(repo, interactive=True, answers=None)
    wiz.state.version = "v1.2.0"
    wiz.section_registry()
    assert asked == [("Enter a different login for scr.syneto.eu?", False)]
    assert logins == [] and wiz.state.registry_login  # nothing re-entered; the deployment still counts as logged in
