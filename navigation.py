"""Navigation (service 0x08) — turn-by-turn directions on the glasses. DECODED, untested live.

Plain English: the phone does the routing. It opens the navigation screen on the glasses, then
sends a small "basic info" card on every progress update: an arrow (directionSignIndex), the
distance to the next turn, the road name, time/distance remaining, ETA and speed. It keeps the
screen alive with a heartbeat and closes it on arrival or stop. Every byte layout below was read
from the official app's decompile (blutter, `navigate_ble_service.dart` /
`heartbeat_controller.dart`); none of it has been seen working live yet.
Spec: esp32_ai/docs/research/native_apps_findings.md §4.

How a session works:

    app     → START (cmd 5)                opens the navigation screen; reply errorCode 0 = OK
    app     → BASIC_INFO (cmd 7) on every progress update (arrow, "200 m", "Main St", …)
    app     → HEARTBEAT (cmd 0) every 5 s   the app stops if none succeeds for 30 s
    app     → RECALCULATING (10) / ARRIVE (11) / STOP (12)
    glasses → NOTIFY_EXIT (13) or REQUEST_END_SESSION (20)   the user left; tear down

Launched from the glasses menu instead: glasses MENU_STARTUP (1) → app LOCATION_LIST (2)
favorites (or LOCATION_NONE (3) if there are none) → glasses LOCATION_SELECTED (4) → app routes
→ START → BASIC_INFO loop.

Every message is a `navigation_main_msg_ctx` protobuf:
    1 cmd          which command this is (CMD_* below); cmd 0 (heartbeat) is written explicitly
    2 magicRandom  request id; use a fresh one per packet
    3..12          exactly one sub-message; field 3 (extend_info, empty) for the simple commands

Strings are UTF-8 with NO NUL terminator, each cut to ≤60 bytes on a character boundary; a
missing value is sent as "--". Functions here only build or decode bytes. Mini/overview map
bitmaps (cmds 8/9) are not decoded and not implemented.
"""
from . import evenhub as eh

SID = 0x08

# cmd (Navigation_Cmd_list)
CMD_HEARTBEAT = 0                 # app→glasses
CMD_MENU_STARTUP = 1              # glasses→app: user opened Navigate from the menu
CMD_LOCATION_LIST = 2             # app→glasses: favorites
CMD_LOCATION_NONE = 3             # app→glasses: no favorites
CMD_LOCATION_SELECTED = 4         # glasses→app
CMD_START = 5                     # app→glasses
CMD_ERROR_INFO = 6                # app→glasses
CMD_BASIC_INFO = 7                # app→glasses
CMD_MINI_MAP = 8                  # app→glasses (not implemented)
CMD_MAX_MAP = 9                   # app→glasses (not implemented)
CMD_RECALCULATING = 10            # app→glasses
CMD_ARRIVE = 11                   # app→glasses (NAVIGATION_COMPLETE)
CMD_STOP = 12                     # app→glasses (REQUEST_EXIT)
CMD_NOTIFY_EXIT = 13              # glasses→app
CMD_VIEW_CHANGED = 14             # glasses→app
CMD_COMPASS_CHANGED = 15          # glasses→app
CMD_COMPASS_CALIBRATE_START = 16  # glasses→app
CMD_COMPASS_CALIBRATE_DONE = 17   # glasses→app
CMD_SWITCH_TRANSPORT_REQ = 18     # glasses→app
CMD_SWITCH_TRANSPORT_RSP = 19     # app→glasses
CMD_END_SESSION_REQ = 20          # glasses→app

CMD_NAMES = {CMD_HEARTBEAT: 'HEARTBEAT', CMD_MENU_STARTUP: 'MENU_STARTUP',
             CMD_LOCATION_LIST: 'LOCATION_LIST', CMD_LOCATION_NONE: 'LOCATION_NONE',
             CMD_LOCATION_SELECTED: 'LOCATION_SELECTED', CMD_START: 'START',
             CMD_ERROR_INFO: 'ERROR_INFO', CMD_BASIC_INFO: 'BASIC_INFO', CMD_MINI_MAP: 'MINI_MAP',
             CMD_MAX_MAP: 'MAX_MAP', CMD_RECALCULATING: 'RECALCULATING', CMD_ARRIVE: 'ARRIVE',
             CMD_STOP: 'STOP', CMD_NOTIFY_EXIT: 'NOTIFY_EXIT', CMD_VIEW_CHANGED: 'VIEW_CHANGED',
             CMD_COMPASS_CHANGED: 'COMPASS_CHANGED',
             CMD_COMPASS_CALIBRATE_START: 'COMPASS_CALIBRATE_START',
             CMD_COMPASS_CALIBRATE_DONE: 'COMPASS_CALIBRATE_DONE',
             CMD_SWITCH_TRANSPORT_REQ: 'SWITCH_TRANSPORT_REQ',
             CMD_SWITCH_TRANSPORT_RSP: 'SWITCH_TRANSPORT_RSP',
             CMD_END_SESSION_REQ: 'END_SESSION_REQ'}

# Package fields (chosen by message type, not by cmd)
F_EXT = 3            # extend_info_msg {1 errorCode, 2 errorMessage, 3 errorIndex}
F_LIST = 4           # LocationList_msg
F_INFO = 5           # basic_info_msg
F_MINI_MAP = 6
F_MAX_MAP = 7
F_SELECT = 8         # os_select_location_msg
F_VIEW = 9
F_COMPASS = 10
F_TRANSPORT_REQ = 11
F_TRANSPORT_RSP = 12

# Where each glasses message (and each reply echo) carries its sub-message, and the errorCode
# field inside it.
_SUB_FOR_CMD = {CMD_LOCATION_LIST: (F_LIST, 4), CMD_BASIC_INFO: (F_INFO, 9),
                CMD_LOCATION_SELECTED: (F_SELECT, 2), CMD_MINI_MAP: (F_MINI_MAP, 4),
                CMD_MAX_MAP: (F_MAX_MAP, 7), CMD_VIEW_CHANGED: (F_VIEW, None),
                CMD_COMPASS_CHANGED: (F_COMPASS, None),
                CMD_SWITCH_TRANSPORT_REQ: (F_TRANSPORT_REQ, None),
                CMD_SWITCH_TRANSPORT_RSP: (F_TRANSPORT_RSP, None)}

# eErrorCode
ERR_SUCCESS = 0
ERR_FAIL = 7
ERROR_NAMES = {0: 'SUCCESS', 1: 'CRC', 2: 'PKT_LOST', 3: 'TIMEOUT', 4: 'NO_RESOURCES',
               5: 'PB_ERROR', 6: 'NULL', 7: 'FAIL', 8: 'NOT_SUPPORT', 9: 'SUPPORT', 10: 'HEAD_ID',
               11: 'INVALID_LENGTH', 12: 'INVALID_SDID', 13: 'DUPLICATE_PACKET',
               90: 'RING_CONNECT_TIMEOUT'}

# navigateWorkMethod
METHOD_WALKING = 1
METHOD_CYCLING = 2
METHOD_NAMES = {METHOD_WALKING: 'WALKING', METHOD_CYCLING: 'CYCLING'}

# NavigateRoadSignType (maneuver) → directionSignIndex, the arrow the glasses draw.
# From the jump table at 0x1969abc. Note the odd tail: mergeUnspecified=34 and
# roundaboutSharpRightCCW=35, while the other roundabout/merge/fork entries run 11..33.
DIRECTION = {
    'DEPART': 1, 'STRAIGHT': 2, 'TURN_RIGHT': 3, 'TURN_LEFT': 4,
    'TURN_SLIGHT_RIGHT': 5, 'TURN_SLIGHT_LEFT': 6, 'TURN_SHARP_RIGHT': 7, 'TURN_SHARP_LEFT': 8,
    'UTURN_CLOCKWISE': 9, 'UTURN_COUNTERCLOCKWISE': 10,
    'ROUNDABOUT_SHARP_RIGHT_CW': 11,
    'ROUNDABOUT_RIGHT_CCW': 12, 'ROUNDABOUT_RIGHT_CW': 13,
    'ROUNDABOUT_SLIGHT_RIGHT_CCW': 14, 'ROUNDABOUT_SLIGHT_RIGHT_CW': 15,
    'ROUNDABOUT_STRAIGHT_CCW': 16, 'ROUNDABOUT_STRAIGHT_CW': 17,
    'ROUNDABOUT_SLIGHT_LEFT_CCW': 18, 'ROUNDABOUT_SLIGHT_LEFT_CW': 19,
    'ROUNDABOUT_LEFT_CCW': 20, 'ROUNDABOUT_LEFT_CW': 21,
    'ROUNDABOUT_SHARP_LEFT_CCW': 22, 'ROUNDABOUT_SHARP_LEFT_CW': 23,
    'ROUNDABOUT_UTURN_CCW': 24, 'ROUNDABOUT_UTURN_CW': 25,
    'ROUNDABOUT_CCW': 26, 'ROUNDABOUT_CW': 27,
    'ROUNDABOUT_EXIT_CCW': 28, 'ROUNDABOUT_EXIT_CW': 29,
    'MERGE_RIGHT': 30, 'MERGE_LEFT': 31, 'FORK_RIGHT': 32, 'FORK_LEFT': 33,
    'MERGE_UNSPECIFIED': 34, 'ROUNDABOUT_SHARP_RIGHT_CCW': 35,
}
DIRECTION_NAMES = {v: k for k, v in DIRECTION.items()}
# Short aliases accepted by direction_index() (the hub UI sends 'left' / 'right').
DIRECTION_ALIASES = {'RIGHT': 'TURN_RIGHT', 'LEFT': 'TURN_LEFT', 'SLIGHT_RIGHT': 'TURN_SLIGHT_RIGHT',
                     'SLIGHT_LEFT': 'TURN_SLIGHT_LEFT', 'UTURN': 'UTURN_COUNTERCLOCKWISE'}
TURN_RIGHT = DIRECTION['TURN_RIGHT']
TURN_LEFT = DIRECTION['TURN_LEFT']
STRAIGHT = DIRECTION['STRAIGHT']
DEPART = DIRECTION['DEPART']

TEXT_MAX_BYTES = 60
MISSING = '--'
HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_GIVE_UP_S = 30.0


def build_package(cmd, magic, field, inner):
    """Wrap a sub-message in navigation_main_msg_ctx. cmd is always written, even 0."""
    return (eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(field, inner))


def truncate_utf8(text, limit=TEXT_MAX_BYTES):
    """Cut `text` to ≤limit UTF-8 bytes without splitting a character (like utf8SafeTruncate).
    None or blank → MISSING ("--"). Returns bytes."""
    if text is None or not str(text).strip():
        return MISSING.encode('utf-8')
    b = str(text).encode('utf-8')[:limit]
    return b.decode('utf-8', errors='ignore').encode('utf-8')


def _simple(cmd, magic):
    return build_package(cmd, magic, F_EXT, b'')


def build_start(magic):
    """START {3:{}}: open the navigation screen. STATUS: DECODED (sendNavigationStart), untested live."""
    return _simple(CMD_START, magic)


def build_heartbeat(magic):
    """HEARTBEAT (cmd 0) {3:{}}: every HEARTBEAT_INTERVAL_S. STATUS: DECODED, untested live."""
    return _simple(CMD_HEARTBEAT, magic)


def build_recalculating(magic):
    """RECALCULATING (cmd 10) {3:{}}: rerouting. STATUS: DECODED, untested live."""
    return _simple(CMD_RECALCULATING, magic)


def build_arrive(magic):
    """ARRIVE (cmd 11) {3:{}}: destination reached. STATUS: DECODED, untested live."""
    return _simple(CMD_ARRIVE, magic)


def build_stop(magic):
    """STOP (cmd 12) {3:{}}: close navigation. STATUS: DECODED, untested live."""
    return _simple(CMD_STOP, magic)


def build_location_none(magic):
    """LOCATION_NONE (cmd 3): answer to MENU_STARTUP when there are no favorites. The payload is
    not documented; {3:{}} is INFERRED from the other simple commands. STATUS: untested."""
    return _simple(CMD_LOCATION_NONE, magic)


def build_error_info(magic, message, index=0, error=ERR_FAIL):
    """ERROR_INFO (cmd 6) {3:{1 errorCode, 2 errorMessage, 3 errorIndex}}.
    STATUS: DECODED, untested live."""
    inner = (eh.encode_varint_field(1, error) + eh.encode_bytes_field(2, truncate_utf8(message)) +
             (eh.encode_varint_field(3, index) if index else b''))
    return build_package(CMD_ERROR_INFO, magic, F_EXT, inner)


def direction_index(direction):
    """Accept a directionSignIndex (int) or a DIRECTION name ('TURN_RIGHT', 'turn right', …)."""
    if isinstance(direction, int):
        return direction
    key = str(direction).strip().upper().replace(' ', '_').replace('-', '_')
    return DIRECTION[DIRECTION_ALIASES.get(key, key)]


def build_basic_info(magic, direction, distance=None, road=None, remaining_time=None,
                     remaining_distance=None, eta=None, speed=None, method=METHOD_WALKING,
                     counter=0):
    """BASIC_INFO (cmd 7) {5:{1 directionSignIndex, 2 distance, 3 roadName, 4 spendTime,
    5 remainDistance, 6 etaTime, 7 speed, 8 navigateWorkMethod, 10 basicInfoIndex}}.
    All text is pre-formatted by the caller ("200 m", "5 min", "1.2 km", "5 km/h"); `eta` is the
    time ("14:35") and gets the "ETA: " prefix unless it already has one. Missing text → "--".
    `counter` is a running index reset when the route starts.
    STATUS: DECODED (sendNavigationBasicInfo), untested live."""
    if eta is not None and str(eta).strip() and not str(eta).startswith('ETA'):
        eta = f'ETA: {eta}'
    inner = eh.encode_varint_field(1, direction_index(direction))
    for field, text in ((2, distance), (3, road), (4, remaining_time), (5, remaining_distance),
                        (6, eta), (7, speed)):
        inner += eh.encode_bytes_field(field, truncate_utf8(text))
    if method:
        inner += eh.encode_varint_field(8, method)
    if counter:
        inner += eh.encode_varint_field(10, counter)
    return build_package(CMD_BASIC_INFO, magic, F_INFO, inner)


def build_location_list(magic, names, icons=None):
    """LOCATION_LIST (cmd 2) {4:{1 listNum, 2 locationName[], 3 iconIndex[]}}: favorites, the
    answer to MENU_STARTUP. Icons are written packed (proto3 default for repeated uint32;
    INFERRED). STATUS: DECODED, untested live."""
    inner = eh.encode_varint_field(1, len(names))
    for name in names:
        inner += eh.encode_bytes_field(2, truncate_utf8(name))
    if icons:
        inner += eh.encode_bytes_field(3, b''.join(eh.encode_varint(i) for i in icons))
    return build_package(CMD_LOCATION_LIST, magic, F_LIST, inner)


def build_switch_transport_response(magic, status=0):
    """SWITCH_TRANSPORT_RSP (cmd 19) {12:{1 status}}: answer to SWITCH_TRANSPORT_REQ (18).
    STATUS: DECODED from the schema, untested live."""
    inner = eh.encode_varint_field(1, status) if status else b''
    return build_package(CMD_SWITCH_TRANSPORT_RSP, magic, F_TRANSPORT_RSP, inner)


def decode(pb):
    """Decode a Navigation message from the glasses (event or reply echo) into a dict:
    {cmd, name, magic, value, location, method, error, label}. `value` is the view/compass index
    or the chosen location's index; `location` its name; `method` the transport method.
    STATUS: DECODED, untested live."""
    cmd = eh.read_varint_field(pb, 1, 0)       # cmd 0 may be omitted (proto3 default)
    magic = eh.read_varint_field(pb, 2, -1)
    name = CMD_NAMES.get(cmd, f'cmd={cmd}')
    out = {'cmd': cmd, 'name': name, 'magic': magic, 'value': None, 'location': None,
           'method': None, 'error': None}
    label = name
    field, err_field = _SUB_FOR_CMD.get(cmd, (F_EXT, 1))
    sub = eh.read_bytes_field(pb, field)
    if sub is not None:
        if cmd == CMD_LOCATION_SELECTED:
            out['location'] = eh.read_string_field(sub, 1, '')
            out['value'] = eh.read_varint_field(sub, 3, 0)
            out['method'] = eh.read_varint_field(sub, 4, 0)
            label += f' #{out["value"]} {out["location"]!r} {METHOD_NAMES.get(out["method"], out["method"])}'
        elif cmd in (CMD_VIEW_CHANGED, CMD_COMPASS_CHANGED):
            out['value'] = eh.read_varint_field(sub, 1, 0)
            label += f' {out["value"]}'
        elif cmd == CMD_SWITCH_TRANSPORT_REQ:
            out['method'] = eh.read_varint_field(sub, 1, 0)
            label += f' {METHOD_NAMES.get(out["method"], out["method"])}'
        if err_field:
            out['error'] = eh.read_varint_field(sub, err_field, 0)
            if out['error']:
                label += f' errorCode={ERROR_NAMES.get(out["error"], out["error"])}'
    out['label'] = label
    return out


def is_exit(msg):
    """True when a decode() result means the glasses left navigation (cmd 13 or 20)."""
    return msg['cmd'] in (CMD_NOTIFY_EXIT, CMD_END_SESSION_REQ)
