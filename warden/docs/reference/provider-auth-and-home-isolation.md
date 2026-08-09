# Provider Auth & Home Isolation

> How authentication works across the four harnesses, and why **relocating a
> provider's home directory (which is how task persistence works) strands the
> ambient login unless you inject a token via the environment.**

Applies to: `claude` (SDK), `claude-cli` (`claude -p`), `codex`, `openharness`.
See the code in `workspace/task_workspace.py` (`home_env`).

---

## The one rule

**When a task pins the provider's home into its folder, auth must come from an env
token — not from the machine's interactive login.**

```
CLAUDE_CODE_OAUTH_TOKEN   # claude / claude-cli   (from `claude setup-token`)
ANTHROPIC_API_KEY         # claude / claude-cli   (API-key billing)
OPENAI_API_KEY            # codex
```

Set one of these in the environment of the process that launches the provider.
There is **no "the box is already logged in, so it'll be fine" fallback** once
persistence is on.

---

## Why — two things that look like one

Auth and session state feel like the same thing ("my login"), but they live in
two independent places:

| | Where it lives (default) | What we do to it for persistence |
|---|---|---|
| **Session / home** — transcripts, resume metadata, settings | the provider's *home dir* (`~/.claude`, `~/.codex`) | **relocate** into the task folder (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`) so it travels with the restore unit |
| **Credentials** | macOS Keychain (claude) or a file inside the home (`~/.codex/auth.json`) | must be re-supplied to the relocated home |

Persistence *requires* relocating the home — that is the whole mechanism that makes
a task folder one self-contained, tar-and-restore unit. But relocating the home is
exactly what severs the ambient login:

- **claude on macOS:** the OAuth token is in the **Keychain**, keyed to the default
  home. A fresh `CLAUDE_CONFIG_DIR` is *not* seen as logged in → the CLI reports
  **"Not logged in"** (`is_error: true`). Verified live in the Phase 2 integration
  test. This is not an edge case — relocating the home *always* does this.
- **codex:** credentials are a **file** (`auth.json`) *inside* `CODEX_HOME`. Point
  `CODEX_HOME` at a fresh task dir and that file is absent → unauthenticated, unless
  you inject `OPENAI_API_KEY` (or seed `auth.json`).

So the fix — hand the subprocess an explicit token via `env` — is **load-bearing**:
remove it and *every* persisted task fails to authenticate.

---

## Per-provider reference

| Provider | Default credential store | Home relocation this pass? | Auth under relocation |
|---|---|---|---|
| **claude-cli** (`claude -p`) | macOS Keychain (`apiKeySource: none`) or `~/.claude/.credentials.json` on Linux | **Yes** — `CLAUDE_CONFIG_DIR=<task>/.claude-home` | **Must** inject `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` |
| **claude** (SDK) | env token, or the CLI's login | No (SDK home not relocated this pass) | env token (standard); no stranding because the home isn't moved |
| **codex** (`codex exec`) | `auth.json` inside `CODEX_HOME` (holds `auth_mode`, `OPENAI_API_KEY`, OAuth `tokens`) | **Yes** — `CODEX_HOME=<task>/.codex` | Inject `OPENAI_API_KEY`, or seed `auth.json` into the relocated home |
| **openharness** (library) | none — local Ollama, dummy key `"ollama"` | No (transcript path hardcoded) | none for local Ollama; supply `api_key` only if pointed at a real OpenAI-compatible endpoint |

---

## How the code handles it — `home_env()`

Two functions, cleanly separated:

- **`providers/auth.py::resolve_auth(provider, env=None)`** — the auth
  seam. Given a provider it returns the credential env vars to inject, reading from
  `env` (defaults to `os.environ`). It is **pure**: no file/Keychain reads, no
  `os.environ` mutation. The `env` parameter *is* the pluggability point — a container
  passes creds in as env; a secret manager can pre-populate the mapping. Companions:
  `is_authed(provider, env=None)` and `auth_hint(provider)` for CLI/container preflight.
- **`workspace/task_workspace.py::home_env(task_dir, provider)`** — the
  home-relocation composer. It builds the `CLAUDE_CONFIG_DIR`/`CODEX_HOME` var, then
  merges in `resolve_auth(provider)`:

```python
home_env(task_dir, "claude-cli")
# -> {"CLAUDE_CONFIG_DIR": ".../task/.claude-home",
#     "CLAUDE_CODE_OAUTH_TOKEN": "<value>"}   # token only if present in os.environ
```

Rules baked into it (all non-negotiable — see the gotchas below):

1. **Token is copied from the launching env**, only if already set. `home_env` does
   not fetch or discover tokens — whoever starts the process is responsible for
   having one in the environment.
2. **Never mutates `os.environ`.** It returns a plain dict; the caller merges it onto
   the *subprocess* `env` only. (Global mutation breaks concurrent tasks.)
3. **Never writes a token to a file** — not to the workspace, not to
   `bootstrap.lock.json`, not into the backup archive.

The wired path (`Orchestrator` → `SessionManager.create/resume`) passes the home
subfolder to the provider constructor (`claude_config_dir` / `codex_home`); the
provider then builds `env = {**os.environ, HOME_VAR: ...}` for its subprocess, so any
token in `os.environ` is inherited automatically.

---

## Getting a token

| Env var | How to obtain | Billing |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | run `claude setup-token` (works with a Pro/Max subscription) | subscription/OAuth |
| `ANTHROPIC_API_KEY` | Anthropic Console → API keys | API usage |
| `OPENAI_API_KEY` | OpenAI dashboard (or copy from `~/.codex/auth.json`) | API usage |

Either Claude token works; the provider accepts whichever is present.

---

## Local dev vs. prod vs. tests

- **Production / servers:** export `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`)
  and `OPENAI_API_KEY` in the launching environment (systemd unit, container env,
  secret manager). This is the contract — persistence does not work without it.
- **Local dev without persistence:** if you don't pin a home (no `--task-id`), the
  CLI uses your normal `~/.claude` login and no token is needed.
- **Tests:** the Phase 2 integration test runs the *real* CLI with a pinned home, so
  it needs a token. On a machine whose only credential is an interactive Keychain
  login (this dev box), a **test-only** autouse fixture reads the OAuth token out of
  the Keychain and exports `CLAUDE_CODE_OAUTH_TOKEN` for the subprocess to inherit.
  No production code reads the Keychain. If no token can be obtained, the tests
  **skip** (they never fail for lack of auth).

---

## Running in a container (Docker)

A container has none of the host's ambient logins, so auth must be handed in — this
is exactly the seam `resolve_auth` was built for. `docker/` contains a working
reference (Dockerfile + `run.sh`) that runs all four providers in isolation; all four
were verified `PASS` in-container. How each credential crosses the boundary:

| Provider | How auth enters the container |
|---|---|
| **claude / claude-cli** | `-e CLAUDE_CODE_OAUTH_TOKEN=…` — `run.sh` extracts it from the host Keychain at launch (never baked into the image). `ANTHROPIC_API_KEY` works too. |
| **codex** | bind-mount `~/.codex/auth.json` into a writable `CODEX_HOME` (ChatGPT OAuth is **file-based**, not an env var). Also needs a **git-inited workdir** — `codex exec` refuses a non-trusted dir. |
| **openharness** | no cloud cred; set `OPENHARNESS_BASE_URL=http://host.docker.internal:11434` + `--add-host=host.docker.internal:host-gateway` to reach the host's Ollama. |

Rule of thumb: **the credential never lives in the image** — it's injected at
`docker run` via `-e` (secrets) or `-v` (OAuth files). See `docker/README.md`.

### Caveat — `is_authed("codex")` is env-only

`is_authed`/`resolve_auth` model the **env-token** path. Codex on a ChatGPT-OAuth
machine authenticates from a **file** (`auth.json`), which the pure env seam cannot
see — so `is_authed("codex")` reports `False` even when codex will happily run from a
mounted `auth.json`. Treat `is_authed("codex")` as "is there an `OPENAI_API_KEY`?",
not "can codex authenticate?". A read-only `auth.json` mount also can't persist an
OAuth **refresh**, so a stale ChatGPT token will fail in-container — refresh on the
host first, or mount `:rw`.

---

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `"Not logged in"` / `is_error: true` from `claude -p` with a pinned home | Keychain login not seen by the relocated `CLAUDE_CONFIG_DIR` | export `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` |
| codex auth error with a pinned `CODEX_HOME` | fresh home has no `auth.json` | export `OPENAI_API_KEY` (or seed `auth.json`) |
| Works for one task, fails intermittently under concurrency | global `os.environ` was mutated instead of the subprocess `env` | pass the token in the subprocess `env` only |
| Auth works today, breaks after a token refresh | OAuth-refresh-rename stranding on file-based creds | prefer the env token (immune to the rename) |
| Integration tests skip with an auth reason | no env token and no Keychain token available | run `claude setup-token`, or set an API key |

---

## Gotchas (the load-bearing list)

- **A relocated home does not inherit the interactive login.** This is the whole
  point of this doc. Verified live.
- **Env token, not the home dir, is the source of truth for auth.** macOS Keychain
  isolation misses (won't-fix upstream); the env token sidesteps Keychain scoping
  *and* OAuth-refresh-rename stranding.
- **Subprocess `env` only — never mutate global `os.environ`.** Concurrency-critical.
- **Never persist a token** into a file, lockfile, or the backup archive. `home_env`
  keeps tokens in memory only.
- **codex's `auth.json` is file-based** — it *would* travel if you copied the home,
  but we start each task home fresh, so inject `OPENAI_API_KEY`. Note `auth.json` also
  carries OAuth `tokens` + `auth_mode`; don't hand-edit it.
- **Keep the home path short** — a long `CODEX_HOME` overruns the macOS socket
  `SUN_LEN` (~104 chars). `data/workspaces/<user>/<task>/.codex` is fine; a deep temp
  path may not be.
- **Sub-tools/hooks may still write to `~/.claude`** — don't assume the relocated
  home is the only write target.

---

## See also

- `workspace/task_workspace.py` — `home_env`, the implementation.
- The per-provider workspace/session matrix — the key implication being
  "Auth ≠ home dir": pinning a task's home does not move the login.
