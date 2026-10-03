"""Service IDs (SIDs) — which on-glasses feature a message is for.

Plain English: every BLE frame to or from the glasses carries a one-byte service ID in its
header (byte 6, see g2.evenhub.frame_pb). It routes the payload to one subsystem, the way a port
number routes network traffic to one program. Example: a frame with byte 6 = 0x07 is for Even AI,
0x01 is for the native dashboard, 0x0c is for the task list.

The full table comes from the official app's `service_id_def` enum (blutter decompile) and is
listed in esp32_ai/docs/research/unknowns_findings.md §3. SID 0x0d (app sync) reports which app
is in the foreground using these same numbers (e.g. foreground=7 means Even AI is open).

This module holds only constants plus name(); nothing here talks to the glasses. Feature modules
keep their own copy of the SID they use (e.g. dashboard.SID_DASHBOARD == sids.DASHBOARD).
"""

DASHBOARD = 0x01
MENU = 0x03
NOTIFICATION = 0x04
TRANSLATE = 0x05
TELEPROMPTER = 0x06
EVEN_AI = 0x07
NAVIGATION = 0x08
SETTINGS = 0x09
TRANSCRIBE = 0x0A
CONVERSATE = 0x0B
QUICKLIST = 0x0C
APP_SYNC = 0x0D
HEALTH = 0x0E
LOGGER = 0x0F
ONBOARDING = 0x10
TRACEPOINT = 0x11
DASHBOARD_EXT = 0x1F      # watchface layout
MODULE_CONFIG = 0x20
TERMINAL = 0x30
DEVICE_CONFIG = 0x80      # also auth/bonding
CASE = 0x81
RING = 0x90
RING_2 = 0x91
OTA_FIRST, OTA_LAST = 0xC0, 0xC3
FILE_CMD = 0xC4           # file service: START / DATA prefix / RESULT_CHECK
FILE_DATA = 0xC5          # file service: raw file bytes
FILE_EXPORT_CMD = 0xC6    # glasses → app file export
FILE_EXPORT_DATA = 0xC7
EVENHUB = 0xE0            # custom pages (text/list/image containers) + session heartbeat

# Human-readable name for every known SID (used for logs, e.g. by the hub's frame tap).
NAMES = {DASHBOARD: 'Dashboard', MENU: 'Menu', NOTIFICATION: 'Notification', TRANSLATE: 'Translate',
         TELEPROMPTER: 'Teleprompter', EVEN_AI: 'Even AI', NAVIGATION: 'Navigation',
         SETTINGS: 'Settings', TRANSCRIBE: 'Transcribe', CONVERSATE: 'Conversate',
         QUICKLIST: 'Quicklist', APP_SYNC: 'App sync', HEALTH: 'Health', LOGGER: 'Logger',
         ONBOARDING: 'Onboarding', TRACEPOINT: 'Tracepoint', DASHBOARD_EXT: 'Watchface layout',
         MODULE_CONFIG: 'Module config', TERMINAL: 'Terminal', DEVICE_CONFIG: 'Device config',
         CASE: 'Case', RING: 'Ring', RING_2: 'Ring', FILE_CMD: 'File (cmd)',
         FILE_DATA: 'File (data)', FILE_EXPORT_CMD: 'File export (cmd)',
         FILE_EXPORT_DATA: 'File export (data)', EVENHUB: 'EvenHub'}
NAMES.update({sid: 'OTA' for sid in range(OTA_FIRST, OTA_LAST + 1)})


def name(sid):
    """Human name for a SID, e.g. name(0x07) → 'Even AI'; unknown ids → '0x2a'."""
    return NAMES.get(sid, f'0x{sid:02x}')
