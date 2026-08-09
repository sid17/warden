"""Managed credentials (harness-owned, shared across products).

- :class:`KeyRegistry` — operator-held multi-key registry + user→key/budget map;
  resolves the per-run ``auth_env`` injected into the provider subprocess.

The pricing table + cost function moved to
:mod:`warden.harness_api.governance.pricing`; the spend GATE moved to the
Governor's reservation ledger (the N10 allow-first ``SpendTracker`` is retired).

See the L1 plan §6 (managed multi-key + per-user spend cap).
"""

from warden.harness_api.credentials.keys import KeyRegistry, ManagedKey

__all__ = [
    "KeyRegistry",
    "ManagedKey",
]
