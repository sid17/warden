"""In-process streamable-HTTP MCP server for Codex custom-tool delivery.

Codex cannot consume the harness's in-proc ``CustomTool`` list directly (the way
Claude/OpenHarness do) — it is a subprocess. But codex DOES connect to a
streamable-HTTP MCP server by URL (``codex mcp add --url`` → ``[mcp_servers.<n>]
url = "…"``). This module runs that MCP server **in-process** (via the ``mcp``
package's ``FastMCP``) so the tool handlers stay in harness memory; codex reaches
it over localhost.

**Ungated by construction.** Codex MCP tool calls ride the
``mcpServer/elicitation/request`` approval path, which cannot carry a meaningful
per-call ``can_use_tool`` decision (verified in the phase-3 SDK-reality note). So
this delivery is UNGATED — it is only wired when the caller sets the explicit
``allow_ungated_custom_tools`` opt-in on ``CodexSdkSession``. This module itself
holds no gating; the adapter owns the opt-in gate and the loud warning.

Security: the server binds ``127.0.0.1`` ONLY (never ``0.0.0.0``) on an ephemeral
free port.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import socket
from typing import Any

from warden.seams.custom_tools import CustomTool

logger = logging.getLogger(__name__)

# JSON-schema primitive → Python annotation, used to build a real function
# signature so FastMCP advertises the CustomTool's input_schema to codex.
_JSON_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}

_HOST = "127.0.0.1"


def _free_port() -> int:
    """Grab an OS-assigned free localhost port (bind-then-close)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((_HOST, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _make_handler_fn(tool: CustomTool):
    """Build a wrapper whose SIGNATURE mirrors ``tool.input_schema`` so FastMCP
    derives the correct JSON schema, and whose body invokes the CustomTool's
    (sync-or-async) handler and returns text.
    """
    schema = tool.input_schema or {}
    properties: dict[str, Any] = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    params: list[inspect.Parameter] = []
    for prop_name, prop_schema in properties.items():
        py_type = _JSON_TO_PY.get((prop_schema or {}).get("type", "string"), str)
        default = inspect.Parameter.empty if prop_name in required else None
        annotation = py_type if prop_name in required else (py_type | None)
        params.append(
            inspect.Parameter(
                prop_name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=annotation,
            )
        )

    async def _wrapper(**kwargs: Any) -> str:
        result = tool.handler(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, str) else str(result)

    _wrapper.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    _wrapper.__name__ = tool.name
    _wrapper.__doc__ = tool.description
    return _wrapper


class CustomToolMcpServer:
    """Serve a ``list[CustomTool]`` over an in-process streamable-HTTP MCP server.

    ``start()`` binds a free localhost port, launches the server as a background
    asyncio task, waits until it is accepting connections, and returns the base
    URL (``http://127.0.0.1:{port}/mcp``) the adapter injects into the codex
    thread config. ``stop()`` cancels the server task.
    """

    def __init__(self, tools: list[CustomTool], *, server_name: str = "harness_custom"):
        self._tools = tools
        self._server_name = server_name
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self._port: int | None = None
        self.url: str | None = None

    def _build_server(self, port: int) -> Any:
        """Construct the FastMCP server and register each CustomTool."""
        from mcp.server.fastmcp import FastMCP

        server = FastMCP(self._server_name, host=_HOST, port=port)
        for tool in self._tools:
            server.add_tool(
                _make_handler_fn(tool),
                name=tool.name,
                description=tool.description,
            )
        return server

    async def _wait_accepting(self, port: int, timeout: float = 10.0) -> None:
        """Block until the server accepts a TCP connection (or fail loudly)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                _reader, writer = await asyncio.open_connection(_HOST, port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.05)
        raise RuntimeError(
            f"CustomToolMcpServer never came up on {_HOST}:{port} within {timeout}s"
        )

    async def start(self) -> str:
        """Launch the in-proc MCP server; return its base URL."""
        if self.url is not None:
            return self.url
        port = _free_port()
        self._port = port
        self._server = self._build_server(port)
        self._task = asyncio.create_task(self._server.run_streamable_http_async())
        await self._wait_accepting(port)
        self.url = f"http://{_HOST}:{port}/mcp"
        logger.info(
            "CustomToolMcpServer serving %d custom tool(s) at %s",
            len(self._tools), self.url,
        )
        return self.url

    async def stop(self) -> None:
        """Cancel the server task and shut it down cleanly."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # The uvicorn lifespan raises CancelledError on shutdown; that is
                # the expected teardown path, not a failure.
                pass
        self._task = None
        self._server = None
        self.url = None
        logger.info("CustomToolMcpServer stopped")
