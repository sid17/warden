"""Harness config package — one typed surface for driving the engine.

Three tiers (see ``docs/config-plan.md``):
- ``settings.py`` — the flat env layer (``HarnessSettings``; existing env names + ``.env`` overlay).
- ``models.py``  — the nested declarative ``HarnessConfig`` a consumer reads/sets.
- ``build.py``   — turns declarative config into runtime seam objects.

Read config via ``get_harness_config()`` (the single read point). ``get_harness_settings`` /
``HarnessSettings`` remain exported for back-compat with existing importers.
"""

from __future__ import annotations

from warden.config.models import HarnessConfig
from warden.config.settings import HarnessSettings, get_harness_settings


def get_harness_config() -> HarnessConfig:
    """The single read point: the nested config, derived from the env layer.

    Not cached — cheap to build from the (cached) ``get_harness_settings()`` —
    so tests that mutate the environment see fresh config without extra teardown.
    """
    return HarnessConfig.from_settings(get_harness_settings())


__all__ = [
    "HarnessConfig",
    "HarnessSettings",
    "get_harness_config",
    "get_harness_settings",
]
