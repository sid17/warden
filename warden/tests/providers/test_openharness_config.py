"""Tests for OpenHarnessSession config resolution (env overrides).

These construct the session WITHOUT calling start() so no Ollama health
check / network access occurs. All env manipulation goes through
monkeypatch so os.environ is never mutated across tests.
"""

from pathlib import Path

from warden.providers.openharness.session import OpenHarnessSession

REPO = Path("/tmp")


class TestDefaults:
    def test_base_url_and_model_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENHARNESS_BASE_URL", raising=False)
        monkeypatch.delenv("OPENHARNESS_MODEL", raising=False)
        monkeypatch.delenv("OPENHARNESS_API_KEY", raising=False)

        session = OpenHarnessSession(repo_path=REPO)

        assert session._base_url == "http://localhost:11434"
        assert session._model == "qwen3:1.7b"
        assert session._api_key == "ollama"


class TestEnvOverride:
    def test_base_url_env_override(self, monkeypatch):
        monkeypatch.setenv("OPENHARNESS_BASE_URL", "http://host.docker.internal:11434")
        monkeypatch.delenv("OPENHARNESS_MODEL", raising=False)

        session = OpenHarnessSession(repo_path=REPO)

        assert session._base_url == "http://host.docker.internal:11434"
        # Model still falls back to default when its env var is unset.
        assert session._model == "qwen3:1.7b"

    def test_model_env_override(self, monkeypatch):
        monkeypatch.delenv("OPENHARNESS_BASE_URL", raising=False)
        monkeypatch.setenv("OPENHARNESS_MODEL", "llama3:8b")

        session = OpenHarnessSession(repo_path=REPO)

        assert session._model == "llama3:8b"
        # Base URL still falls back to default when its env var is unset.
        assert session._base_url == "http://localhost:11434"

    def test_api_key_env_override(self, monkeypatch):
        monkeypatch.setenv("OPENHARNESS_API_KEY", "sk-real-key")

        session = OpenHarnessSession(repo_path=REPO)

        assert session._api_key == "sk-real-key"


class TestExplicitParamPrecedence:
    def test_explicit_base_url_beats_env(self, monkeypatch):
        monkeypatch.setenv("OPENHARNESS_BASE_URL", "http://from-env:11434")

        session = OpenHarnessSession(
            repo_path=REPO, base_url="http://explicit:11434"
        )

        assert session._base_url == "http://explicit:11434"

    def test_explicit_model_beats_env(self, monkeypatch):
        monkeypatch.setenv("OPENHARNESS_MODEL", "from-env-model")

        session = OpenHarnessSession(repo_path=REPO, model="explicit-model")

        assert session._model == "explicit-model"

    def test_explicit_api_key_beats_env(self, monkeypatch):
        monkeypatch.setenv("OPENHARNESS_API_KEY", "from-env-key")

        session = OpenHarnessSession(repo_path=REPO, api_key="explicit-key")

        assert session._api_key == "explicit-key"


class TestInjectedProviderConfig:
    """C7 (M8): the session receives a ProviderConfig slice through the
    provider-factory channel and resolves its knobs from it — never reaching
    for get_harness_settings() itself."""

    def test_injected_provider_config_used(self, monkeypatch):
        from warden.config.models import ProviderConfig

        # Env says one thing; the injected slice must win over the env fallback.
        monkeypatch.setenv("OPENHARNESS_BASE_URL", "http://from-env:11434")
        monkeypatch.setenv("OPENHARNESS_MODEL", "from-env-model")

        session = OpenHarnessSession(
            repo_path=REPO,
            provider_config=ProviderConfig(
                openharness_base_url="http://injected:1234",
                openharness_model="injected-model",
                openharness_api_key="injected-key",
            ),
        )

        assert session._base_url == "http://injected:1234"
        assert session._model == "injected-model"
        assert session._api_key == "injected-key"

    def test_injected_slice_skips_config_surface(self, monkeypatch):
        # With an injected slice, the session must NOT read the global config
        # surface at all (and thus never get_harness_settings()).
        import warden.config as cfg_pkg
        from warden.config.models import ProviderConfig

        def _boom():  # pragma: no cover - only fires on a regression
            raise AssertionError("must not read the global config when injected")

        monkeypatch.setattr(cfg_pkg, "get_harness_config", _boom)

        session = OpenHarnessSession(
            repo_path=REPO,
            provider_config=ProviderConfig(openharness_base_url="http://x:1"),
        )
        assert session._base_url == "http://x:1"
