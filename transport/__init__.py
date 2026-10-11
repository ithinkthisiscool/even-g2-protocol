"""g2.transport -- BLE transport layer.

Low-level connectivity for the G2 glasses:
    transport      BLE characteristic UUIDs + auth handshake
    ble            lens discovery and connection helpers (requires ``bleak``)
    session        GlassesSession: connect both lenses, auth, heartbeat, send/receive
    file_service   EFS file-transfer frame builders

The names that historically lived at ``g2.transport`` are re-exported here so
``from g2.transport import ...`` keeps working after the reorganization.
"""
from .transport import *  # noqa: F401,F403
