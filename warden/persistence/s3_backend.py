"""S3 (boto3) storage backend: tar.gz a task folder, streamed via disk tempfiles.

Satisfies the same ``StorageBackend`` Protocol as ``LocalFileBackend``, sharing
the archive build/extract logic in ``persistence.archive``. Works
against real AWS S3 or an S3-compatible endpoint (MinIO) via ``endpoint_url``.

The boto3 client is created lazily on first use and cached, so importing this
module (or the ``persistence`` package) never requires boto3 to be installed.
The archive is built into / downloaded to a tempfile **on disk** — never
buffered in RAM — matching the local backend's streaming contract.

C7 (M8): this backend is a config leaf — it takes its S3 knobs (endpoint,
region, the access-key pair) as explicit constructor args and NEVER reads
``get_harness_settings()`` itself. The config-layer builder
(:func:`config.build.build_persistence`) resolves the ``S3Config`` slice and
threads the values in. Standard credentials (``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY``) still come from boto3's own env chain; the
non-standard ``AWS_ACCESS_KEY`` pair is passed explicitly only when the
standard id is absent.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from warden.persistence.archive import (
    extract_tar_gz,
    read_member_from_tar,
    write_tar_gz,
)
from warden.persistence.backend import WorkspaceRestoreError


class S3Boto3Backend:
    """A `StorageBackend` storing gzip tarballs as S3 objects."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        access_key: str | None = None,
        secret_access_key: str | None = None,
        exclude_patterns: tuple[str, ...] = (),
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._exclude_patterns = tuple(exclude_patterns)
        # Injected by the builder from the S3Config slice (C7) — no settings
        # read here. MinIO uses an explicit endpoint; the caller has already
        # applied the bucket-location-wins-over-region precedence.
        self._endpoint_url = endpoint_url
        self._region = region
        self._access_key_id = access_key_id
        self._access_key = access_key
        self._secret_access_key = secret_access_key
        self._cached_client = None

    # --- lazy boto3 client ----------------------------------------------------

    @property
    def _client(self):
        """boto3 S3 client, created on first use and cached.

        boto3 is imported lazily here so that importing this module never
        requires the dependency. Credentials come from boto3's standard env
        chain; if the non-standard ``AWS_ACCESS_KEY``/``AWS_SECRET_ACCESS_KEY``
        pair is set (and the standard one is not), pass them explicitly.
        """
        if self._cached_client is not None:
            return self._cached_client

        import boto3  # lazy: keeps `import persistence` boto3-free

        kwargs: dict = {}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        if self._region:
            kwargs["region_name"] = self._region

        # Standard chain uses AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (boto3
        # reads its own env). Also honor the non-standard AWS_ACCESS_KEY pair
        # (threaded in as constructor args, C7) by passing it explicitly when
        # the standard id is absent.
        if self._access_key and self._secret_access_key and not self._access_key_id:
            kwargs["aws_access_key_id"] = self._access_key
            kwargs["aws_secret_access_key"] = self._secret_access_key

        self._cached_client = boto3.client("s3", **kwargs)
        return self._cached_client

    # --- key → object key -----------------------------------------------------

    def _object_key(self, key: str) -> str:
        if self._prefix:
            return f"{self._prefix.rstrip('/')}/{key}"
        return key

    # --- blocking implementations (run in a worker thread) --------------------

    def _backup_sync(self, local_dir: Path, key: str) -> dict:
        local_dir = Path(local_dir)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".tar.gz")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            stats = write_tar_gz(local_dir, tmp_path, self._exclude_patterns)
            self._client.upload_file(
                str(tmp_path), self._bucket, self._object_key(key)
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        return {"key": key, "files": stats["files"], "bytes": stats["bytes"]}

    def _restore_sync(self, key: str, local_dir: Path) -> dict:
        from botocore.exceptions import ClientError

        obj_key = self._object_key(key)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".tar.gz")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            try:
                self._client.download_file(self._bucket, obj_key, str(tmp_path))
            except ClientError as exc:
                if _is_not_found(exc):
                    raise WorkspaceRestoreError(
                        f"archive key not found in store: {key}",
                        key=key,
                        retries_attempted=0,
                        last_error=exc,
                    ) from exc
                raise
            stats = extract_tar_gz(tmp_path, Path(local_dir), key)
        finally:
            tmp_path.unlink(missing_ok=True)

        return {"key": key, "files": stats["files"], "bytes": stats["bytes"]}

    def _read_file_sync(self, key: str, member_path: str) -> bytes:
        from botocore.exceptions import ClientError

        # No range-read of a member inside a gzip tarball — pull the whole object to
        # a tempfile (the _restore_sync idiom), then read the one member. Fine for
        # small course artifacts; note it for large files (E1 §6 gotcha).
        obj_key = self._object_key(key)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".tar.gz")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            try:
                self._client.download_file(self._bucket, obj_key, str(tmp_path))
            except ClientError as exc:
                if _is_not_found(exc):
                    raise FileNotFoundError(
                        f"archive key not found in store: {key}"
                    ) from exc
                raise
            return read_member_from_tar(tmp_path, member_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _exists_sync(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(
                Bucket=self._bucket, Key=self._object_key(key)
            )
            return True
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise  # surface transport/permission errors, don't swallow

    # --- async surface --------------------------------------------------------

    async def backup(self, local_dir: Path, key: str) -> dict:
        return await asyncio.to_thread(self._backup_sync, local_dir, key)

    async def restore(self, key: str, local_dir: Path) -> dict:
        return await asyncio.to_thread(self._restore_sync, key, local_dir)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._exists_sync, key)

    async def read_file(self, key: str, member_path: str) -> bytes:
        return await asyncio.to_thread(self._read_file_sync, key, member_path)


def _is_not_found(exc) -> bool:
    """True if a botocore ClientError is a 404 / NoSuchKey miss."""
    err = getattr(exc, "response", {}).get("Error", {})
    code = err.get("Code")
    status = (
        getattr(exc, "response", {})
        .get("ResponseMetadata", {})
        .get("HTTPStatusCode")
    )
    return code in ("404", "NoSuchKey", "NoSuchBucket") or status == 404
