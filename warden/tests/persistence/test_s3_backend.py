"""Tests for persistence.s3_backend — S3Boto3Backend.

Mirrors test_local_backend.py against S3 using moto (no network, no creds).
Async methods are driven with asyncio.run(...) inside sync tests, matching the
repo's existing convention (no pytest-asyncio configured).
"""

import asyncio
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from warden.persistence.backend import WorkspaceRestoreError
from warden.persistence.factory import get_backend
from warden.persistence.local_backend import LocalFileBackend
from warden.persistence.s3_backend import S3Boto3Backend

KEY = "v1/default/task_1.tar.gz"
BUCKET = "test-bucket"
REGION = "us-east-1"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_workspace(root: Path) -> Path:
    ws = root / "ws"
    _write(ws / "COUNT.txt", "1 2 3")
    _write(ws / ".claude" / "settings.json", "{}")
    _write(ws / "src" / "main.py", "print('hi')")
    return ws


def _create_bucket() -> None:
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)


def _backend(exclude_patterns=()) -> S3Boto3Backend:
    return S3Boto3Backend(
        bucket=BUCKET, region=REGION, exclude_patterns=exclude_patterns
    )


@mock_aws
def test_backup_restore_roundtrip_preserves_contents(tmp_path):
    _create_bucket()
    ws = _make_workspace(tmp_path)
    backend = _backend()

    stats = asyncio.run(backend.backup(ws, KEY))
    assert stats["key"] == KEY
    assert stats["files"] == 3
    assert stats["bytes"] > 0

    out = tmp_path / "restored"
    rstats = asyncio.run(backend.restore(KEY, out))
    assert rstats["files"] == 3

    assert (out / "COUNT.txt").read_text() == "1 2 3"
    assert (out / ".claude" / "settings.json").read_text() == "{}"
    assert (out / "src" / "main.py").read_text() == "print('hi')"


@mock_aws
def test_exists_true_and_false(tmp_path):
    _create_bucket()
    ws = _make_workspace(tmp_path)
    backend = _backend()

    assert asyncio.run(backend.exists(KEY)) is False
    asyncio.run(backend.backup(ws, KEY))
    assert asyncio.run(backend.exists(KEY)) is True


@mock_aws
def test_second_backup_overwrites(tmp_path):
    _create_bucket()
    ws = _make_workspace(tmp_path)
    backend = _backend()

    asyncio.run(backend.backup(ws, KEY))

    # Mutate the workspace, back up again -> object reflects the new content.
    (ws / "COUNT.txt").write_text("10 11 12")
    asyncio.run(backend.backup(ws, KEY))

    out = tmp_path / "restored"
    asyncio.run(backend.restore(KEY, out))
    assert (out / "COUNT.txt").read_text() == "10 11 12"


@mock_aws
def test_restore_missing_key_raises(tmp_path):
    _create_bucket()
    backend = _backend()
    with pytest.raises(WorkspaceRestoreError) as excinfo:
        asyncio.run(backend.restore(KEY, tmp_path / "restored"))
    assert excinfo.value.key == KEY


@mock_aws
def test_excludes_drop_node_modules_and_pycache(tmp_path):
    _create_bucket()
    ws = tmp_path / "ws"
    _write(ws / "keep.py", "ok")
    _write(ws / "node_modules" / "dep" / "index.js", "junk")
    _write(ws / "pkg" / "__pycache__" / "mod.cpython.pyc", "bytecode")
    _write(ws / "stale.pyc", "bytecode")

    backend = _backend(
        exclude_patterns=("node_modules/", "__pycache__/", "*.pyc")
    )
    asyncio.run(backend.backup(ws, KEY))

    out = tmp_path / "restored"
    asyncio.run(backend.restore(KEY, out))

    assert (out / "keep.py").exists()
    assert not (out / "node_modules").exists()
    assert not (out / "pkg" / "__pycache__").exists()
    assert not (out / "stale.pyc").exists()


@mock_aws
def test_git_dir_is_kept_even_with_excludes(tmp_path):
    _create_bucket()
    ws = tmp_path / "ws"
    _write(ws / "file.txt", "data")
    _write(ws / ".git" / "HEAD", "ref: refs/heads/main")
    _write(ws / ".claude-home" / "token.json", "creds")
    _write(ws / ".codex" / "session.json", "sess")
    # Even a broad exclude set must not drop the resume-critical dirs.
    _write(ws / "node_modules" / "x.js", "junk")

    backend = _backend(
        exclude_patterns=("node_modules/", ".venv/", "__pycache__/", "*.pyc")
    )
    asyncio.run(backend.backup(ws, KEY))

    out = tmp_path / "restored"
    asyncio.run(backend.restore(KEY, out))

    assert (out / ".git" / "HEAD").read_text() == "ref: refs/heads/main"
    assert (out / ".claude-home" / "token.json").exists()
    assert (out / ".codex" / "session.json").exists()
    assert not (out / "node_modules").exists()


@mock_aws
def test_prefix_is_applied_to_object_key(tmp_path):
    _create_bucket()
    ws = _make_workspace(tmp_path)
    backend = S3Boto3Backend(bucket=BUCKET, prefix="team-a/", region=REGION)

    asyncio.run(backend.backup(ws, KEY))

    # Object lands under the prefixed key.
    client = boto3.client("s3", region_name=REGION)
    keys = [o["Key"] for o in client.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert keys == [f"team-a/{KEY}"]
    assert asyncio.run(backend.exists(KEY)) is True


def test_get_backend_factory(tmp_path):
    local = get_backend("local", state_root=tmp_path / "store")
    assert isinstance(local, LocalFileBackend)

    s3 = get_backend("s3", bucket=BUCKET, region=REGION)
    assert isinstance(s3, S3Boto3Backend)

    with pytest.raises(ValueError):
        get_backend("nope")


# --- config injection (C7 / M8) -------------------------------------------
# The backend is a config leaf: it takes its S3 knobs as explicit constructor
# args (threaded by config.build.build_persistence from the S3Config slice) and
# never reads get_harness_settings() itself.


class TestConfigInjection:
    def test_endpoint_and_region_from_args(self):
        backend = S3Boto3Backend(
            bucket=BUCKET, endpoint_url="http://minio:9000", region="eu-west-1"
        )
        assert backend._endpoint_url == "http://minio:9000"
        assert backend._region == "eu-west-1"

    def test_no_settings_read_on_construction_or_client(self, monkeypatch):
        """C7: constructing the backend and building its client must NOT reach
        for get_harness_settings() — the leaf reads only its injected args."""
        import warden.config as cfg_pkg

        def _boom():  # pragma: no cover - only fires on a regression
            raise AssertionError("S3 backend must not read get_harness_settings()")

        monkeypatch.setattr(cfg_pkg, "get_harness_settings", _boom)

        captured: dict = {}

        def fake_client(_service, **kwargs):
            captured.update(kwargs)
            return object()

        import boto3 as _boto3

        monkeypatch.setattr(_boto3, "client", fake_client)

        backend = S3Boto3Backend(
            bucket=BUCKET, endpoint_url="http://minio:9000", region="eu-west-1"
        )
        _ = backend._client  # forces the client-build path too
        assert captured["endpoint_url"] == "http://minio:9000"
        assert captured["region_name"] == "eu-west-1"

    def test_nonstandard_keys_passed_when_standard_id_absent(self, monkeypatch):
        # The non-standard access-key pair is passed explicitly to boto3 only
        # when the standard id is absent — now sourced from injected args.
        captured: dict = {}

        def fake_client(_service, **kwargs):
            captured.update(kwargs)
            return object()

        import boto3 as _boto3

        monkeypatch.setattr(_boto3, "client", fake_client)

        backend = S3Boto3Backend(
            bucket=BUCKET,
            access_key="nonstd-id",
            secret_access_key="nonstd-secret",
            access_key_id=None,
        )
        _ = backend._client

        assert captured["aws_access_key_id"] == "nonstd-id"
        assert captured["aws_secret_access_key"] == "nonstd-secret"

    def test_nonstandard_keys_skipped_when_standard_id_present(self, monkeypatch):
        captured: dict = {}

        def fake_client(_service, **kwargs):
            captured.update(kwargs)
            return object()

        import boto3 as _boto3

        monkeypatch.setattr(_boto3, "client", fake_client)

        backend = S3Boto3Backend(
            bucket=BUCKET,
            access_key="nonstd-id",
            secret_access_key="nonstd-secret",
            access_key_id="std-id",
        )
        _ = backend._client

        # boto3's standard chain handles it — no explicit creds injected.
        assert "aws_access_key_id" not in captured
        assert "aws_secret_access_key" not in captured
