"""Allow running as: python -m warden.drive.cli"""

import asyncio

from warden.drive.cli import main

asyncio.run(main())
