"""g2.device -- device info and on-device settings.

    device      device identity / info queries
    settings    on-device settings read/write

The names that historically lived at ``g2.device`` are re-exported here so
``from g2.device import ...`` keeps working after the reorganization.
"""
from .device import *  # noqa: F401,F403
