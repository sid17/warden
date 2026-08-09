"""Product *profiles* for the real Runs-API harness — product-out-of-core.

A profile teaches the (product-agnostic) harness how ONE product drives it: it
supplies a per-run ``chat_api_factory`` that injects that product's custom tools,
plus a runnable driver/recipe. The harness core imports nothing from here; the
server entrypoint (:mod:`serve`) loads a profile only when ``WARDEN_PROFILE`` is
set, and the profile lazy-imports product code so the core stays clean.

Mirrors the mock's ``harness_api_mock/profiles/`` (task-14): same
product-out-of-core principle, adapted to the real Runner + in-process MCP tools.
"""
