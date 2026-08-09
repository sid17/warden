# 09 — Setting up the environment: auth & credentials

> **What this is.** How to authenticate the harness correctly — the credentials a
> run needs, the **options for each provider**, the **one preference that matters
> most** (prefer a long-lived OAuth token over a scraped interactive one), and the
> typed `resolve → inject` model the harness uses so a run gets exactly the right
> credential without ever leaking it. It complements `07` (one provider turn) and
> `08` (a session across turns); read those for *what* a run does, this for *how to
> give it the credentials to do it*.
>
> **Golden rule (cost):** prefer **OAuth / free-local** for everything. A
> subscription OAuth token is **$0 marginal**; a local model (Ollama) is free. The
> **API-key lane bills per token** — use it only for a deliberate API-key test, and
> know you're paying. See [§2](#2-the-cost-model--the-golden-rule).

---

## 1. What a run needs

Two independent kinds of credential — keep them separate:

| Kind | Direction | Purpose | Expires? |
|------|-----------|---------|----------|
| **Provider auth** | harness → the model provider (Claude / Codex / a local model) | so the agent can call the LLM | **OAuth tokens can** — the only thing that expires |
| **Caller auth** *(only if you run the Runs API server)* | a client → the harness | so the harness only accepts runs from known callers | No — a static shared secret |

A single-provider CLI run needs only **provider auth**. The HTTP Runs API adds
**caller auth** ([§7](#7-caller-auth-for-the-runs-api)). Everything else (state dir,
telemetry, S3 persistence) is optional config, not credentials.

---

## 2. The cost model & the golden rule

| Lane | How it authenticates | Who pays | Use it for |
|------|----------------------|----------|------------|
| **OAuth** (subscription) | Claude `CLAUDE_CODE_OAUTH_TOKEN`; Codex ChatGPT session file | **$0 marginal** — your existing subscription | **The default for everything.** |
| **Free local** | a local model server (e.g. Ollama) — no cloud credential | **$0** — runs on your machine | Any "pure" functional run that just needs *a* model. |
| **API key** | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | **Bills per token** | Only a deliberate API-key-specific test. |

**Reach for OAuth first, a local model second, an API key never** — unless the
*point* of the run is the API-key path. A run that just needs *a* model to exercise
a permission gate or a custom tool should use the free lane, not a billed key. The
one trap that silently flips you onto the billed lane is a stray API key in your
shell — see [§6.1](#61-the-api-key-trap).

---

## 3. Provider auth — the options

### 3.1 Claude — prefer a `setup-token` (long-lived)

Ranked best → worst for a **headless** harness:

1. **✅ `claude setup-token` (long-lived, ~1 year).** A token minted for headless/CI
   use. It needs no mid-run refresh, so a multi-minute run — or one parked at a gate
   for hours — never 401s. This is the correct credential for a headless harness.
   ```bash
   claude setup-token          # interactive once; prints  sk-ant-oat01-…
   ```
   Put the printed value in the environment as `CLAUDE_CODE_OAUTH_TOKEN` (in a
   git-ignored `.env.local`, not your shell rc — see [§6.2](#62-precedence-os-env-wins)).

2. **⚠️ Interactive login token (`claude login`).** The access token from an
   interactive login is **short-lived (hours)** with no refresh token available to a
   headless process — it will **401 mid-run** on anything longer than a smoke test.
   On macOS it lives in the Keychain item `Claude Code-credentials`
   (`claudeAiOauth.accessToken`); harvesting it is a fine way to *unblock immediately*
   but not to run anything real.

3. **❌ API key (`ANTHROPIC_API_KEY`).** Bills. Only for a deliberate API-key test.

### 3.2 Codex — ChatGPT OAuth (a session file)

Codex authenticates via a **session file** written by `codex login` (ChatGPT OAuth),
by default at `~/.codex/auth.json`. The harness injects it by setting `CODEX_HOME` to
the directory holding it (see `SessionFile` in [§4](#4-the-typed-resolve--inject-model))
— no token is copied, the file is the credential. An `OPENAI_API_KEY` in the
environment **overrides** this onto the billed lane ([§6.1](#61-the-api-key-trap)).

### 3.3 A local model (OpenHarness / Ollama) — no credential

The local-model provider talks to a model server (e.g. Ollama at
`http://localhost:11434`) and needs **no cloud credential**. This is the lane to
prefer for any run that just needs *a* model. Setup is just running the server and
pulling a model (`ollama pull <model>`).

---

## 4. The typed resolve → inject model

The harness does not read `os.environ` ad-hoc per provider. It resolves a typed
**`AuthMethod`** per `(user, provider)` and injects it through **one** function.

**The union** (`harness_api/credentials/methods.py:104`):

```python
AuthMethod = Union[OAuthToken, ApiKey, SessionFile, Inherit]
#   OAuthToken  → injects an OAuth token env var (e.g. CLAUDE_CODE_OAUTH_TOKEN)
#   ApiKey      → injects a provider API key   (the billed lane)
#   SessionFile → sets a home var (e.g. CODEX_HOME) pointing at a login file
#   Inherit     → inject nothing; the run uses the launching process's credential
```

**The pieces:**

- **`CredentialStore`** (`credentials/store.py`) — records keyed by `(user_id,
  provider)`, so one user can hold a **Claude OAuth token AND a Codex key at once**.
  Backed by a JSONL file (`credentials.jsonl`) that replays on restart.
- **Secrets by reference.** A record stores the **env-var *name*** of the secret
  (`secret_ref`), never the value — so the store file stays committable/diffable and
  no secret lands in it. The value is injected at process-spawn time.
- **A policy gate** (`credentials/resolver.py`) — an OAuth allow-list
  (`oauth_allowed_users`); a non-whitelisted user's OAuth request can `downgrade` to a
  managed API key (`on_oauth_denied`). Configured via `AuthConfig` (`harness_api/config.py`).
- **One strip-then-inject** (`credentials/injection.py:apply_method`) — strips any
  ambient auth env then injects the resolved one, **never mutating `os.environ`**, so
  two concurrent runs never bleed credentials into each other.

**Empty store ⇒ `Inherit`.** With no matching record, `auth_env_for` returns `None`
and the run inherits the launching process's credential (the simple single-key case).
So the typed store is **opt-in**: you get it when you seed records, and the legacy
"one key in the env" behavior otherwise.

### Seeding the store by hand

Drive the typed path directly — put secret **values** in the environment under any
names you like, store only the **names**, then let the resolver inject:

```bash
# 1. The secret VALUES live in your env, under names you choose (never in the store):
export MY_CLAUDE_OAUTH="sk-ant-oat01-…"      # e.g. from `claude setup-token`
export MY_OPENAI="sk-…"                        # or leave codex on a session_file record

# 2. Declare the typed store via AUTH_* config (maps into AuthConfig):
export AUTH_STORE_BACKEND=jsonl
export AUTH_STATE_DIR=/tmp/auth_demo           # credentials.jsonl is written here
export AUTH_OAUTH_ALLOWED_USERS=me             # OAuth allow-list ("*" = open)
export AUTH_ON_OAUTH_DENIED=downgrade          # non-whitelisted OAuth → managed api-key
```

```python
# 3. Seed records (secret REFERENCES, never values) and resolve+inject:
import asyncio, os
from pathlib import Path
from warden.harness_api.config import get_harness_api_config
from warden.harness_api.credentials.config import build_auth_resolver, init_auth
from warden.harness_api.credentials.store import CredentialRecord, JsonlCredentialStore

async def main():
    store = JsonlCredentialStore(Path(os.environ["AUTH_STATE_DIR"]) / "credentials.jsonl")
    await store.load()
    await store.put(CredentialRecord(user_id="me", provider="claude",
                                     auth_method="oauth", secret_ref="MY_CLAUDE_OAUTH"))
    await store.put(CredentialRecord(user_id="me", provider="codex",
                                     auth_method="api_key", secret_ref="MY_OPENAI"))
    resolver = build_auth_resolver(get_harness_api_config())
    await init_auth(resolver)                              # replays credentials.jsonl
    for user, prov in [("me", "claude"), ("me", "codex")]:
        env = resolver.auth_env_for(user, prov) or {}      # the {VAR: secret} overlay
        print(user, prov, {k: v[:4] + "…" for k, v in env.items()})   # never print secrets

asyncio.run(main())
```

The written `credentials.jsonl` holds `"secret_ref": "MY_CLAUDE_OAUTH"` — the **name**,
proving secrets stay by reference. (Note: `get_harness_api_config()` is cached — export
the `AUTH_*` vars **before** the first read, i.e. in a fresh process; in tests build an
`AuthConfig(...)` directly.)

---

## 5. Failure symptom: an expired token

When a provider OAuth token has expired, the model call **401s mid-run**: the agent
produces no output for that turn and the run ends without completing its work. If you
see this on *every* new run at once, **suspect an expired token first**, not a bug —
this is exactly why [§3.1](#31-claude--prefer-a-setup-token-long-lived) prefers the
long-lived `setup-token` over a short-lived interactive one. Fix = drop a fresh
`setup-token` into the environment and restart.

---

## 6. Environment hygiene

### 6.1 The API-key trap

If `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` is set in your shell, the provider silently
uses the **billed** API-key lane instead of free OAuth/local. Strip it for a run with
`env -u`:

```bash
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY <your run command>
# check first:
echo "${OPENAI_API_KEY:+SET → will BILL}${OPENAI_API_KEY:-unset → good}"
```

And **do not blanket-`source .env`** if it contains billed keys — that exports them
into your shell and every later run silently switches to the billed lane. Export only
the specific vars you need.

### 6.2 Precedence: OS env wins

Loaders (a `.env.local` / `.env`) are read **only if the var isn't already set** — an
exported shell value **wins**, and empty values are dropped (so a blank placeholder
falls back rather than injecting an empty token). The consequence that bites people:
exporting a short-lived token "just to refresh" **overrides** the durable one in
`.env.local`, and the run then 401s mid-stream. So put the durable `setup-token` in
`.env.local` and **don't** export a competing one in your shell.

---

## 7. Caller auth for the Runs API

Only relevant if you run the HTTP **Runs API** server (`docker/run-harness-api.sh`).
A per-service token (`x-service-token`) authenticates the *client calling the harness*
— separate from provider auth. The registry (`credentials/service_tokens.py`,
`ServiceTokenRegistry.verify`) loads its `{name → token}` map from
`SERVICE_TOKENS_JSON` (inline) → `SERVICE_TOKENS_FILE` (path) → empty:

```bash
SERVICE_TOKENS_JSON='{"my-app":"<static-secret>"}' docker/run-harness-api.sh
```

- **Set** ⇒ enforced: a request must present a matching `x-service-token` (+ its
  `x-user-id` for per-user ownership on run-scoped routes).
- **Unset** ⇒ open registry (fine for a single-tenant local dev server).

The token is a **static secret** — generate once (`openssl rand -hex 32`), never
expires. It must match on both sides (the caller sends it; the harness verifies it).

---

## 8. Running in a container

The same rules, applied to `docker run`:

- **Inject at run time, never bake in.** Pass the resolved credential as `-e VAR`
  (OAuth token) or a read-only bind-mount (a session file → `CODEX_HOME`). Secrets are
  **never** copied into the image.
- **Strip billed keys** at the host boundary: `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY docker run …`.
- **Keep credentials out of persisted state.** When a run's workspace is snapshotted
  (for crash-recovery — see `08`), the credential is **excluded** from the snapshot and
  **re-injected out-of-band** on restore, so a backup never carries a secret.
- Reach a host-run local model server from inside the container via the platform's
  host-gateway address rather than `localhost`.

---

## 9. Preflight recon (safe — prints booleans, never secrets)

Confirm you have what a run needs before starting. This prints only presence/names:

```bash
# Claude OAuth token present in the environment? (bool — no value)
echo "claude oauth: ${CLAUDE_CODE_OAUTH_TOKEN:+present}${CLAUDE_CODE_OAUTH_TOKEN:-MISSING}"
# Codex login file?
[ -f ~/.codex/auth.json ] && echo "codex auth.json: EXISTS" || echo "codex auth.json: MISSING"
# Local model server + models?
curl -s --max-time 3 localhost:11434/api/tags \
  | python3 -c 'import json,sys; print("local models:", [m["name"] for m in json.load(sys.stdin).get("models",[])])' 2>/dev/null \
  || echo "local model server: DOWN"
# No billed key lurking in the shell?
echo "${OPENAI_API_KEY:+OPENAI_API_KEY SET — unset before a free run}${OPENAI_API_KEY:-OPENAI_API_KEY unset — good}"
```

---

## 10. Cross-references

| For… | See |
|------|-----|
| One provider turn — the contract + Docker bed philosophy | `07-provider-contract-and-testing.md` |
| Sessions / resume-recall / crash-recovery — the contract | `08-session-contract-and-testing.md` |
| Adding a profile (product code on top of the generic harness) | `10-adding-a-profile.md` |
| The auth-module internals (typed union, store, resolver, no-bleed) | `harness_api/credentials/` + its tests under `tests/harness_api/credentials/` |
| The generic Runs API server launcher | `docker/run-harness-api.sh`, `docker/README.md` |
