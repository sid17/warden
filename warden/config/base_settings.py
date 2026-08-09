"""Behavioral base for every settings bundle in the runtime — self-contained, with
**zero** external/monorepo config dependencies.

:class:`HarnessBaseSettings` centralizes the pydantic-settings *behavioral flags*
(``extra="ignore"``, alias population, case-insensitivity), the ``.env`` overlays,
and the ``environment`` field + predicates, so every config slice inherits identical
loading semantics.

**The ``.env`` anchor is *discovered*, not hardcoded.** A fixed ``parents[N]`` walk
would break the moment the package moves (in-repo vs. shipped standalone, where the
project root differs). Instead we resolve the dotenv dir dynamically so it Just Works
in both worlds:

1. ``WARDEN_DOTENV_DIR`` env var — an explicit override (highest precedence).
2. otherwise the **nearest ancestor directory containing a ``.env``** (walking up
   from this file). In a nested checkout that is the outer project root (its single ``.env``);
   shipped standalone it is the harness project root.
3. otherwise the current working directory (fresh checkout with no ``.env`` yet —
   pydantic-settings tolerates a missing env_file, so config falls through to OS
   env + defaults, which is exactly what the hermetic tests and Docker rely on).

Environment selection is unchanged: ``WARDEN_ENV`` ∈ ``dev|test|staging|prod``
picks the overlay. Precedence (highest → lowest): OS/container env >
``.env.{WARDEN_ENV}`` > ``.env`` > field defaults (pydantic-settings' default
source order).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_WARDEN_ENV = os.getenv("WARDEN_ENV", "dev").strip().lower()

Environment = Literal["dev", "test", "staging", "prod"]


def _resolve_dotenv_dir() -> Path:
    """Locate the directory the ``.env`` overlays live in (see module docstring)."""
    override = os.getenv("WARDEN_DOTENV_DIR")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".env").is_file():
            return parent
    return Path.cwd()


_DOTENV_DIR = _resolve_dotenv_dir()


class HarnessBaseSettings(BaseSettings):
    """Behavioral base shared by every harness settings bundle. Not read directly."""

    model_config = SettingsConfigDict(
        env_file=(str(_DOTENV_DIR / ".env"), str(_DOTENV_DIR / f".env.{_WARDEN_ENV}")),
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,   # empty string in .env → fall through to default
        case_sensitive=False,
        populate_by_name=True,   # allow construction by field name (tests, Depends overrides)
    )

    # -- environment --------------------------------------------------------
    environment: Environment = Field(
        default=_WARDEN_ENV if _WARDEN_ENV in ("dev", "test", "staging", "prod") else "dev",
        validation_alias=AliasChoices("WARDEN_ENV", "ENVIRONMENT"),
    )

    def is_dev(self) -> bool:
        return self.environment == "dev"

    # Environment predicates.
    def is_development(self) -> bool:
        return self.environment == "dev"

    def is_staging(self) -> bool:
        return self.environment == "staging"

    def is_production(self) -> bool:
        return self.environment == "prod"

    def is_prod_like(self) -> bool:
        return self.environment in ("staging", "prod")
