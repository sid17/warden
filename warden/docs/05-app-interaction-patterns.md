# Interaction Patterns — a guide for apps built on the harness

> **Audience:** application developers driving the harness (via `ChatAPI` in-process, or
> the Runs API over HTTP). These patterns live in **your app**, not the harness.
>
> **Why this doc exists.** The harness used to ship three built-in "interaction modes" —
> `ask`, `note`, `free` — that prepended mode-specific instructions to the prompt. Those were
> removed because **prompt framing is application policy, not harness mechanism** (see
> [`01-conceptual-model.md`](./01-conceptual-model.md) §2 and §14). The harness now takes a prompt
> and runs it; it no longer knows what an "ask" or a "note" is.
>
> If your app wants ask/note/update-style behavior, you build it **in your app** and hand the
> harness a fully-formed prompt. This doc shows how, and preserves the original implementations
> as reference patterns. The removed code lives in git history under
> `warden/orchestrator/core/interactions/` and
> `warden/orchestrator/schemas/workflow/interactions.py` (removed 2026-07-16).

---

## The mental model

```
BEFORE (framing inside the engine):
   app  ──("Explain ch.3", mode="ask", context={...})──►  harness builds prefix + runs

AFTER (framing in the app):
   app builds the full prompt  ──("[Mode: Ask …] Explain ch.3")──►  harness just runs it
```

The harness contract is `send(prompt) → event stream`. A "mode" is just **a prompt your app
assembles before calling send**. Same for the viewing context, the note-saving instructions, etc.

**One rule to keep it clean:** assemble the *entire* prompt string in your app, then pass it as
the opaque `content`/`input.prompt`. Don't try to push mode semantics back into the harness.

---

## Pattern 1 — "Ask" (question about the thing the user is viewing)

The app decides where answers may come from (repo-only vs. external) and prepends a short
instruction, then the viewing context. Original engine implementation, for reference:

```python
# was: core/interactions/ask.py
_DEFAULT_GUIDANCE = (
    "Answer from the workspace repo context first. "
    "If the answer isn't in the repo, search externally."
)
_SOURCE_GUIDANCE = {
    ("repo",):     "Answer from the workspace repo context only. Do not search externally.",
    ("external",): "Search externally for the answer.",
}

def build_ask_prompt(user_text: str, context: dict, sources: list[str] | None = None) -> str:
    guidance = _SOURCE_GUIDANCE.get(tuple(sorted(sources or [])), _DEFAULT_GUIDANCE)
    prefix = f"[Mode: Ask — the user has a question about the artifact they're viewing. {guidance}]\n\n"
    return prefix + build_context_prefix(context) + user_text
```

Then: `api.send(build_ask_prompt(user_text, context, sources=["repo"]))`.

## Pattern 2 — "Note" (clean up and save a user's note)

The app owns the storage convention and the confirm-before-save policy. Reference:

```python
# was: core/interactions/note.py — storage-path convention
def note_output_path(context: dict) -> str:
    workflow = context.get("workflow") or ""
    instance = context.get("instance") or ""
    if not workflow:
        return ".notes/general/notes.md"
    return f".notes/{workflow}/{instance}/notes.md" if instance else f".notes/{workflow}/index.md"

# the mode instructions the engine used to inject:
#   1. Read the user's thought; fix grammar/spelling/clarity.
#   2. If intent is ambiguous, ask a clarifying question before saving.
#   3. Present the cleaned-up note and ask the user to confirm.
#   4. Only after confirmation, save to: {note_output_path}
#   Format: markdown. Append to existing file or create one.
```

The "confirm before saving" step is a **human-in-the-loop** interaction — see the HITL note below.

## Pattern 3 — "Update" (modify an artifact via natural-language feedback)

There was a schema for this (`UpdateInteraction`) but no prefix builder — it was a declared-but-
unimplemented mode. If you want it, it's just another app-assembled prompt: "[Mode: Update — apply
the following change to `{artifact}` …]". Nothing harness-specific.

## The shared "viewing context" prefix

All three modes shared one context-block builder. It's pure string assembly — keep it in your app:

```python
# was: core/interactions/utils/context.py
def build_context_prefix(context: dict | None) -> str:
    if not context or not context.get("viewing"):
        return ""
    parts = [f'Currently viewing: "{context["viewing"]}"']
    if context.get("workflow") and context.get("instance"):
        parts.append(f'Workflow: {context["workflow"]}, instance: {context["instance"]}')
    elif context.get("workflow"):
        parts.append(f'Workflow: {context["workflow"]}')
    if context.get("artifactPath"):
        parts.append(f'Artifact path: {context["artifactPath"]}')
    if context.get("sources"):
        parts.append(f'Reference files: {", ".join(context["sources"])}')
    return f'[{". ".join(parts)}.]\n\n'
```

There was also `utils/artifact_path.py`, which read `artifacts.base_path` from the workflow YAML
to resolve an instance's artifact path. If your app uses that convention, resolve the path in the
app and pass it in via `context["artifactPath"]`.

---

## Two things the harness still owns (don't rebuild these in the app)

1. **Per-turn tool scope.** The old interaction schema carried an `allowed_tools` list per mode —
   e.g. an "ask" turn could be restricted to read-only tools. That restriction is a *harness
   mechanism*, not prompt framing. **Do not enforce it by prompt text.** It should be a generic
   per-turn `tool_scope` input to the harness (the `ToolScope` type already exists and the
   constructor-level scope is still honored). Re-introducing per-turn scope should be done as a
   nameless `send(..., tool_scope=…)` parameter — **not** by reviving named "modes." Per-turn
   `tool_scope` is an **input to the `ToolScope` enforcement stage** (see
   [`01-conceptual-model.md`](./01-conceptual-model.md) §7a): it narrows a single turn's tool surface
   without minting a new workflow. It is *not* a third source of rules alongside the harness
   deny-baseline and the workflow manifest — configuration and enforcement stay separate.

2. **Human-in-the-loop confirmations.** The note flow's "ask the user to confirm before saving"
   is a real HITL round-trip — the harness's `PermissionHandler` seam. It works on **both** drive
   paths ([`01-conceptual-model.md`](./01-conceptual-model.md) §11): *synchronously* in-process
   (`ChatAPI` blocks the turn on the human), or *durably* over HTTP (the run pauses and resumes via
   `POST /runs/{id}/tool_confirmation`). Choose in-process for tight interactive loops, the durable
   HTTP path when the human may take minutes or hours to answer.

---

## Putting it together (in-process example)

```python
from warden.drive.api import ChatAPI

# config carries the tenant identity (config.workspace.user_id / .task_id) + provider.
api = ChatAPI(config, repo_path=workspace, workflow="course-review")
await api.init()

prompt = build_ask_prompt(user_text, context, sources=["repo"])   # app-side framing
async for event in api.send(prompt):                              # harness just runs it
    ...
await api.close()
```

The `workflow=` argument still exists and still supplies the **permission manifest** (tool/file
allow-deny) — that's harness mechanism and stays. Everything about *how the turn is phrased* is
now yours.
