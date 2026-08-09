"""EXT-W1 (E3) — the seed store: a reusable, versioned, immutable seed artifact.

A *seed* is a product-authored bundle (a ``.tar.gz`` of ``skills/``, ``agents/``,
``workflows/``) uploaded once (``POST /seeds``) and referenced many times
(``POST /provision``). Storage is the harness's job; the content is the product's.

**Reuses the existing persistence ``StorageBackend``** (local or S3, per config) — no
new storage infra (E3 decision 1 / gotcha #1). A seed is stored as a one-file archive
(``bundle.tar.gz`` inside the backend's tarball) so the same backend + the A1
``read_file`` serve it back. The ``seed_ref`` is **content-addressed** (``seed_`` +
sha256 of the bundle), which makes it **immutable** (a ref always resolves to the same
bytes) and **deduplicated** (uploading identical bytes returns the same ref) — exactly
the versioning + permission-surface-stability property W1 needs.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from warden.persistence.backend import StorageBackend

_INNER_MEMBER = "bundle.tar.gz"


class SeedStore:
    """Store/fetch opaque seed bundles over a persistence ``StorageBackend``."""

    def __init__(self, backend: StorageBackend, *, prefix: str = "seeds") -> None:
        self._backend = backend
        self._prefix = prefix

    def _key(self, ref: str) -> str:
        return f"{self._prefix}/{ref}.tar.gz"

    @staticmethod
    def _ref_for(bundle: bytes) -> str:
        return "seed_" + hashlib.sha256(bundle).hexdigest()[:32]

    async def put(self, bundle: bytes) -> str:
        """Store a bundle and return its opaque, immutable ``seed_ref``.

        Content-addressed: the ref is derived from the bytes, so re-uploading the same
        bundle is a no-op that returns the same ref (immutable + dedupe)."""
        ref = self._ref_for(bundle)
        key = self._key(ref)
        if await self._backend.exists(key):
            return ref  # already stored — immutable, so nothing to rewrite
        with tempfile.TemporaryDirectory() as d:
            inner = Path(d) / "seed"
            inner.mkdir()
            (inner / _INNER_MEMBER).write_bytes(bundle)
            await self._backend.backup(inner, key)
        return ref

    async def get(self, ref: str) -> bytes | None:
        """Fetch a bundle by ref, or ``None`` if the ref is unknown."""
        key = self._key(ref)
        if not await self._backend.exists(key):
            return None
        return await self._backend.read_file(key, _INNER_MEMBER)


__all__ = ["SeedStore"]
