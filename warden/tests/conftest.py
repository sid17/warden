"""Hermetic settings fixture for the runtime's test tree.

``HarnessSettings`` / ``HarnessApiSettings`` / ``MockConfig`` all inherit
:class:`HarnessBaseSettings` (the self-contained settings base). Without this
fixture those bundles would load the developer's ``.env`` (langfuse keys, S3
creds, …) during unit tests and stop being hermetic.

This fixture blanks ``env_file`` across the ``HarnessBaseSettings`` subtree and
clears the cached accessors, so tests see only ``os.environ`` + field defaults —
hermetic in every checkout. It is intentionally self-contained: it imports only
this package, so it travels with the runtime.
"""

import pytest

from warden.config import get_harness_settings
from warden.config.base_settings import HarnessBaseSettings
from warden.harness_api.config import get_harness_api_settings


def _iter_settings_subclasses(cls):
    """Yield ``cls`` and every descendant subclass (recursively, deduped)."""
    seen = set()

    def _walk(c):
        for sub in c.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                yield sub
                yield from _walk(sub)

    yield cls
    yield from _walk(cls)


@pytest.fixture(autouse=True)
def _hermetic_harness_settings():
    """Isolate every harness settings bundle for every test: hermetic + uncached.

    Each pydantic v2 subclass owns its own merged ``model_config``, so ``env_file``
    must be blanked on each class in the subtree (not just the base). The cached
    accessors are ``@lru_cache``'d, so they are cleared around the blanking so a
    fresh construct sees the blanked config rather than a stale cached one.
    """
    originals: list[tuple[type, object]] = []
    for cls in _iter_settings_subclasses(HarnessBaseSettings):
        originals.append((cls, cls.model_config.get("env_file")))
        cls.model_config["env_file"] = None

    get_harness_settings.cache_clear()
    get_harness_api_settings.cache_clear()
    yield
    for cls, original_env_file in originals:
        cls.model_config["env_file"] = original_env_file
    get_harness_settings.cache_clear()
    get_harness_api_settings.cache_clear()
