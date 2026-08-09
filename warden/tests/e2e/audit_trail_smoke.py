"""M5 --audit-trail bed gate — real audit JSONL across providers + derivation + AUD-3.

The acceptance gate for M5 (doc 05 §4 "Real integration test"). Runs an audited
turn per provider via a CONFIG-first ChatAPI (AuditConfig(enabled=True, run_id,
log_dir) — NOT a raw AUDIT_ENABLED env read), then asserts the per-agent JSONL
trail landed and is well-formed, that the derivation pipeline (aggregate +
derive_manifest) proposes a per-sub-agent diff, and that a governance stop is
recorded in the trail (AUD-3).

Run on the HOST, OAuth Claude + OAuth Codex + free Ollama qwen3:8b:

    env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY PYTHONPATH=. \
      .venv/bin/python -m warden.tests.e2e.audit_trail_smoke [claude|openharness|codex|all]

Exits 0 on all-pass, 1 on any fail.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from warden import ChatAPI, ErrorEvent, HarnessConfig, MessageEvent
from warden.config.models import AuditConfig
from warden.observability.audit.aggregate import (
    load_events,
)
from warden.observability.audit.derive_manifest import derive_manifests
from warden.observability.audit.record import write_governance_stop
from warden.tests.observability.audit.test_smoke import validate_jsonl

# A multi-tool prompt: read a file + a bash command (+ a sub-agent for Claude).
PROMPTS = {
    "claude": "Do these steps with tools: (1) Read the file CLAUDE.md. (2) Run "
              "`wc -l CLAUDE.md` with the Bash tool. (3) Use the Agent tool to "
              "find files containing 'LANGFUSE'. Keep it brief.",
    "openharness": "Use the Read tool to read the file CLAUDE.md, then tell me "
                   "its first line. Use the tool — do not guess.",
    "codex": "Run the shell command `wc -l CLAUDE.md` and report the number.",
}


def _config(provider: str, run_id: str, log_dir: Path) -> HarnessConfig:
    cfg = HarnessConfig()
    cfg.provider.provider = provider
    if provider == "openharness":
        cfg.provider.openharness_model = "qwen3:8b"
    a = cfg.observability.audit
    a.enabled = True
    a.run_id = run_id
    a.log_dir = str(log_dir)
    return cfg


class _Allow:
    """can_use_tool allow-decision (codex exec is fail-closed by default)."""
    behavior = "allow"


async def _fire_codex_direct(prompt: str, run_id: str, log_dir: Path) -> None:
    """Codex has no config-threaded approval mode + exec is fail-closed, so drive
    the session DIRECTLY with an allowing can_use_tool (the bed's codex pattern),
    audit config-gated via the ``audit`` kwarg (the tap is built in the ctor).

    Uses a WRITE shell command (a side effect codex cannot estimate/hallucinate)
    so it reliably goes through the gated command-execution path — the proven
    exec trigger from codex_perm_smoke."""
    from warden.providers.codex.sdk_session import CodexSdkSession

    target = log_dir / f"codex_probe_{run_id}.txt"
    write_prompt = (
        f"Run the shell command: echo audit-ok > {target}\n"
        f"Use your shell tool to run it, then confirm it ran."
    )

    async def _allow_cb(tool_name, tool_input, context):
        print(f"    [tool_use] {tool_name}")
        return _Allow()

    session = CodexSdkSession(
        repo_path=Path("."),
        can_use_tool=_allow_cb,
        audit=AuditConfig(enabled=True, run_id=run_id, log_dir=str(log_dir)),
    )
    await session.start()
    try:
        async for ev in session.send(write_prompt):
            if isinstance(ev, dict) and ev.get("kind") == "error":
                print(f"    [run ERROR] {ev.get('text')}")
    finally:
        await session.close()


async def _fire(provider: str, prompt: str, run_id: str, log_dir: Path) -> None:
    if provider == "codex":
        await _fire_codex_direct(prompt, run_id, log_dir)
        return
    api = ChatAPI(_config(provider, run_id, log_dir), repo_path=".")
    await api.init()
    try:
        async for ev in api.send(prompt, workflow=None):
            if isinstance(ev, ErrorEvent):
                print(f"    [run ERROR] {ev.text}")
            elif isinstance(ev, MessageEvent) and ev.kind == "tool_use":
                print(f"    [tool_use] {ev.content.get('toolName', '?')}")
    finally:
        await api.close()


def _check(cond: bool, msg: str, failures: list) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        failures.append(msg)


# Per-provider what the trail must minimally show. Claude reliably drives tools +
# a sub-agent; Codex reliably runs the shell command; OpenHarness (qwen3:8b) is
# lenient — a valid non-empty trail with tool events when it does call the tool.
EXPECT = {
    "claude": {"event_types": ["PreToolUse", "PostToolUse"], "min_events": 2},
    "codex": {"event_types": ["PreToolUse", "PostToolUse"], "min_events": 2},
    "openharness": {"min_events": 1},
}


async def _assert_provider(provider: str, log_dir: Path, failures: list) -> str:
    run_id = f"audit-{provider}-{int(time.time())}"
    print(f"\n=== {provider}: fire audited turn (config-gated) → validate JSONL ===")
    await _fire(provider, PROMPTS[provider], run_id, log_dir)
    jsonl = log_dir / f"{run_id}.jsonl"
    _check(jsonl.exists(), f"audit JSONL written at {jsonl.name}", failures)
    if jsonl.exists():
        errs = validate_jsonl(str(jsonl), EXPECT[provider])
        _check(not errs, f"JSONL valid vs {EXPECT[provider]} (errs={errs})", failures)
        # gen_ai dot-notation present on a tool line (if any tool fired)
        lines = [json.loads(x) for x in jsonl.read_text().strip().split("\n") if x]
        tool_lines = [e for e in lines if e.get("event_type") == "PreToolUse"]
        if tool_lines:
            _check("gen_ai.tool.name" in tool_lines[0],
                   f"{provider}: PreToolUse carries gen_ai.tool.name dot-notation",
                   failures)
    return run_id


async def _assert_derivation(log_dir: Path, failures: list) -> None:
    print("\n=== derivation: aggregate + derive_manifest over the trails ===")
    events = load_events(str(log_dir))
    _check(len(events) > 0, f"loaded {len(events)} audit events for derivation",
           failures)
    manifests = derive_manifests(events)
    _check(len(manifests) > 0, f"derive_manifest proposed {len(manifests)} agent entries",
           failures)
    # root (if present) is kept broad
    if "root" in manifests:
        _check(manifests["root"]["disallowed_tools"] == [],
               "derivation keeps the root orchestrator broad (disallowed_tools: none)",
               failures)
    # every entry has the manifest shape
    shape_ok = all(
        {"disallowed_tools", "read_globs", "status"} <= set(e) for e in manifests.values()
    )
    _check(shape_ok, "every manifest entry has the proposed-diff shape", failures)


def _assert_aud3(log_dir: Path, failures: list) -> None:
    print("\n=== AUD-3: governance stop recorded in the audit trail ===")
    run_id = f"audit-gov-{int(time.time())}"
    write_governance_stop(
        AuditConfig(enabled=True, run_id=run_id, log_dir=str(log_dir)),
        "sess-gov", "budget",
    )
    jsonl = log_dir / f"{run_id}.jsonl"
    ok = False
    if jsonl.exists():
        line = json.loads(jsonl.read_text().strip().split("\n")[0])
        ok = line.get("event_type") == "Stop" and line.get("stop_reason") == "budget"
    _check(ok, "governance stopped(budget) recorded as a terminal Stop line", failures)


async def main(which: str) -> int:
    providers = ["claude", "openharness", "codex"] if which == "all" else [which]
    failures: list = []
    with tempfile.TemporaryDirectory(prefix="audit-trail-") as td:
        log_dir = Path(td)
        print(f"audit-trail gate — providers={providers} log_dir={log_dir}")
        for p in providers:
            await _assert_provider(p, log_dir, failures)
            # a 2nd run of the same provider so convergence is measurable
            await _assert_provider(p, log_dir, failures)
        await _assert_derivation(log_dir, failures)
        _assert_aud3(log_dir, failures)

    print("\n" + "=" * 60)
    if failures:
        print(f"AUDIT-TRAIL GATE: FAIL ({len(failures)} check(s))")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("AUDIT-TRAIL GATE: PASS")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    sys.exit(asyncio.run(main(arg)))
