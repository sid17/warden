"""Canary token detection for prompt leakage testing.

SAFE-4 (rung 3e-3): the CANARY backstop. Plant a synthetic token in the system
prompt (``plant_canary``) and detect it verbatim in output. The token is a benign
HTML comment the model shouldn't repeat, but which is byte-detectable if the
system prompt leaks verbatim at egress.
"""

from __future__ import annotations

_CANARY = "CANARY_7f3a8b2c"

# Public default token — tests may pin their own via ``canary_token`` config.
DEFAULT_CANARY = _CANARY


def check_canary(text: str) -> bool:
    """Returns True if the canary token leaked into output."""
    return _CANARY in text


def plant_canary(system_prompt: str | None, token: str) -> str:
    """Return a system prompt with ``token`` embedded as a benign marker line.

    Appends ``\\n\\n<!-- {token} -->`` — an HTML comment the model has no reason
    to repeat, yet which is verbatim-detectable at egress if the prompt leaks. A
    ``None`` base starts from an empty prompt (so the result is the marker alone).
    """
    base = system_prompt or ""
    return f"{base}\n\n<!-- {token} -->"
