"""Experiment preset configurations.

Each preset is a dict wiring system_prompt, allowed_tools, custom_tools,
middleware, mode, and experiment-specific flags (keys starting with _).
"""

from warden.safety.experiments.prompts import (
    _ASK_SYSTEM_PROMPT,
    _E1_SYSTEM_PROMPT,
    _E15_SYSTEM_PROMPT,
    _E6_SYSTEM_PROMPT,
    _NOTE_SYSTEM_PROMPT,
)
from warden.safety.experiments.tools import SAFE_READ_TOOL, SAVE_NOTE_TOOL
from warden.safety.middleware.input.sanitize import (
    E3ExpandedMiddleware,
    SanitizeMiddleware,
)

EXPERIMENT_PRESETS: dict[str, dict] = {
    # --- Base presets ---
    "unrestricted": {
        "system_prompt": None,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "free",
    },
    "ask-only": {
        "system_prompt": _ASK_SYSTEM_PROMPT,
        "allowed_tools": ["Read", "Grep", "Glob"],
        "custom_tools": None,
        "middleware": None,
        "mode": "ask",
    },
    "note-taking": {
        "system_prompt": _NOTE_SYSTEM_PROMPT,
        "allowed_tools": ["Read", "Grep", "Glob", "save-note"],
        "custom_tools": [SAVE_NOTE_TOOL],
        "middleware": None,
        "mode": "note",
    },
    "prompt-guard": {
        "system_prompt": None,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": [SanitizeMiddleware()],
        "mode": "free",
    },
    "layered": {
        "system_prompt": _ASK_SYSTEM_PROMPT,
        "allowed_tools": ["Read", "Grep", "Glob"],
        "custom_tools": None,
        "middleware": [SanitizeMiddleware()],
        "mode": "ask",
    },
    # --- Round 1 experiments ---
    "e1-system-prompt": {
        "system_prompt": _E1_SYSTEM_PROMPT,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "ask",
    },
    "e2-tool-restriction": {
        "system_prompt": None,
        "allowed_tools": ["Read", "Grep", "Glob"],
        "custom_tools": None,
        "middleware": None,
        "mode": "ask",
    },
    "e3-input-middleware": {
        "system_prompt": None,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": [E3ExpandedMiddleware()],
        "mode": "free",
    },
    # --- Round 2 experiments ---
    "e1b-append-prompt": {
        "system_prompt": {"type": "preset", "preset": "claude_code", "append": _E1_SYSTEM_PROMPT},
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "ask",
    },
    "e2b-path-restricted": {
        # Same as e2 — path restriction via can_use_tool is impossible (SDK limitation).
        "system_prompt": None,
        "allowed_tools": ["Read", "Grep", "Glob"],
        "custom_tools": None,
        "middleware": None,
        "mode": "ask",
    },
    "e5a-output-filter": {
        "system_prompt": None,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "free",
        "_output_filter": True,
    },
    # --- Round 3 experiments ---
    "e6-structural-absence": {
        "system_prompt": _E6_SYSTEM_PROMPT,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "ask",
    },
    "e7-generic-deny": {
        "system_prompt": None,
        "allowed_tools": ["Read", "Grep", "Glob"],
        "custom_tools": None,
        "middleware": None,
        "mode": "ask",
        "_generic_deny": True,
    },
    "e8-post-sanitize": {
        "system_prompt": None,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "free",
        "_output_sanitize": True,
    },
    "e9-intent-classifier": {
        "system_prompt": None,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "free",
        "_intent_classifier": True,
    },
    # --- Round 4 experiments ---
    "e11-streaming-filter": {
        "system_prompt": None,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "free",
        "_streaming_filter": True,
    },
    "e12-fuzzy-classifier": {
        "system_prompt": None,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "free",
        "_fuzzy_classifier": True,
    },
    "e13-custom-read": {
        "system_prompt": None,
        "allowed_tools": ["Grep", "Glob", "safe-read"],
        "custom_tools": [SAFE_READ_TOOL],
        "middleware": None,
        "mode": "ask",
    },
    "e15-canary-token": {
        "system_prompt": _E15_SYSTEM_PROMPT,
        "allowed_tools": None,
        "custom_tools": None,
        "middleware": None,
        "mode": "ask",
        "_canary_check": True,
    },
}
