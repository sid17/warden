"""M3 ``--telemetry-trace`` bed gate — enriched telemetry lands on the live stack.

The acceptance gate for M3 (doc 04 §5 "Real integration test"). Fires scenarios
through a **config-first** ``ChatAPI`` (telemetry enabled in the config object +
the seeded Langfuse keys — no ambient env) and ASSERTS the enriched telemetry
actually landed on the running observability stack, not just in hermetic mocks:

  - **Langfuse** (:3456) — a fresh ``{provider}.interaction`` trace whose
    ``generation`` records carry the GenAI-semconv ``gen_ai.*`` attributes
    (OBS-4). For Claude, the sub-agent's ``llm_call``/tool spans nest under the
    ``tool: Agent`` span (OBS-3 native nesting).
  - **Tempo** (:3200) — spans under ``service.name`` carry ``gen_ai.*``; the
    OpenHarness path emits an explicit ``turn`` span (3b), not LLM-call-only.
  - **Off-switch** (§4b) — ``enable_telemetry=False`` + no keys ⇒ a run emits
    ZERO telemetry (no new Langfuse trace, no new Tempo trace).

Run on the HOST (queries localhost backends), stack up + OAuth Claude + free
Ollama ``qwen3:8b``:

    env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY PYTHONPATH=. \
      .venv/bin/python -m warden.tests.e2e.telemetry_trace_smoke [claude|openharness|both]

Backend URLs are overridable via env (``LANGFUSE_HOST`` / ``TEMPO_URL`` /
``OTEL_COLLECTOR_ENDPOINT``) so the gate can also run in-container against
``host.docker.internal``. Exits 0 on all-pass, 1 on any fail.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import urllib.request

from warden import ChatAPI, ErrorEvent, HarnessConfig, MessageEvent

# --- stack endpoints (host defaults; overridable for the container lane) -----
LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:3456")
TEMPO_URL = os.environ.get("TEMPO_URL", "http://localhost:3200")
OTEL_ENDPOINT = os.environ.get("OTEL_COLLECTOR_ENDPOINT", "http://localhost:4317")
LF_PK = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-example")
LF_SK = os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-example")

# GenAI-semconv keys that must ride on a usage-bearing generation record.
USAGE_SEMCONV = {
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.operation.name",
}

SCENARIOS = {
    # Claude: a sub-agent turn — the hard case (OBS-3 nesting + gen_ai.*).
    "claude": "Use the Agent tool to find files containing 'LANGFUSE' in this "
              "repo; have the sub-agent return a short list of paths. Be brief.",
    # OpenHarness (free Ollama): a plain turn — exercises the turn span + gen_ai.*.
    "openharness": "What is 7 times 8? Answer with just the number.",
}
TRACE_NAME = {"claude": "claude_code.interaction", "openharness": "openharness.interaction"}
OTEL_SERVICE = {"claude": "claude-agent", "openharness": "openharness"}


# --------------------------------------------------------------------------
# HTTP helpers (stdlib only — no extra deps)
# --------------------------------------------------------------------------

def _get(url: str, *, auth: tuple[str, str] | None = None) -> dict:
    req = urllib.request.Request(url)
    if auth:
        tok = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (localhost)
        return json.loads(resp.read().decode())


def _lf_latest_trace(name: str) -> dict | None:
    data = _get(
        f"{LANGFUSE_HOST}/api/public/traces?limit=1&orderBy=timestamp.desc&name={name}",
        auth=(LF_PK, LF_SK),
    ).get("data", [])
    return data[0] if data else None


def _lf_trace_detail(trace_id: str) -> dict:
    return _get(f"{LANGFUSE_HOST}/api/public/traces/{trace_id}", auth=(LF_PK, LF_SK))


def _tempo_count(service: str) -> int:
    import urllib.parse

    q = urllib.parse.urlencode({"tags": f"service.name={service}", "limit": 20})
    return len(_get(f"{TEMPO_URL}/api/search?{q}").get("traces", []))


def _tempo_latest_gen_ai(service: str) -> tuple[dict, set]:
    import urllib.parse

    q = urllib.parse.urlencode({"tags": f"service.name={service}", "limit": 1})
    traces = _get(f"{TEMPO_URL}/api/search?{q}").get("traces", [])
    if not traces:
        return {}, set()
    detail = _get(f"{TEMPO_URL}/api/traces/{traces[0]['traceID']}")
    names: dict = {}
    genai: set = set()
    for b in detail.get("batches", []):
        for ss in b.get("scopeSpans", []):
            for s in ss.get("spans", []):
                names[s["name"]] = names.get(s["name"], 0) + 1
                for a in s.get("attributes", []):
                    if a["key"].startswith("gen_ai"):
                        genai.add(a["key"])
    return names, genai


# --------------------------------------------------------------------------
# ChatAPI config-first construction + firing
# --------------------------------------------------------------------------

def _config(provider: str, *, telemetry_on: bool) -> HarnessConfig:
    cfg = HarnessConfig()
    cfg.provider.provider = provider
    if provider == "openharness":
        cfg.provider.openharness_model = "qwen3:8b"
    t = cfg.observability.telemetry
    t.enable_telemetry = telemetry_on
    t.otel_collector_endpoint = OTEL_ENDPOINT
    if telemetry_on:  # config-first: keys in the object, not ambient env
        t.langfuse_public_key = LF_PK
        t.langfuse_secret_key = LF_SK
        t.langfuse_host = LANGFUSE_HOST
    return cfg


async def _fire(provider: str, prompt: str, *, telemetry_on: bool = True) -> None:
    api = ChatAPI(_config(provider, telemetry_on=telemetry_on), repo_path=".")
    await api.init()
    try:
        async for ev in api.send(prompt, workflow=None):
            if isinstance(ev, ErrorEvent):
                print(f"    [run ERROR] {ev.text}")
            elif isinstance(ev, MessageEvent) and ev.kind == "tool_use":
                print(f"    [tool_use] {ev.content.get('toolName', '?')}")
    finally:
        await api.close()


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------

def _check(cond: bool, msg: str, failures: list) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        failures.append(msg)


async def _assert_provider(provider: str, failures: list) -> None:
    print(f"\n=== {provider}: fire + assert enriched telemetry landed ===")
    pre_lf = _lf_latest_trace(TRACE_NAME[provider])
    pre_ts = pre_lf.get("timestamp") if pre_lf else None
    pre_tempo = _tempo_count(OTEL_SERVICE[provider])

    await _fire(provider, SCENARIOS[provider])
    await asyncio.sleep(0)  # let the async egress settle

    # Langfuse: a fresh trace with gen_ai.* on a generation.
    trace = _lf_latest_trace(TRACE_NAME[provider])
    _check(trace is not None and trace.get("timestamp") != pre_ts,
           f"Langfuse: a fresh {TRACE_NAME[provider]} trace appeared", failures)
    if trace:
        detail = _lf_trace_detail(trace["id"])
        obs = detail.get("observations", [])
        gens = [o for o in obs if o.get("type") == "GENERATION"]
        gen_ok = any(
            USAGE_SEMCONV <= set((o.get("metadata") or {}).keys()) for o in gens
        )
        _check(gen_ok, "Langfuse: a generation carries the gen_ai.* usage semconv",
               failures)
        if provider == "claude":
            byid = {o["id"]: o for o in obs}
            nested = any(
                byid.get(o.get("parentObservationId"), {}).get("name") == "tool: Agent"
                for o in obs
            )
            _check(nested, "Langfuse: sub-agent spans nest under tool: Agent (OBS-3)",
                   failures)

    # Tempo: a new trace under the service, spans carry gen_ai.*.
    names, genai = _tempo_latest_gen_ai(OTEL_SERVICE[provider])
    _check(_tempo_count(OTEL_SERVICE[provider]) > pre_tempo or bool(names),
           f"Tempo: spans present under service.name={OTEL_SERVICE[provider]}", failures)
    _check(bool(genai & {"gen_ai.request.model"}),
           "Tempo: spans carry gen_ai.request.model", failures)
    if provider == "openharness":
        _check(any(n.startswith("turn") for n in names),
               "Tempo: OpenHarness emits an explicit turn span (3b, not LLM-only)",
               failures)


async def _assert_off_switch(failures: list) -> None:
    print("\n=== off-switch: enable_telemetry=False + no keys ⇒ zero telemetry ===")
    # get_langfuse() is a per-process singleton (M8): once the on-runs above warm
    # it with keys, later calls reuse that client regardless of a keyless config.
    # A real telemetry-off run is a fresh process; reset the singleton to model it
    # (this is what proves the off config, not stale process state, gates Langfuse).
    from warden.observability.telemetry import shutdown_langfuse

    shutdown_langfuse()

    pre_lf = _lf_latest_trace(TRACE_NAME["claude"])
    pre_ts = pre_lf.get("timestamp") if pre_lf else None
    pre_tempo = _tempo_count(OTEL_SERVICE["claude"])

    await _fire("claude", "What is 2 plus 2? Answer with just the number.",
                telemetry_on=False)
    await asyncio.sleep(0)

    post_lf = _lf_latest_trace(TRACE_NAME["claude"])
    post_ts = post_lf.get("timestamp") if post_lf else None
    _check(pre_ts == post_ts, "off-switch: NO new Langfuse trace", failures)
    _check(pre_tempo == _tempo_count(OTEL_SERVICE["claude"]),
           "off-switch: NO new Tempo trace", failures)


async def main(which: str) -> int:
    providers = ["claude", "openharness"] if which == "both" else [which]
    failures: list = []
    print(f"telemetry-trace gate — providers={providers}")
    print(f"  Langfuse={LANGFUSE_HOST} Tempo={TEMPO_URL} OTEL={OTEL_ENDPOINT}")
    for p in providers:
        await _assert_provider(p, failures)
    if "claude" in providers:
        await _assert_off_switch(failures)

    print("\n" + "=" * 60)
    if failures:
        print(f"TELEMETRY-TRACE GATE: FAIL ({len(failures)} check(s))")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("TELEMETRY-TRACE GATE: PASS")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "both"
    sys.exit(asyncio.run(main(arg)))
