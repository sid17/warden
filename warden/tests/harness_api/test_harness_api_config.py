"""Tests for the Axis-2 ``get_harness_api_config()`` — the account/billing config
that composes the engine config and adds managed keys + spend/pricing.

The autouse hermetic fixture in the repo-root conftest blanks each settings
bundle's env_file AND clears the settings caches before/after every test, so
these env manipulations are order-independent and never see a developer's local
.env.

Importable without the heavy SDK deps (config imports only pydantic-settings +
pydantic).
"""

from warden.harness_api.config import (
    HarnessApiConfig,
    get_harness_api_config,
    get_harness_api_settings,
)


def test_engine_knobs_flow_through_engine_slice(monkeypatch):
    # The knobs RunnerConfig used to duplicate now come from config.engine.
    monkeypatch.setenv("WARDEN_CONCURRENCY", "16")
    monkeypatch.setenv("WARDEN_BASE_DIR", "/data/ws")
    monkeypatch.setenv("WARDEN_STATE_ROOT", "/data/store")
    monkeypatch.setenv("WARDEN_SESSION_DB", "/data/sessions.db")
    monkeypatch.setenv("WARDEN_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("AWS_BUCKET_NAME", "my-bucket")

    cfg = get_harness_api_config()

    assert cfg.engine.concurrency.max_concurrent == 16
    assert cfg.engine.workspace.base_dir == "/data/ws"
    assert cfg.engine.persistence.state_root == "/data/store"
    assert cfg.engine.persistence.session_db_path == "/data/sessions.db"
    assert cfg.engine.persistence.backend == "s3"
    assert cfg.engine.persistence.s3.bucket == "my-bucket"


def test_defaults(monkeypatch):
    for var in (
        "WARDEN_CONCURRENCY",
        "MANAGED_KEYS_JSON",
        "MANAGED_KEYS_FILE",
        "PRICING_JSON",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = get_harness_api_config()

    assert isinstance(cfg, HarnessApiConfig)
    assert cfg.engine.concurrency.max_concurrent == 8
    assert cfg.keys.managed_keys_json is None
    assert cfg.keys.managed_keys_file is None
    assert cfg.spend.pricing_json is None


def test_account_layer_reads_axis2_env(monkeypatch):
    monkeypatch.setenv("MANAGED_KEYS_JSON", '{"keys": {}, "users": {}}')
    monkeypatch.setenv("PRICING_JSON", '{"claude-opus-4-8": [1.0, 2.0]}')

    cfg = get_harness_api_config()

    assert cfg.keys.managed_keys_json == '{"keys": {}, "users": {}}'
    assert cfg.spend.pricing_json == '{"claude-opus-4-8": [1.0, 2.0]}'


def test_managed_keys_are_off_the_engine():
    # The engine settings must NOT carry Axis-2 fields (§8 account-agnostic).
    from warden.config import get_harness_settings

    s = get_harness_settings()
    assert not hasattr(s, "managed_keys_json")
    assert not hasattr(s, "managed_keys_file")
    # ...they live on the Axis-2 settings instead.
    api_s = get_harness_api_settings()
    assert hasattr(api_s, "managed_keys_json")
    assert hasattr(api_s, "pricing_json")
