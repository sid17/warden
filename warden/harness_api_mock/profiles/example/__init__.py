"""Example profile — the mock harness's product-agnostic reference profile.

The open-source engine ships this so the mock harness has a runnable, demonstrable
profile out of the box (and a neutral default that names no product). Real products
supply their own profile out-of-tree, loaded by fully-qualified ``MOCK_WARDEN_PROFILE``
(see the FQMN-aware ``profile_loader.load_profile``).
"""
