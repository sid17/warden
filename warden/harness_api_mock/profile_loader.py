"""Profile contract + loader (task-14) — the engine's product seam.

A **profile** is the product-specific bundle the engine loads at boot: the canned
scripts, the writeback-invoker factory, and the fixtures dir. The engine core names
**no** product — it loads the active profile by convention from
``profiles/<name>/profile.py`` (``MockConfig.profile``), so registering a new
product's mock is literally *"drop a ``profiles/<name>/`` package exposing
``PROFILE``"* — no engine edit, not even a registry line.

The ``build_invoker`` factory takes the config **and** a ``job_id_for`` resolver
(``run_id -> task_id``) supplied by the runner's registry — the invoker needs it to
target the right product job, and only the runner holds that mapping (D7).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid runtime import cycles — these are type-only.
    from warden.harness_api_mock.config import MockConfig
    from warden.harness_api_mock.steps import Script
    from warden.harness_api_mock.tool_seam import ToolInvoker

# The dotted package the engine looks under for ``<name>/profile.py``.
_PROFILES_PKG = "warden.harness_api_mock.profiles"

# ``build_invoker(config, job_id_for) -> ToolInvoker`` — the runner passes its
# ``run_id -> task_id`` resolver so the invoker can target the product job (D7).
InvokerFactory = Callable[["MockConfig", Callable[[str], str]], "ToolInvoker"]


@dataclass(frozen=True)
class Profile:
    """A product's mock bundle — everything the engine needs that is not generic."""

    name: str
    scripts: dict[str, "Script"]  # workflow name -> Script (must include "default")
    fixture_dir: Path            # this profile's fixtures/ root
    build_invoker: InvokerFactory


def load_profile(name: str) -> Profile:
    """Import ``profiles/<name>/profile.py`` and return its ``PROFILE``.

    A **dotted** ``name`` is a fully-qualified module base used verbatim (so a product
    profile can live OUTSIDE this engine tree — e.g. a private integration package like
    ``myco_integration.example.mock``); a **bare** name resolves against the
    built-in ``profiles/`` package, preserving existing usage.

    Fail loud (LAW 4): an unknown profile name or a module that does not expose a
    ``PROFILE: Profile`` is a ``ValueError`` — never a silent fallback to some other
    product's scripts.
    """
    base = name if "." in name else f"{_PROFILES_PKG}.{name}"
    module_path = f"{base}.profile"
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        raise ValueError(
            f"unknown mock harness profile {name!r}: no module {module_path!r} "
            f"(create profiles/{name}/profile.py exposing PROFILE)"
        ) from exc
    profile = getattr(module, "PROFILE", None)
    if not isinstance(profile, Profile):
        raise ValueError(
            f"profile {name!r} module {module_path!r} does not expose a "
            f"PROFILE: Profile"
        )
    return profile


__all__ = ["Profile", "InvokerFactory", "load_profile"]
