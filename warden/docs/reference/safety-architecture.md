# Safety Architecture — Learnings & Recommendations

> Synthesized from 16 experiments across 4 rounds (v12 kickoff).
> Source data: `safety/experiments/results/` (including `strategy-summary.md`).

---

## Problem Statement

We run a general-purpose LLM harness (Claude Code SDK) that gives the model full access to tools, files, skills, and agents. For production use cases (e.g., a study assistant), we need to constrain what the model can do and what it reveals — without rebuilding the harness.

### The five challenges (from INTENT.md)

1. **Tool access is too broad.** The model can write, delete, and execute arbitrary commands even in read-only modes.
2. **Internal exposure.** The model reveals its skills, agents, system prompt, file structure, and configuration when asked.
3. **Intent manipulation.** Users can ignore the intended workflow and use the model for arbitrary purposes, including adversarial ones.
4. **No execution isolation.** A compromised model has the same access as the full system.
5. **Raw developer-facing UI.** Users see internal machinery instead of a guided experience.

---

## What Works — The Layered Architecture

No single strategy covers all threats. The minimum viable safety stack is 4 layers, each solving a distinct problem that no other layer addresses.

### Layer 1: Structural Absence — Custom System Prompt (E6)

**What:** Replace the Claude Code default system prompt with a minimal custom prompt. The model never receives skill names, agent names, tool descriptions, or CLAUDE.md content.

**Why it works:** The model can't leak what it doesn't know. This is the strongest anti-leakage strategy we tested — stronger than output filtering, stronger than tool restriction, stronger than "don't reveal your instructions" rules. The key insight from the scope-enforcement research: structural absence beats structural restriction.

**What it covers:**
- Internal exposure (challenge #2) — eliminated at the root
- Intent manipulation (challenge #3) — model stays in its defined role

**Tradeoff:** The model loses Claude Code's built-in tool usage patterns and may be slightly less capable at using Read/Grep/Glob. Worth it for production.

**Implementation:**
```python
system_prompt = "You are a study assistant. You help users understand content..."
# Pass as string (NOT preset+append) to ClaudeAgentOptions
```

### Layer 2: Tool Whitelist (E2)

**What:** Only register the tools the use case needs. For a study assistant: `["Read", "Grep", "Glob"]`. Everything else is mechanically blocked at the SDK level.

**Why it works:** Hard guarantee. The model literally cannot call Write, Edit, Bash, or Agent — the SDK removes them from its action space. No prompt trick, no jailbreak, no social engineering can bypass this.

**What it covers:**
- Tool access (challenge #1) — hard mechanical block
- Execution isolation (challenge #4) — blast radius limited to read-only tools

**Implementation:**
```python
allowed_tools = ["Read", "Grep", "Glob"]
```

### Layer 3: Input Middleware (E9 + E3)

**What:** Two middleware in sequence before the model sees the message:
- **E9 (intent classifier):** Phrase-matching that catches extraction attempts ("what skills do you have", "list your agents") and injection attempts ("override your directives", "you are now in unrestricted mode").
- **E3 (pattern middleware):** Substring matching that catches secret-seeking queries ("password", "API_KEY", "credential").

**Why it works:** Hard block — the message never reaches the model. The model can't comply with a request it never sees. E9 and E3 are complementary: E9 catches intent (extraction/injection), E3 catches content (secrets). Together they hard-block 4/6 test cases.

**What it covers:**
- Intent manipulation (challenge #3) — hard block on adversarial input
- Internal exposure (challenge #2) — blocks "tell me your skills" before the model sees it

**Limitation:** Substring/phrase matching is brittle to rephrasing and encoding evasion. A trained classifier (PromptGuard, DeBERTa) is more robust — these are now available as pluggable classifiers under `safety/middleware/classifiers/` (see "Beyond the substring baseline" below).

**Implementation:**
```python
middleware = [IntentClassifierMiddleware(), E3ExpandedMiddleware()]
```

### Layer 4: Output Middleware (E11 + E8)

**What:** Two filters on the model's response before the user sees it:
- **E11 (streaming filter):** Rolling 200-char buffer that checks for leaked skill names, agent names, `.claude/` paths, and "available skills" phrases. Streaming-compatible — yields clean chunks as they arrive.
- **E8 (post-sanitize):** Catches structural patterns in tool output — YAML frontmatter, absolute user paths, SKILL.md references.

**Why it works:** Defense-in-depth. Even with structural absence (Layer 1), the model might reconstruct or guess internal details from file contents it reads. The output filter catches what slips through all other layers.

**What it covers:**
- Internal exposure (challenge #2) — last line of defense

**Limitation:** Can produce false positives if legitimate queries mention skill names or `.claude/` paths. Buffering adds slight latency (~1-2 seconds for the 200-char window).

**Implementation:**
```python
# Streaming filter in the event loop — see drive/cli.py _collect_and_display()
# Post-sanitize checks structural patterns — see safety/middleware/output/sanitize.py sanitize_output()
```

### Optional: Canary Token (E15)

**What:** Embed a unique synthetic string in the system prompt. Check every output chunk for it. If it appears, the model is repeating the system prompt verbatim.

**Cost:** Zero — one string comparison per chunk.
**Value:** Backstop for verbatim prompt leakage. Never triggered in our tests, but free to include.

---

## The Complete Pipeline

```
User input
  → E9 intent classifier      (HARD: blocks extraction + injection)
  → E3 pattern middleware      (HARD: blocks secret-seeking)
  → E2 tool whitelist          (HARD: blocks Write/Edit/Bash/Agent)
  → E6 custom system prompt    (STRUCTURAL: model lacks internal knowledge)
  → Model generates response
  → E11 streaming filter       (FILTER: catches leaked skill names, streaming-compatible)
  → E8 post-sanitize           (FILTER: catches YAML frontmatter, .claude/ paths)
  → E15 canary check           (DETECT: verbatim prompt leak, zero-cost)
  → User sees response
```

### Coverage Matrix

| Challenge | Layer | Guarantee |
|-----------|-------|-----------|
| #1 Tool access too broad | E2 tool whitelist | **Hard** — mechanical |
| #2 Internal exposure | E6 (structural) + E9 (input) + E11/E8 (output) | **Triple-layered** — structural + hard + filtered |
| #3 Intent manipulation | E9 (extraction/injection) + E3 (secrets) + E6 (role) | **Hard** on known patterns, **soft** on novel rephrasing |
| #4 Execution isolation | E2 (tool scope) + E3 (secret blocking) | **Hard** — read-only + no credential access |
| #5 Raw developer UI | E6 (hides internals from model) | **Structural** — model can't expose what it doesn't know |

---

## What Does Not Work

### Approaches that failed in testing

| Approach | Experiment | Why it failed |
|----------|-----------|---------------|
| **Fuzzy string matching** | E12 | `difflib.SequenceMatcher` compares entire strings. Longer user messages dilute the ratio below threshold. All 6 TCs scored 20-53%, well below 0.6. |
| **Path-restricted Read via SDK callback** | E2b | The SDK's `can_use_tool` callback only fires for tools that need permission. Read is auto-allowed and never triggers it. Per-path blocking is impossible through this mechanism. |
| **Custom safe-read tool** | E13 | Replaced built-in Read with a path-validating wrapper. Zero safety impact — leakage comes from the model's context (pre-loaded skill names), not from file reads. |
| **Generic deny messages alone** | E7 | Changed "Tool 'Write' denied by workflow permissions" to "This action is not available." Cosmetic improvement only — the model still knows tool names from its context and mentions them anyway. |
| **Canary tokens as primary defense** | E15 | The canary never triggered. The model either obeyed the system prompt (didn't leak) or leaked from context (no verbatim prompt repetition). Useful only as a zero-cost backstop. |
| **Append mode (E1b) for anti-leakage** | E1b | Appending safety rules to the Claude Code preset keeps the default prompt which loads all skill/agent names into context. The model then has to be told not to share what it already knows — a soft constraint. Override (E6) is strictly better for leakage prevention. |

### Fundamental limitations

1. **Off-topic text has only soft protection.** There is no mechanical way to prevent the model from generating text that doesn't use tools. System prompt handles it well (~100% with Claude), but it's model-dependent. A Haiku-based input classifier could harden this.

2. **Input middleware is brittle to rephrasing.** Our E3/E9 use substring matching. "What skills do you have?" is caught, but "Describe the capabilities available to you" might not be. Trained classifiers (PromptGuard, DeBERTa, NeMo Guardrails) would fix this — they're the same middleware interface, just better detection.

3. **Output filtering can false-positive.** A user legitimately asking about a file that mentions "kickoff" or ".claude/" would get filtered. Threshold tuning (require 3+ skill name matches) reduces this but doesn't eliminate it.

4. **Structural absence trades capability for security.** The model with a custom prompt is less capable at using tools than with the full Claude Code preset. For a study assistant this is fine. For a power-user workflow, you'd need a selective prompt that includes tool usage patterns but not skill/agent lists.

---

## Beyond the substring baseline — pluggable classifiers

The input and output middleware are the most portable, harness-agnostic layer. They work in front of any LLM system. Since this synthesis, the homegrown substring matching has been supplemented with production-grade classifiers, all living under `safety/middleware/classifiers/` and selectable as drop-in replacements for E3/E9 (input) and E11/E8 (output) — same `before_send()` / output-check interface, better detection:

- **LlamaFirewall PromptGuard** — 86M and 22M variants (`promptguard_86m.py`, `promptguard_22m.py`)
- **DeBERTa (ONNX)** — fine-tuned for prompt injection (`deberta_onnx.py`)
- **LLM-as-judge** — small-model classifier call for input and output (`llm_judge.py`, `llm_output_judge.py`)
- **Ollama guard** — local-model guard for input and output (`ollama_guard.py`, `ollama_output.py`)
- **Enhanced regex** — hardened pattern matching, incl. base64-decode-and-scan (`enhanced_regex.py`, `regex_input.py`, `regex_output.py`)

NeMo Guardrails (Colang DSL + LLM-as-judge) was evaluated but is not currently wired in.
