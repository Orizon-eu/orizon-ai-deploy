from orizonctl.schema import EnvFile, available_files, is_secret_name, split_by_file


def test_available_files_full_layout(repo):
    assert available_files(repo) == set(EnvFile)


def test_available_files_old_layout(old_layout_repo):
    assert available_files(old_layout_repo) == {EnvFile.ENV}


def test_split_routes_and_unprefixes_aliases(repo):
    out = split_by_file(
        {"DEFAULT_MODEL": "m", "VLLM_DEFAULT_MODEL": "m", "NGINX_SERVER_PORT": "8005", "CLOUDFLARE_API_TOKEN": "t", "ORIZON_INTERNAL_API_KEY": "k"},
        available_files(repo),
    )
    assert out[EnvFile.ENV] == {"DEFAULT_MODEL": "m"}
    assert out[EnvFile.VLLM] == {"DEFAULT_MODEL": "m"}
    assert out[EnvFile.NGINX] == {"SERVER_PORT": "8005", "CLOUDFLARE_API_TOKEN": "t"}
    assert out[EnvFile.SECRETS] == {"ORIZON_INTERNAL_API_KEY": "k"}


def test_split_old_layout_collapses_into_env(old_layout_repo):
    out = split_by_file(
        {"DEFAULT_MODEL": "m", "VLLM_DEFAULT_MODEL": "m", "NGINX_DOMAIN": "x.example.com", "ORIZON_INTERNAL_API_KEY": "k", "GPU_MEMORY_UTILIZATION": "0.5"},
        available_files(old_layout_repo),
    )
    assert set(out) == {EnvFile.ENV}
    env = out[EnvFile.ENV]
    assert env["DEFAULT_MODEL"] == "m"
    assert "VLLM_DEFAULT_MODEL" not in env  # alias collapsed
    assert env["NGINX_DOMAIN"] == "x.example.com"
    assert env["ORIZON_INTERNAL_API_KEY"] == "k"
    assert env["GPU_MEMORY_UTILIZATION"] == "0.5"


def test_secret_name_rule_is_whole_word():
    assert is_secret_name("ORIZON_MCP_ENCRYPTION_KEY")
    assert is_secret_name("POSTGRES_PASSWORD")
    assert is_secret_name("CLOUDFLARE_API_TOKEN")
    assert not is_secret_name("ORIZON_MAILCAL_AI_MAX_TOKENS")
    assert not is_secret_name("MODELS_PATH")
