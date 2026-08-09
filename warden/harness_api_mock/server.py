"""uvicorn entrypoint for the mock harness service.

    python -m warden.harness_api_mock.server

Binds host/port from ``MockConfig`` (env-overridable). The app itself is built by
``app.create_app()`` (imported as ``app`` for ``uvicorn app:app`` too).
"""

from __future__ import annotations

import uvicorn

from warden.harness_api_mock.app import app
from warden.harness_api_mock.config import MockConfig


def main() -> None:
    config = MockConfig()
    uvicorn.run(app, host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
