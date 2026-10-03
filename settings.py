"""Glasses settings: brightness, head-up, wear detection, silent mode, units, gestures (SID 0x09),
plus screen-off time and UI language (SID 0x20 "module configure").

Plain English: the glasses keep their own settings. The phone changes one by sending a small
"set" message, and can ask for the current values with a "read" message; the glasses also push
the same read-back message by themselves whenever something changes (display wakes, charging
starts, ...). decode() turns that read-back into a dict like
    {'battery': 100, 'charging': 0, 'brightness_auto': 1, 'head_up_angle': 30, 'wear_detection': 1,
     'silent_mode': 0, 'left_version': '2.3.0.24', ...}
which is how the hub learns the glasses' current state.

SID 0x09 messages are `G2SettingPackage` protobufs:
    1 commandId   1 set (DeviceReceiveInfo)  2 read (DeviceReceiveRequest)
                  3 glasses→app event        4/5 package acks
    2 magicRandom request id; replies echo it (pushes carry the glasses' own counter)
    3 set:  DeviceReceiveInfoFromAPP  — exactly one sub-message per write:
            1 brightness{1 autoAdjust, 2 level, 3 leftCal, 4 rightCal}   2 Y{1 level}   3 X{1 level}
            4 headUp{1 switch, 2 angle, 3 calibSwitch, 4 calib}   5 wear{1}   6 silent{1}
            7 appPage{1}   8 advanced{1 killAllFeature}   9 universe{1 unitFormat, 2 distanceUnit,
            3 timeFormat, 4 dateFormat, 5 temperatureUnit}   10 gestures{1 repeated {1 screenOn,
            2 operationType, 3 apptype}}   11 dominantHand{1 hand, 2 ringMac}   12 control{1 turnOn}
    4 read: DeviceReceiveRequestFromAPP{1 settingInfoType} → reply fills 2..19 (READBACK_FIELDS)
The glasses ack a set by echoing the sub-message with its zero fields left out (CAPTURED: units
{0,0,1,0,1} is acked as {3:1, 5:1}). The official app writes explicitly-set zeros (08 00); so do we.

SID 0x20 messages are `module_configure_main_msg_ctx{1 cmd, 2 magic, 3 system{1 languageIndex},
4 dashboard{1 autoCloseValue}}`: cmd 0 language, cmd 1 read screen-off time, cmd 2 set it.

Status legend (per function): LIVE = worked from our hub; CAPTURED = byte-identical to the
official app's traffic (golden tests in tests/test_g2_device_settings.py); DECODED = from the
decompiled app only, never sent. Every builder validates its input and raises ValueError.

Functions only build or decode bytes. Frame with evenhub.frame_pb(pb, SID, evenhub.FLAG_REQUEST,
seq) (or SID_MODULE for 0x20) and send to the right lens.
"""
from . import evenhub as eh

SID = 0x09          # G2SettingPackage
SID_MODULE = 0x20   # module_configure_main_msg_ctx

# g2_settingCommandId
CMD_SET = 1
CMD_READ = 2
CMD_DEVICE_EVENT = 3
CMD_DEVICE_ACK = 4
CMD_APP_ACK = 5
CMD_NAMES = {CMD_SET: 'SET', CMD_READ: 'READ', CMD_DEVICE_EVENT: 'DEVICE_EVENT',
             CMD_DEVICE_ACK: 'DEVICE_ACK', CMD_APP_ACK: 'APP_ACK'}

# APPRequestSettingType (DeviceReceiveRequestFromAPP.settingInfoType)
READ_BRIGHTNESS = 0
READ_BASIC = 1

# DeviceReceiveInfoFromAPP field numbers (which sub-message a set carries)
F_BRIGHTNESS, F_Y, F_X, F_HEAD_UP, F_WEAR, F_SILENT = 1, 2, 3, 4, 5, 6
F_APP_PAGE, F_ADVANCED, F_UNITS, F_GESTURES, F_HAND, F_CONTROL = 7, 8, 9, 10, 11, 12
SET_NAMES = {F_BRIGHTNESS: 'brightness', F_Y: 'display_height', F_X: 'display_distance',
             F_HEAD_UP: 'head_up', F_WEAR: 'wear_detection', F_SILENT: 'silent_mode',
             F_APP_PAGE: 'app_page', F_ADVANCED: 'kill_all_features', F_UNITS: 'units',
             F_GESTURES: 'gestures', F_HAND: 'dominant_hand', F_CONTROL: 'turn_on_device'}

BRIGHTNESS_MIN, BRIGHTNESS_MAX = 1, 50      # app slider + clamp(1, 50) in setBrightness
HEAD_UP_ANGLE_MIN, HEAD_UP_ANGLE_MAX = 0, 60   # g2_setting_push_guard.isDirty clamp (degrees)
DISPLAY_LEVEL_MAX = 20   # Y/X range was not recovered (capture reads Y = 6); this is only a guard
ENUM_MAX = 15            # guard for small enums whose meaning is unknown (units, hand, gestures)

# Units the official app sends on every connect (CAPTURED): {1:0, 2:0, 3:1, 4:0, 5:1}.
# Meanings by field (app unit_service, resolved 2026-10-01; docs/status.md): field 1 distance
# 0 km / 1 mi; field 2 health units 0 metric / 1 imperial (so the names of fields 1/2 below may be
# swapped); field 3 time 0 = 24 h, 1 = 12 h; field 4 date 0 yyyyMMdd / 1 MMddyyyy / 2 ddMMyyyy;
# field 5 temperature 1 = °C, 2 = °F.
UNITS_APP_DEFAULT = dict(unit_format=0, distance_unit=0, time_format=1, date_format=0,
                         temperature_unit=1)

# Gesture-control list the official app sends on every connect (CAPTURED): (screenOn,
# operationType, apptype). Meaning of each entry is unknown; apptype 0 = app_unable, 1 = app_menu.
GESTURES_APP_DEFAULT = ((0, 0, 0), (0, 1, 0), (0, 2, 0))

# Read-back (DeviceReceiveRequestFromAPP) field → dict key. Values in the capture in brackets.
READBACK_FIELDS = {
    1: 'setting_type',          # echoed request type (absent in unprompted pushes) [1]
    2: 'brightness_level',      # autoBrightnessLevel; INFERRED 0–100 scale (app halves it) [100]
    3: 'display_height',        # yCoordinateLevelRestored [6]
    4: 'display_distance',      # xCoordinateLevelRestored [0]
    5: 'left_version',          # string [2.3.0.24]
    6: 'right_version',         # string [2.3.0.24]
    7: 'head_up',               # headUpSwitchRestored [0]
    8: 'head_up_angle',         # degrees [30]
    9: 'head_up_calibration',
    10: 'wear_detection',       # [1]
    11: 'running_status',       # deviceRunningStatus; 1 while the display is awake (pushes)
    12: 'battery',              # % [100]
    13: 'charging',             # [0; 1 in pushes while charging]
    14: 'silent_mode',
    15: 'left_calibration',
    16: 'right_calibration',
    17: 'head_up_recalibration_ok',
    18: 'brightness_auto',      # autoBrightnessSwitchRestored [1]
    19: 'unread_count',         # [1]
}
_STRING_FIELDS = (5, 6)

# ── SID 0x20 module configure ────────────────────────────────────────────────────────────────
MOD_CMD_LANGUAGE = 0
MOD_CMD_READ_TIMEOUT = 1
MOD_CMD_SET_TIMEOUT = 2
MOD_CMD_NAMES = {MOD_CMD_LANGUAGE: 'SYSTEM_GENERAL_SETTING', MOD_CMD_READ_TIMEOUT: 'INQUIRE_AUTO_CLOSE',
                 MOD_CMD_SET_TIMEOUT: 'SET_AUTO_CLOSE'}

TIMEOUT_NEVER = 65365   # 0xff55; the app shows any value > 20 as "never"
# UI options 3/5/10/15 s + never; 30 is the value a fresh pair reports, so it is accepted too.
TIMEOUT_CHOICES = (3, 5, 10, 15, 30, TIMEOUT_NEVER)

# glasses_language_sync.dart ConstMap (CONFIRMED in the decompile)
LANGUAGES = {0: 'English', 1: 'German', 2: 'French', 3: 'Spanish', 4: 'Italian', 5: 'Japanese',
             6: 'Chinese (Traditional)', 7: 'Chinese (Simplified)', 8: 'Korean'}


# ── helpers ───────────────────────────────────────────────────────────────────────────────────

def _flag(value, what):
    """Accept bool or 0/1 and return 0/1."""
    if isinstance(value, bool) or value in (0, 1):
        return int(value)
    raise ValueError(f'{what} must be True/False or 0/1, got {value!r}')


def _int_in(value, lo, hi, what):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f'{what} must be an integer {lo}..{hi}, got {value!r}')
    return value


def build_set(magic, field, inner):
    """G2SettingPackage cmd 1 {3:{<field>: inner}}: the envelope for every setting write."""
    return (eh.encode_varint_field(1, CMD_SET) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(3, eh.encode_message_field(field, inner)))


def _module(cmd, magic, field, inner):
    return (eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(field, inner))


# ── SID 0x09 builders ─────────────────────────────────────────────────────────────────────────

def build_read_settings(magic, setting_type=READ_BASIC):
    """cmd 2 {4:{1 settingInfoType}}: ask for the current settings. CAPTURED (READ_BASIC; the app
    sends it on every connect). Reply → decode(). READ_BRIGHTNESS (0) is DECODED only."""
    _int_in(setting_type, 0, 1, 'setting_type')
    return (eh.encode_varint_field(1, CMD_READ) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(4, eh.encode_varint_field(1, setting_type)))


def build_brightness(magic, level):
    """Brightness level {3:{1:{2 level}}}, level 1–50 (the app's slider range). DECODED
    (matches the decompile's hex example; not in a capture). Does not change the auto switch."""
    _int_in(level, BRIGHTNESS_MIN, BRIGHTNESS_MAX, 'brightness level')
    return build_set(magic, F_BRIGHTNESS, eh.encode_varint_field(2, level))


def build_auto_brightness(magic, on):
    """Auto brightness {3:{1:{1 autoAdjust}}}, sent alone (no level). DECODED."""
    return build_set(magic, F_BRIGHTNESS, eh.encode_varint_field(1, _flag(on, 'on')))


def build_brightness_calibration(magic, left, right):
    """Per-lens brightness calibration {3:{1:{3 left, 4 right}}}. DECODED; range unknown (0–255
    accepted)."""
    _int_in(left, 0, 255, 'left')
    _int_in(right, 0, 255, 'right')
    return build_set(magic, F_BRIGHTNESS, eh.encode_varint_field(3, left) +
                     eh.encode_varint_field(4, right))


def build_silent_mode(magic, on):
    """Silent mode / do-not-disturb {3:{6:{1 on}}}. DECODED. Turning it on ends the running
    feature; the user can also toggle it on the glasses (long-press both touchpads)."""
    return build_set(magic, F_SILENT, eh.encode_varint_field(1, _flag(on, 'on')))


def build_wear_detection(magic, on):
    """Wear detection {3:{5:{1 on}}}. Off = the display also works when the glasses are not worn.
    DECODED."""
    return build_set(magic, F_WEAR, eh.encode_varint_field(1, _flag(on, 'on')))


def build_head_up(magic, enabled, angle=None):
    """Head-up (tilt your head up to show the dashboard) {3:{4:{1 switch, 2 angle}}}.
    angle 0–60 degrees (the capture's read-back is 30); None leaves it out. DECODED."""
    inner = eh.encode_varint_field(1, _flag(enabled, 'enabled'))
    if angle is not None:
        inner += eh.encode_varint_field(2, _int_in(angle, HEAD_UP_ANGLE_MIN, HEAD_UP_ANGLE_MAX,
                                                   'head-up angle'))
    return build_set(magic, F_HEAD_UP, inner)


def build_display_height(magic, level):
    """Display height (Y) {3:{2:{1 level}}}. DECODED. The real range was not recovered (the
    capture reads 6); values are guarded to 0..DISPLAY_LEVEL_MAX. Read back before changing."""
    return build_set(magic, F_Y, eh.encode_varint_field(1, _int_in(level, 0, DISPLAY_LEVEL_MAX,
                                                                   'display height')))


def build_display_distance(magic, level):
    """Display distance (X) {3:{3:{1 level}}}. DECODED; range unknown, guarded like height."""
    return build_set(magic, F_X, eh.encode_varint_field(1, _int_in(level, 0, DISPLAY_LEVEL_MAX,
                                                                   'display distance')))


def build_display_position(magic, height=None, distance=None):
    """Convenience: a list of payloads, one per given value (height first). Each setting is its
    own message in the app (setGlassGridHeight / setGlassGridDistance). DECODED."""
    if height is None and distance is None:
        raise ValueError('give height and/or distance')
    out = []
    if height is not None:
        out.append(build_display_height(magic, height))
    if distance is not None:
        out.append(build_display_distance(magic, distance))
    return out


def build_units(magic, unit_format=0, distance_unit=0, time_format=1, date_format=0,
                temperature_unit=1):
    """Units/formats {3:{9:{1 unitFormat, 2 distanceUnit, 3 timeFormat, 4 dateFormat,
    5 temperatureUnit}}}. CAPTURED with the defaults (= UNITS_APP_DEFAULT, sent on every
    connect). All five are always written, zeros included, like the app. Value meanings: see
    UNITS_APP_DEFAULT (time_format 0 = 24 h, 1 = 12 h; temperature_unit 1 = °C, 2 = °F)."""
    vals = (unit_format, distance_unit, time_format, date_format, temperature_unit)
    names = ('unit_format', 'distance_unit', 'time_format', 'date_format', 'temperature_unit')
    inner = b''
    for i, (v, n) in enumerate(zip(vals, names), start=1):
        inner += eh.encode_varint_field(i, _int_in(v, 0, ENUM_MAX, n))
    return build_set(magic, F_UNITS, inner)


def build_gestures(magic, entries=GESTURES_APP_DEFAULT):
    """Gesture-control list {3:{10:{1:{1 screenOn, 2 operationType, 3 apptype}} ...}}.
    CAPTURED with the default (GESTURES_APP_DEFAULT). The meaning of each entry is unknown:
    copy the app's list unless you are experimenting. apptype 0 = app_unable, 1 = app_menu."""
    if not entries:
        raise ValueError('entries must not be empty')
    inner = b''
    for e in entries:
        if len(e) != 3:
            raise ValueError(f'each gesture entry is (screen_on, operation_type, app_type), got {e!r}')
        screen_on, op, app = e
        item = (eh.encode_varint_field(1, _flag(screen_on, 'screen_on')) +
                eh.encode_varint_field(2, _int_in(op, 0, ENUM_MAX, 'operation_type')) +
                eh.encode_varint_field(3, _int_in(app, 0, 1, 'app_type')))
        inner += eh.encode_message_field(1, item)
    return build_set(magic, F_GESTURES, inner)


def build_dominant_hand(magic, hand, ring_mac=None):
    """Dominant hand {3:{11:{1 hand, 2 ringMac}}}. DECODED; only matters with the Even ring.
    The hand values are not recovered (INFERRED 0/1); ring_mac is 6 bytes if given."""
    inner = eh.encode_varint_field(1, _int_in(hand, 0, 1, 'hand'))
    if ring_mac is not None:
        if len(ring_mac) != 6:
            raise ValueError('ring_mac must be 6 bytes')
        inner += eh.encode_bytes_field(2, bytes(ring_mac))
    return build_set(magic, F_HAND, inner)


# ── SID 0x20 builders ─────────────────────────────────────────────────────────────────────────

def _timeout_value(seconds):
    if seconds is None or seconds == 'never':
        return TIMEOUT_NEVER
    if isinstance(seconds, bool) or seconds not in TIMEOUT_CHOICES:
        raise ValueError(f"display timeout must be one of {TIMEOUT_CHOICES} or 'never', got {seconds!r}")
    return seconds


def build_display_timeout(magic, seconds):
    """Dashboard auto-close (screen-off) time, SID 0x20 cmd 2 {4:{1 seconds}}. seconds in
    TIMEOUT_CHOICES; 'never' or None → TIMEOUT_NEVER (65365). DECODED (matches the decompile's hex
    example; never captured). Frame on SID_MODULE."""
    return _module(MOD_CMD_SET_TIMEOUT, magic, 4, eh.encode_varint_field(1, _timeout_value(seconds)))


def build_read_display_timeout(magic):
    """SID 0x20 cmd 1 {4:{}}: read the current screen-off time (fresh pair reports 30). DECODED.
    Reply → decode_module()['timeout']."""
    return _module(MOD_CMD_READ_TIMEOUT, magic, 4, b'')


def build_language(magic, index):
    """Glasses UI language, SID 0x20 cmd 0 {1:0, 2:magic, 3:{1 languageIndex}} (cmd 0 is written
    explicitly, as the app does for cmd 0 elsewhere). index is a key of LANGUAGES. DECODED."""
    if isinstance(index, str):
        match = [k for k, v in LANGUAGES.items() if v.lower() == index.lower()]
        if not match:
            raise ValueError(f'unknown language {index!r}; choose from {list(LANGUAGES.values())}')
        index = match[0]
    _int_in(index, 0, max(LANGUAGES), 'language index')
    return _module(MOD_CMD_LANGUAGE, magic, 3, eh.encode_varint_field(1, index))


# ── decoders ──────────────────────────────────────────────────────────────────────────────────

def decode_readback(sub):
    """DeviceReceiveRequestFromAPP bytes → {key: value} using READBACK_FIELDS. Only fields present
    on the wire are included (the glasses leave zero values out, so a missing switch means 0);
    use with_defaults() to fill them. Versions are decoded as text."""
    out = {}
    for f, wire, v in eh._iter_fields(sub):
        key = READBACK_FIELDS.get(f, f'field_{f}')
        if f in _STRING_FIELDS and wire == 2:
            out[key] = v.decode('utf-8', 'replace')
        elif wire == 0:
            out[key] = v
    return out


def with_defaults(values):
    """Fill every numeric READBACK_FIELDS key missing from a read-back dict with 0 (proto3
    default), so e.g. 'silent_mode' is 0 rather than absent."""
    full = {k: 0 for f, k in READBACK_FIELDS.items() if f not in _STRING_FIELDS and f != 1}
    full.update(values)
    return full


def _decode_set(sub):
    """Decode a set sub-message (DeviceReceiveInfoFromAPP) or its ack echo into {name: value}."""
    out = {}
    for f, wire, v in eh._iter_fields(sub):
        if wire != 2:
            continue
        name = SET_NAMES.get(f, f'field_{f}')
        if f == F_BRIGHTNESS:
            out[name] = {'auto': eh.read_varint_field(v, 1, 0), 'level': eh.read_varint_field(v, 2, 0),
                         'left_calibration': eh.read_varint_field(v, 3, 0),
                         'right_calibration': eh.read_varint_field(v, 4, 0)}
        elif f == F_HEAD_UP:
            out[name] = {'enabled': eh.read_varint_field(v, 1, 0), 'angle': eh.read_varint_field(v, 2, 0),
                         'calibration_switch': eh.read_varint_field(v, 3, 0),
                         'calibration': eh.read_varint_field(v, 4, 0)}
        elif f == F_UNITS:
            out[name] = {k: eh.read_varint_field(v, i, 0) for i, k in enumerate(
                ('unit_format', 'distance_unit', 'time_format', 'date_format', 'temperature_unit'),
                start=1)}
        elif f == F_GESTURES:
            out[name] = [(eh.read_varint_field(g, 1, 0), eh.read_varint_field(g, 2, 0),
                          eh.read_varint_field(g, 3, 0))
                         for gf, gw, g in eh._iter_fields(v) if gf == 1 and gw == 2]
        elif f == F_HAND:
            mac = eh.read_bytes_field(v, 2)
            out[name] = {'hand': eh.read_varint_field(v, 1, 0),
                         'ring_mac': mac.hex(':') if mac else None}
        else:
            out[name] = eh.read_varint_field(v, 1, 0)
    return out


def decode(pb):
    """Decode any SID 0x09 message from the glasses into a dict:
      {cmd, name, magic, push, values, set, event, label}
    - READ reply / unprompted push (cmd 2): `values` = decode_readback(...) e.g. {'battery': 100,
      'head_up_angle': 30, 'left_version': '2.3.0.24', ...}. push=True when the request type
      (field 1) is missing, which is how the glasses' own pushes (flag 0x01) look. CAPTURED.
    - SET ack (cmd 1): `set` = the echoed setting(s), zero fields filled in, e.g.
      {'units': {'time_format': 1, 'temperature_unit': 1, ...}}. CAPTURED.
    - DEVICE_EVENT (cmd 3): `event` = {'recalibration_status', 'silent_mode'}. DECODED.
    - DEVICE_ACK / APP_ACK (cmd 4/5): `event` = {'package_id', 'flag'}. DECODED."""
    cmd = eh.read_varint_field(pb, 1, -1)
    out = {'cmd': cmd, 'name': CMD_NAMES.get(cmd, f'cmd={cmd}'), 'magic': eh.read_varint_field(pb, 2, -1),
           'push': False, 'values': None, 'set': None, 'event': None}
    label = out['name']
    req = eh.read_bytes_field(pb, 4)
    if req is not None:
        out['values'] = decode_readback(req)
        out['push'] = 'setting_type' not in out['values']
        v = out['values']
        label += (' push' if out['push'] else '') + ''.join(
            f' {k}={v[k]}' for k in ('battery', 'charging', 'brightness_level', 'head_up_angle')
            if k in v)
    sub = eh.read_bytes_field(pb, 3)
    if sub is not None:
        out['set'] = _decode_set(sub)
        label += ' ' + ','.join(out['set']) if out['set'] else ''
    ev = eh.read_bytes_field(pb, 5)
    if ev is not None:
        out['event'] = {'recalibration_status': eh.read_varint_field(ev, 1, 0),
                        'silent_mode': eh.read_varint_field(ev, 2, 0)}
    for fld in (6, 7):
        ack = eh.read_bytes_field(pb, fld)
        if ack is not None:
            out['event'] = {'package_id': eh.read_varint_field(ack, 1, 0),
                            'flag': eh.read_varint_field(ack, 2, 0)}
    out['label'] = label
    return out


def decode_module(pb):
    """Decode a SID 0x20 reply: {cmd, name, magic, language, timeout, timeout_never}. timeout is
    the auto-close value in seconds (TIMEOUT_NEVER or anything > 20 means "never", as in the app).
    DECODED (no 0x20 traffic in any capture)."""
    cmd = eh.read_varint_field(pb, 1, -1)
    out = {'cmd': cmd, 'name': MOD_CMD_NAMES.get(cmd, f'cmd={cmd}'),
           'magic': eh.read_varint_field(pb, 2, -1), 'language': None, 'timeout': None,
           'timeout_never': None}
    sysset = eh.read_bytes_field(pb, 3)
    if sysset is not None:
        out['language'] = eh.read_varint_field(sysset, 1, 0)
    dash = eh.read_bytes_field(pb, 4)
    if dash is not None:
        t = eh.read_varint_field(dash, 1, -1)
        if t >= 0:
            out['timeout'] = t
            out['timeout_never'] = t > 20
    return out
