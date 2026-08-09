"""Hermetic proof that a REMOTE (S3) backup carries no credential (A3 + A2).

The credential-exclusion lives in the shared ``persistence.archive`` layer, so it
applies to the S3 backend identically to the local one — but "works locally" does
not imply "works on S3", so we prove it against a mocked S3 object: back up a
workspace holding ``.codex/auth.json``, download the produced object, and inspect
the tarball. No credential, no raw token bytes; the transcript survives. Then a
full S3 round-trip + out-of-band re-injection reconstructs a usable home.

Uses moto (no network, no creds) — the CI-safe half of A3; the live MinIO/S3 run
is a documented runbook (docs/08 Part F / ADR credential-backup-separation).
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from warden.persistence.config import DEFAULT_EXCLUDE_PATTERNS
from warden.persistence.s3_backend import S3Boto3Backend
from warden.workspace.credentials import reinject_credentials

BUCKET = "crash-bucket"
REGION = "us-east-1"
KEY = "v1/default/crash_task.tar.gz"
TOKEN = "sk-S3-SECRET-TOKEN"


def _make_task(root: Path) -> Path:
    """A task workspace shaped like a persisted codex home."""
    td = root / "task"
    codex = td / ".codex" / "sessions"
    codex.mkdir(parents=True)
    (td / ".codex" / "auth.json").write_text(f'{{"OPENAI_API_KEY":"{TOKEN}"}}')
    (codex / "rollout-2026-xyz.jsonl").write_text('{"turn": 1, "text": "heliotrope"}\n')
    return td


def _object_bytes(client) -> bytes:
    return client.get_object(Bucket=BUCKET, Key=KEY)["Body"].read()


@mock_aws
def test_s3_object_carries_no_credential(tmp_path: Path) -> None:
    """The uploaded S3 tarball must contain no auth.json and no raw token."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET)

    td = _make_task(tmp_path)
    # Even with EMPTY exclude patterns the credential is dropped — the exclusion
    # is unconditional in the archive layer, not a configurable pattern.
    backend = S3Boto3Backend(bucket=BUCKET, region=REGION, exclude_patterns=())
    asyncio.run(backend.backup(td, KEY))

    blob = _object_bytes(client)
    assert TOKEN.encode() not in blob, "raw OAuth token uploaded to S3 in the clear"

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = [m.name for m in tar.getmembers()]
    assert not any(Path(n).name == "auth.json" for n in names), (
        f"auth.json uploaded to S3: {names}"
    )
    # The transcript (memory) must be in the remote object — that's the point.
    assert any(n.endswith("rollout-2026-xyz.jsonl") for n in names), (
        f"transcript missing from S3 object: {names}"
    )


@mock_aws
def test_s3_roundtrip_then_reinject_reconstructs_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full remote crash path: backup → wipe → S3 restore → out-of-band reinject.

    After an S3 restore the pinned home has the transcript but NO credential
    (it was never uploaded). ``reinject_credentials`` re-hydrates it from the
    out-of-band source — the same production path a stateless worker takes.
    """
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET)

    td = _make_task(tmp_path)
    backend = S3Boto3Backend(
        bucket=BUCKET, region=REGION, exclude_patterns=DEFAULT_EXCLUDE_PATTERNS
    )
    asyncio.run(backend.backup(td, KEY))

    # Wipe (fresh container by construction) and restore from S3.
    import shutil

    shutil.rmtree(td)
    restored = tmp_path / "restored_task"
    asyncio.run(backend.restore(KEY, restored))

    # Transcript is back; credential is NOT (never uploaded).
    assert (restored / ".codex" / "sessions" / "rollout-2026-xyz.jsonl").is_file()
    assert not (restored / ".codex" / "auth.json").exists()

    # Out-of-band re-injection from a mounted source (not from the S3 object).
    src = tmp_path / "mounted"
    src.mkdir()
    (src / "auth.json").write_text(f'{{"OPENAI_API_KEY":"{TOKEN}"}}')
    monkeypatch.setenv("WARDEN_CODEX_AUTH_SOURCE", str(src))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    copied = reinject_credentials("codex", restored)

    assert copied == ["auth.json"]
    assert (restored / ".codex" / "auth.json").is_file()
    assert TOKEN in (restored / ".codex" / "auth.json").read_text()
