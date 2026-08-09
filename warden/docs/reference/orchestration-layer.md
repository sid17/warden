# Orchestration Layer Guide

## What it does

The orchestration layer is the transport-agnostic core that sits between user-facing transports (WebSocket, CLI, Python API) and LLM provider sessions (Claude SDK, OpenHarness/Ollama, Codex). It owns the full message lifecycle: middleware processing, interaction-mode prefixing, tool scoping, permission enforcement, session creation/resume, and response streaming. Transports only handle protocol framing; all logic lives here.

## Architecture diagram

```
                       Transports
           +-------------------------------+
           |  WebSocket   CLI   ChatAPI    |
           |  handler           (Python)   |
           +------+--------+-------+------+
                  |        |       |
                  v        v       v
         +------------------------------------+
         |          Orchestrator              |
         |                                    |
         |  middleware -> prefix -> scope ->  |
         |  session lookup -> stream          |
         |------------------------------------|
         |  PermissionHandler (protocol)      |
         |  ToolScope (whitelist/blacklist)    |
         |  CustomTool[]                      |
         |  PermissionChecker (workflow YAML) |
         +------+--------+-------+-----------+
                |        |       |
                v        v       v
         +------------------------------------+
         |        SessionManager              |
         |   create / resume / register       |
         +------+--------+-------+-----------+
                |        |       |
                v        v       v
         +----------+ +----------+ +--------+
         | Claude   | | Open-    | | Codex  |
         | Session  | | Harness  | | Session|
         +----------+ +----------+ +--------+
```

## Key files

| File | What it does | When to read |
|------|-------------|-------------|
| `orchestrator/orchestrator.py` (`Orchestrator`) | Core engine: middleware, permissions, session lifecycle, streaming | First. This is the central file. |
| `schemas/events.py` (`OrchestratorEvent`) | Typed dataclasses yielded by the orchestrator | When adding a new event type |
| `drive/api.py` (`ChatAPI`) | Python-native wrapper over `Orchestrator` | When using orchestration from scripts/CLI |
| `drive/cli.py` | CLI drive path (`_collect_and_display`) | When changing the terminal transport |
| `seams/permissions.py` (`PermissionHandler`, `AutoAllowHandler`, `CLIPermissionHandler`) | `PermissionHandler` protocol + built-in handlers | When adding a new transport or changing permission UX |
| `schemas/tool_scope.py` (`ToolScope`) | Whitelist/blacklist tool restriction | When changing which tools a mode can access |
| `seams/custom_tools.py` (`CustomTool`) | `CustomTool` dataclass for user-defined tools | When registering a new tool |
| `seams/middleware.py` (`Middleware`, `SendContext`, `RejectResult`) | `Middleware` protocol + send context types | When adding pre-send message processing |
| `providers/__init__.py` (`create_session`) | Provider factory | When adding a new provider |
| `providers/claude/session.py` | Claude SDK wrapper | When debugging Claude-specific behavior |
| `providers/openharness/session.py` | OpenHarness/Ollama wrapper | When debugging local model behavior |
| `orchestrator/session/manager.py` (`SessionManager`) | Session create/resume/register/close lifecycle | When changing session persistence |

## Data flow

What happens when a transport (e.g. an app over WebSocket, or the CLI) sends a message:

1. **Transport receives the message** -- The transport (an app WebSocket adapter, or `drive/cli.py`) parses the incoming message and calls `orchestrator.send_message()`. Transports live app-side; only `drive/cli.py` ships in this package.

2. **Workflow loading** -- If a workflow name is provided, `orchestrator.py` loads it from YAML and hot-swaps the `PermissionChecker` if the workflow changed.

3. **Provider/model change detection** -- If the provider or model changed since the last message, the current session is closed (forces a fresh session with the new config).

4. **Middleware pipeline** -- Each middleware's `before_send()` runs in order. Any middleware can modify the content string or return `RejectResult` to abort early (`middleware.py`).

5. **Prompt assembly** -- The middleware-processed content is sent directly to the provider. The former interaction-mode prefix layer was removed — prompt framing is now app-side (see `../01-conceptual-model.md`, §2 "mechanism vs. policy").

6. **Tool scope resolution** -- The constructor-level `ToolScope` resolves the active scope. If scope changed, the session is closed (SDK `disallowed_tools` is immutable after creation).

7. **3-way session lookup** -- The orchestrator tries, in order: (a) client's `session_id` if active, (b) orchestrator's current session, (c) DB resume via `SessionManager.resume()`. Falls through to `SessionManager.create()` if none found.

8. **Streaming** -- `session.send(prompt)` yields raw SDK messages. A background task reads them, normalizes via the provider's `message_handler`, and pushes `OrchestratorEvent`s onto an `asyncio.Queue`. The generator in `send_message` drains the queue and `yield`s events.

9. **Permission checks** -- During streaming, the SDK calls `_can_use_tool()`. This checks tool scope (fast deny), then `PermissionChecker` (workflow YAML rules), then falls through to `PermissionHandler.request_permission()` for interactive confirmation.

10. **Transport sends events** -- The transport maps each `OrchestratorEvent` subclass (`schemas/events.py`) to its wire protocol. For the CLI this is `_collect_and_display()` in `drive/cli.py`; an app WebSocket adapter does the equivalent JSON mapping.

## Extension points

`ChatAPI` is config-driven: it takes a `HarnessConfig` and a `repo_path`, and
builds its seam objects (permission handler, tool scope, middleware, custom
tools, system prompt) from that config via `config/build.py`. You extend
behavior by populating the config, not by passing seam objects as constructor
kwargs.

```python
from warden.config import get_harness_config
from warden.drive.api import ChatAPI

api = ChatAPI(get_harness_config(), repo_path="/path/to/workspace")
await api.init()
```

### Add a custom tool

Custom tools are `CustomTool` objects carried on `config.custom_tools.tools`.

```python
from warden.seams.custom_tools import CustomTool

def my_tool_handler(query: str) -> str:
    return f"Result for {query}"

config.custom_tools.tools.append(
    CustomTool(
        name="my_tool",
        description="Does something useful",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=my_tool_handler,
    ),
)
```

### Add middleware

Implement the `Middleware` protocol, then register it on `config.middleware`
(input/output slots) so `build_middleware()` picks it up.

```python
from warden.seams.middleware import Middleware, SendContext, RejectResult

class LoggingMiddleware:
    async def before_send(self, content: str, context: SendContext) -> str | RejectResult:
        print(f"[{context.workflow}] {content[:80]}")
        return content  # pass through (or return RejectResult("reason") to block)
```

### Add a system prompt

The system prompt is declared on the safety slice: `config.safety.system_prompt`.

### Restrict tools per mode

Tool restriction is declared on the permissions slice
(`config.permissions.allowed_tools` / `denied_tools`) and built into a
`ToolScope` by `build_tool_scope()`.

```python
from warden.schemas.tool_scope import ToolScope

# Whitelist: only these tools allowed
scope = ToolScope(allowed=["Read", "Grep", "Glob"])

# Blacklist: everything except these
scope = ToolScope(denied=["Bash", "Write", "Edit"])
```

### Add a new provider

1. Create `providers/myprovider/session.py` implementing the `AgentProvider` protocol (see `providers/base_provider.py`) — `start`, `send`, `stop`, `close`, `session_id`, `jsonl_path`.
2. Add a `message_handler.py` that normalizes SDK messages to `OrchestratorEvent`s.
3. Register the provider in `providers/__init__.py`'s `create_session()` factory.

## Key design decisions

- **Transport-agnostic core.** The orchestrator knows nothing about WebSockets, HTTP, or CLI. Transports implement `PermissionHandler` to bridge user interaction. This lets the same logic power the browser, CLI, and Python API.

- **Session IDs are provider-owned.** Session IDs come from the SDK (Claude's `session_id`, Codex's `thread_id`), not generated upfront. The orchestrator captures them from the first streaming message and calls `SessionManager.register()`. This avoids ID mismatches with the SDK.

- **Tool scope forces session restart.** The SDK's `disallowed_tools` is set at session creation and cannot be changed. When tool scope changes (e.g., switching interaction modes), the session is closed and a new one created. This is intentional -- it's the only safe way.

- **Permission chain: scope -> checker -> handler.** Tool scope is checked first (fast deny, no I/O). Then `PermissionChecker` evaluates workflow YAML rules. Only if the checker says "needs confirmation" does the orchestrator call the `PermissionHandler` (which may block waiting for user input via WebSocket).

- **Middleware sees raw user content.** Middleware runs on the content the user actually typed, before it is handed to the provider. (The former interaction-mode prefix layer was removed; prompt framing is now app-side.)

- **Queue-based streaming.** A background `asyncio.Task` drives `session.send()` and pushes events onto a queue. The `send_message` generator drains the queue. This decouples the provider's streaming pace from the consumer and allows permission-check events to be interleaved.

- **Workflow hot-swap.** When the workflow name changes between messages, the `PermissionChecker` is rebuilt from the new workflow's YAML. This avoids restarting the session just for permission rule changes.
