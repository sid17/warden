"""Profile-aware ASGI entrypoint for the Runs-API server.

    WARDEN_PROFILE unset  → the plain, product-agnostic app (``harness_api.app:app``).
    WARDEN_PROFILE=<name> → load ``profiles/<name>/profile.py``'s ``PROFILE``, build
                             its per-run ``chat_api_factory``, and construct the app
                             around a ``Runner`` wired with that factory.
    WARDEN_PROFILE=<a.b.c> → a dotted value is a fully-qualified module base loaded
                             verbatim (``<a.b.c>.profile``) — so a product profile can
                             live OUTSIDE this engine tree (e.g. a private integration
                             package), keeping the open-source core product-agnostic.

This is the ONE seam that lets a product's per-run tool injection reach the generic
server WITHOUT ``harness_api/app.py`` learning about any product: the core app stays
byte-for-byte unchanged; a profile is applied only here, by env pointer. The profile
module lazy-imports product code, so an unset ``WARDEN_PROFILE`` never touches it.

Run it as::

    WARDEN_PROFILE=example python -m uvicorn \
        warden.harness_api.profiles.serve:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import importlib
import os

from warden.harness_api.app import app as _plain_app
from warden.harness_api.app import create_app
from warden.harness_api.config import get_harness_api_config
from warden.harness_api.runner import Runner

#: The built-in profiles package a bare ``WARDEN_PROFILE`` name resolves against.
_DEFAULT_PROFILES_PKG = "warden.harness_api.profiles"


def _profile_module_base(profile_name: str) -> str:
    """Resolve a ``WARDEN_PROFILE`` value to its Python module base.

    A **dotted** name is a fully-qualified package base (e.g. an out-of-tree
    integration profile like ``myco_integration.example.real``) and is
    used verbatim — this is the seam that lets a product profile live OUTSIDE the
    open-source engine tree. A **bare** name (e.g. ``example``)
    resolves against the built-in ``profiles/`` package, preserving existing usage.
    """
    return (
        profile_name
        if "." in profile_name
        else f"{_DEFAULT_PROFILES_PKG}.{profile_name}"
    )


def build_app():
    """Return the app for the selected profile (or the plain app when unset)."""
    profile_name = os.environ.get("WARDEN_PROFILE", "").strip()
    if not profile_name:
        return _plain_app

    module = importlib.import_module(f"{_profile_module_base(profile_name)}.profile")
    profile = module.PROFILE
    config = get_harness_api_config()
    factory = profile.build_factory(config)
    runner = Runner(config, chat_api_factory=factory)
    return create_app(runner=runner)


# uvicorn target: `...profiles.serve:app`.
app = build_app()
