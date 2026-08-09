"""Product profiles for the mock harness (task-14).

Each subpackage is a self-contained **profile** — the product-specific bundle the
otherwise product-agnostic engine loads at boot: its canned scripts, its writeback
invoker, and its fixtures. The active profile is chosen by ``MockConfig.profile``
(env ``MOCK_WARDEN_PROFILE``) and loaded by ``profile_loader.load_profile``.

Adding a product's mock = drop a ``profiles/<name>/`` package exposing
``PROFILE: Profile`` (see ``../docs/ADDING-A-PROFILE.md``). The engine names no product.
"""
