"""g2 -- a from-scratch Python library for Even Realities G2 smart glasses over Bluetooth LE.

Layers (each only imports the ones above it):
    transport     BLE characteristic UUIDs + auth handshake
    evenhub       protobuf field encoding, frame format, EvenHub pages + heartbeat
    session       GlassesSession: connect both lenses, auth, heartbeat, send/receive
    sids          service-id table (constants only)
    dashboard, menu, quicklist, file_service, even_ai   one module per glasses feature

Start with docs/g2-library.md (API reference) and docs/protocol.md (wire format).

Package layout
--------------
The source is organized into subpackages, but every module is re-exported here
so the historical flat import surface keeps working unchanged::

    g2/
      sids.py            core service-id constants (stays top-level)
      transport/         transport, ble, session, file_service
      apps/              translate, teleprompter, navigation, even_ai, menu, quicklist
      widgets/           evenhub, dashboard, watchface, stocks, health
      device/            device, settings

Both styles resolve to the same modules::

    from g2 import translate, evenhub, session      # top-level names
    from g2.session import GlassesSession            # old dotted path
"""

import sys as _sys

# Core constants -- stays at the top level, imported from everywhere.
from . import sids

# Framing + on-glasses widgets (base layer: evenhub depends on nothing internal).
from .widgets import evenhub, dashboard, watchface, stocks, health

# Transport layer.
from .transport import transport, ble, session, file_service

# Higher-level features.
from .apps import translate, teleprompter, navigation, even_ai, menu, quicklist

# Device info + settings.
from .device import device, settings

# ---------------------------------------------------------------------------
# Backward-compatible dotted paths.
#
# After the move the real modules live under g2.transport.*, g2.apps.*,
# g2.widgets.* and g2.device.*. Register the old top-level paths in
# sys.modules so pre-existing imports like ``from g2.session import X`` and
# ``import g2.evenhub`` keep resolving to the exact same module objects.
#
# ``transport`` and ``device`` are intentionally NOT aliased here: those names
# are real subpackages, and their inner same-named modules (transport.py /
# device.py) are re-exported from the subpackage __init__ instead.
# ---------------------------------------------------------------------------
for _name, _mod in {
    "ble": ble,
    "session": session,
    "file_service": file_service,
    "translate": translate,
    "teleprompter": teleprompter,
    "navigation": navigation,
    "even_ai": even_ai,
    "menu": menu,
    "quicklist": quicklist,
    "evenhub": evenhub,
    "dashboard": dashboard,
    "watchface": watchface,
    "stocks": stocks,
    "health": health,
    "settings": settings,
}.items():
    _sys.modules.setdefault(f"{__name__}.{_name}", _mod)

del _sys, _name, _mod

__all__ = [
    "sids",
    "transport",
    "ble",
    "session",
    "file_service",
    "evenhub",
    "dashboard",
    "watchface",
    "stocks",
    "health",
    "translate",
    "teleprompter",
    "navigation",
    "even_ai",
    "menu",
    "quicklist",
    "device",
    "settings",
]
