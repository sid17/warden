"""The example ``PROFILE`` — the mock harness's product-agnostic default.

Binds the reference ``scripts`` + a ``fixtures`` dir + a ``build_invoker`` into one
``Profile`` the product-agnostic runner consumes. The example has no product writeback,
so ``build_invoker`` returns the engine's canned :class:`NoopToolInvoker`. This is what
``MockConfig.profile`` defaults to, so the OSS engine boots a working profile with no
product present.
"""

from __future__ import annotations

from pathlib import Path

from warden.harness_api_mock.profile_loader import Profile
from warden.harness_api_mock.profiles.example.scripts import SCRIPTS
from warden.harness_api_mock.tool_seam import NoopToolInvoker

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _build_invoker(config, job_id_for) -> NoopToolInvoker:
    """The example profile has no product DB writeback — use the canned invoker."""
    return NoopToolInvoker()


PROFILE = Profile(
    name="example",
    scripts=SCRIPTS,
    fixture_dir=_FIXTURE_DIR,
    build_invoker=_build_invoker,
)


__all__ = ["PROFILE"]
