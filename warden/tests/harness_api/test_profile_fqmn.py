"""FQMN-aware profile resolution (open-source split, Phase 2).

Both profile loaders — the real ``serve._profile_module_base`` and the mock
``profile_loader.load_profile`` — accept a *dotted* profile name as a fully-qualified
module base (used verbatim) and a *bare* name as a key under the built-in ``profiles/``
package. This is the seam that lets a product profile live OUTSIDE the open-source
engine tree (e.g. ``myco_integration.example.real``) without the core
naming any product.

These tests exercise the routing logic (bare vs dotted) directly; the real dotted
import is proven end-to-end when the relocated Learning profile is loaded via its new
FQMN in the mock-harness e2e.
"""

from __future__ import annotations

import pytest

from warden.harness_api.profiles.serve import (
    _DEFAULT_PROFILES_PKG,
    _profile_module_base,
)
from warden.harness_api_mock.profile_loader import load_profile


# --- real server (serve._profile_module_base) ---------------------------------


def test_serve_bare_name_prepends_default_pkg():
    assert _profile_module_base("learning") == f"{_DEFAULT_PROFILES_PKG}.learning"
    assert _profile_module_base("example") == f"{_DEFAULT_PROFILES_PKG}.example"


def test_serve_dotted_name_is_used_verbatim():
    fqmn = "myco_integration.example.real"
    assert _profile_module_base(fqmn) == fqmn


# --- mock loader (profile_loader.load_profile) --------------------------------


def test_mock_bare_unknown_reports_default_pkg_path():
    """A bare name routes under the built-in package — the error names that path."""
    with pytest.raises(ValueError, match=r"harness_api_mock\.profiles\.nope\.profile"):
        load_profile("nope")


def test_mock_dotted_unknown_is_used_verbatim():
    """A dotted name is used verbatim (NOT prefixed) — the error names it as given,
    proving the dotted branch routes an out-of-tree package base."""
    with pytest.raises(ValueError, match=r"my\.integration\.pkg\.profile"):
        load_profile("my.integration.pkg")
    # And it must NOT have been prefixed with the built-in package.
    try:
        load_profile("my.integration.pkg")
    except ValueError as exc:
        assert "harness_api_mock.profiles.my.integration.pkg" not in str(exc)
