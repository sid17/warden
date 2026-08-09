"""Smoke test for HarnessSettings — the additive config seam for the harness engine.

Importable/runnable WITHOUT the heavy SDK deps: config.py imports only the local
HarnessBaseSettings + pydantic, so it won't pull claude_agent_sdk.

Purity guard: HarnessSettings is built on HarnessBaseSettings (NOT InfraSettings),
so it must NOT carry the DB/mongo/redis surface — the harness uses SQLite + local FS
+ optional S3.
"""

from warden.config import HarnessSettings


def test_defaults():
    s = HarnessSettings(_env_file=None)
    assert s.harness_concurrency == 8
    assert s.harness_storage_backend == "local"
    assert s.openharness_model == "qwen3:1.7b"
    assert s.openharness_base_url == "http://localhost:11434"
    assert s.audit_enabled is False


def test_harness_settings_base_is_clean():
    # database_dsn is a computed field on InfraSettings — must be absent here.
    assert "database_dsn" not in HarnessSettings.model_fields
    assert "mongo_uri" not in HarnessSettings.model_fields
    assert "redis_url" not in HarnessSettings.model_fields


def test_inherits_environment_from_base_settings():
    assert "environment" in HarnessSettings.model_fields
