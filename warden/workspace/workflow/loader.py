"""Workflow config loader with mtime-based caching.

Loads and parses .workflows/{name}.yaml into Workflow models,
reusing cached results when the file hasn't changed on disk.
"""

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from warden.workspace.workflow import Workflow

logger = logging.getLogger(__name__)

_cache: dict[tuple[Path, str], tuple[float, Workflow]] = {}

# EXT-P1/A2 (E4): the FIXED, closed set of event types an ``event_tool_map`` may
# target. Kept in sync with the wire ``EventType`` milestone subset
# (``harness_api/schemas.MILESTONE_EVENT_TYPES``); defined here to avoid a
# workspace→harness_api import cycle (workspace is the lower layer).
_MILESTONE_EVENT_TYPES = frozenset({"checkpoint", "completion", "milestone"})


def _validate_event_tool_map(event_tool_map: dict[str, str], wf_path: Path) -> None:
    """Every map VALUE must be a known milestone event type, else ``WorkflowLoadError``
    (fail-closed at load — the same discipline as ``compute_deny_baseline``)."""
    for tool_name, event_type in event_tool_map.items():
        if event_type not in _MILESTONE_EVENT_TYPES:
            raise WorkflowLoadError(
                f"Workflow {wf_path}: event_tool_map[{tool_name!r}] = "
                f"{event_type!r} is not a known event type "
                f"(allowed: {sorted(_MILESTONE_EVENT_TYPES)})"
            )


class WorkflowLoadError(Exception):
    """A workflow file exists on disk but could not be parsed or validated.

    Raised (fail-CLOSED) for present-but-broken workflow files — malformed
    YAML, a top-level value that isn't a mapping, or a schema-validation
    failure. Callers that compute security-relevant config (e.g.
    ``compute_deny_baseline``) MUST let this propagate so a broken deny-rule
    file hard-stops session creation instead of silently opening the agent up.

    A MISSING workflow file is NOT an error (not every workflow exists) and
    stays a ``None`` return — see ``load_workflow``.
    """


def load_workflow(repo_path: Path, workflow_name: str) -> Workflow | None:
    """Load a workflow YAML and return a Workflow model.

    Returns ``None`` when the file is MISSING (normal — not every workflow
    exists). Raises :class:`WorkflowLoadError` when the file is PRESENT but
    broken (unparseable YAML, not a mapping, or schema-invalid) so callers
    fail closed rather than silently dropping the workflow's rules.

    Uses mtime-based caching: returns the cached Workflow if the file
    hasn't been modified since the last load.
    """
    wf_path = repo_path / ".workflows" / f"{workflow_name}.yaml"
    if not wf_path.is_file():
        return None

    try:
        mtime = wf_path.stat().st_mtime
    except OSError as exc:
        # The file existed a moment ago (is_file() was True) but can't be
        # stat'd — treat as present-but-broken, not missing.
        logger.error("Cannot stat workflow file %s: %s", wf_path, exc)
        raise WorkflowLoadError(f"Cannot stat workflow file {wf_path}: {exc}") from exc

    cache_key = (repo_path, workflow_name)
    cached = _cache.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        data = yaml.safe_load(wf_path.read_text())
    except (yaml.YAMLError, OSError) as exc:
        logger.error("Failed to parse workflow YAML %s: %s", wf_path, exc)
        raise WorkflowLoadError(f"Malformed workflow YAML {wf_path}: {exc}") from exc

    if not isinstance(data, dict):
        logger.error(
            "Workflow YAML %s did not parse to a mapping (got %s)",
            wf_path,
            type(data).__name__,
        )
        raise WorkflowLoadError(
            f"Workflow YAML {wf_path} is not a mapping (got {type(data).__name__})"
        )

    try:
        wf = Workflow(
            name=data.get("name", workflow_name),
            description=data.get("description", ""),
            permissions=data.get("permissions"),
            middleware=data.get("middleware"),
            event_tool_map=data.get("event_tool_map") or {},
        )
    except ValidationError as exc:
        logger.error("Workflow %s failed schema validation: %s", wf_path, exc)
        raise WorkflowLoadError(
            f"Workflow {wf_path} failed schema validation: {exc}"
        ) from exc

    # EXT-P1/A2 (E4): validate every event_tool_map VALUE is a known milestone event
    # type, fail-closed at session creation (like compute_deny_baseline) — this makes
    # "maps only to known words" a hard guarantee, not a runtime convention.
    _validate_event_tool_map(wf.event_tool_map, wf_path)

    _cache[cache_key] = (mtime, wf)
    return wf


def compute_deny_baseline(repo_path: Path) -> list[str]:
    """Compute intersection of tool_access.deny across all workflows.

    Returns tools denied by EVERY workflow — safe to pass as
    disallowed_tools to the SDK at session creation. If any workflow
    has no deny list, the intersection is empty (conservative).

    Fails CLOSED: if any present workflow file is broken (malformed YAML,
    not a mapping, or schema-invalid), :func:`load_workflow` raises
    :class:`WorkflowLoadError`, which propagates here to hard-stop session
    creation. A broken deny-rule file must NOT be silently dropped.
    """
    wf_dir = repo_path / ".workflows"
    if not wf_dir.is_dir():
        return []

    yaml_files = [f for f in wf_dir.glob("*.yaml") if f.stem != "workspace"]
    if not yaml_files:
        return []

    # Load ALL workflows first so a broken file hard-stops regardless of
    # glob order — a no-deny workflow must not short-circuit before a broken
    # sibling is even parsed. Present-but-broken → load_workflow raises here.
    workflows: list[Workflow] = []
    for yaml_file in yaml_files:
        wf = load_workflow(repo_path, yaml_file.stem)
        if wf is None:
            # Missing file (raced away between glob and load) — a no-op, same
            # as an absent workflow. Present-but-broken raises above instead.
            continue
        workflows.append(wf)

    deny_sets: list[set[str]] = []
    for wf in workflows:
        if wf.permissions and wf.permissions.tool_access and wf.permissions.tool_access.deny:
            deny_sets.append(set(wf.permissions.tool_access.deny))
        else:
            return []

    if not deny_sets:
        return []

    return sorted(deny_sets[0].intersection(*deny_sets[1:]))


def clear_cache() -> None:
    """Clear the workflow cache (for testing)."""
    _cache.clear()
