"""Device config (SID 0x80), onboarding (SID 0x10) and case (SID 0x81): what the official app sends
right after it connects, the 60-second keep-alive, and the device commands (restart, reset, ...).

Plain English: after the phone authenticates to each lens (0x80 cmd 4, see g2.session), the official
app does a few quiet housekeeping steps before it shows anything:

    1. tells the pair which lens takes commands   0x80 cmd 5   PIPE_ROLE_CHANGE  {4:{1:1}} = RIGHT
    2. sets the glasses' clock and time zone      0x80 cmd 128 TIME_SYNC         {128:{1 secs, 2 tz}}
    3. reads the settings, pushes units            0x09 (see g2.settings)
    4. ends any leftover setup tutorial           0x10 cmd 1   CONFIG FINISH     {3:{1:4}}
    5. asks whether the glasses are being worn    0x10 cmd 3   EVENT             {5:{1:1}}
    ...and then, every 60 s for as long as it is connected, it sends a keep-alive to BOTH lenses:
                                                  0x80 cmd 14  BASE_CONNECT_HEART_BEAT {13:{}}
connect_sequence() returns that list in the capture's order, so a session can replay it.

Every 0x80 message is a `DevCfgDataPackage` protobuf:
    1 commandId   CMD_* below                 2 magicRandom  request id; the reply echoes it
    3..14, 128    exactly one sub-message, chosen by the command (FIELD_FOR_CMD below)
Onboarding messages are `OnboardingDataPackage{1 commandId, 2 magic, 3 config, 4 heartbeat, 5 event}`.

Wire facts seen in the three real official-app captures (docs/captures/scratch-0924-0928/
real_btsnoop_hci{,2,3}.log, decoded in docs/research/standalone/*.frames):
  - The app writes fields it has set even when they are 0 (TimeSync tz=0 is on the wire as 10 00).
    The glasses' replies leave zero fields out. The builders here copy the app.
  - The app uses the same number for the frame seq byte and the protobuf magic.
  - Frame flag: 0x20 (evenhub.FLAG_REQUEST) for role, time sync, 0x09, 0x10, 0x81;
    0x00 (FLAG_PLAIN) for auth and the keep-alive. Use flag_for(sid, pb) when framing.
  - Everything in the connect sequence goes to the RIGHT lens. Only auth and the keep-alive go to
    the left lens as well.

Status legend used in every docstring:
  LIVE      seen working from our own hub
  CAPTURED  byte-for-byte what the official app sends (golden tests in
            tests/test_g2_device_settings.py)
  DECODED   from the decompiled app's schema only; never sent by us or seen in a capture

Functions here only build or decode bytes; nothing talks to the glasses. Sending example:
    pb = device.build_time_sync(magic)
    for f in evenhub.frame_pb(pb, device.SID, device.flag_for(device.SID, pb), seq):
        await session._send_right(f)
"""
import time as _time

from . import evenhub as eh

SID = 0x80              # device config (DevCfgDataPackage)
SID_ONBOARDING = 0x10   # OnboardingDataPackage
SID_CASE = 0x81         # GlassesCaseDataPackage

FLAG_REQUEST = eh.FLAG_REQUEST   # 0x20
FLAG_PLAIN = 0x00                # auth + keep-alive use this (capture)

# eDevCfgCommandId
CMD_AUTH = 4
CMD_ROLE_CHANGE = 5
CMD_RING_CONNECT_INFO = 6
CMD_BLE_CONNECT_PARAM = 7
CMD_DISCONNECT = 8
CMD_UNPAIR = 9
CMD_EXCEPTION = 10
CMD_SET_DEVICE_INFO = 11
CMD_GET_DEVICE_INFO = 12
CMD_FACTORY_RESET = 13
CMD_KEEPALIVE = 14
CMD_QUICK_RESTART = 15
CMD_TIME_SYNC = 128
CMD_AUD_CONTROL = 129
CMD_ERROR = 255

CMD_NAMES = {CMD_AUTH: 'AUTHENTICATION', CMD_ROLE_CHANGE: 'PIPE_ROLE_CHANGE',
             CMD_RING_CONNECT_INFO: 'RING_CONNECT_INFO', CMD_BLE_CONNECT_PARAM: 'BLE_CONNECT_PARAM',
             CMD_DISCONNECT: 'DISCONNECT_INFO', CMD_UNPAIR: 'UNPAIR_INFO',
             CMD_EXCEPTION: 'COMMAND_EXCEPTION', CMD_SET_DEVICE_INFO: 'SET_DEVICE_INFO',
             CMD_GET_DEVICE_INFO: 'GET_DEVICE_INFO', CMD_FACTORY_RESET: 'RESTORE_TO_FACTORY_SETTINGS',
             CMD_KEEPALIVE: 'BASE_CONNECT_HEART_BEAT', CMD_QUICK_RESTART: 'QUICK_RESTART',
             CMD_TIME_SYNC: 'TIME_SYNC', CMD_AUD_CONTROL: 'AUD_CONTROL', CMD_ERROR: 'COMMAND_ERROR'}

# Which DevCfgDataPackage field carries the sub-message for each command.
FIELD_FOR_CMD = {CMD_AUTH: 3, CMD_ROLE_CHANGE: 4, CMD_RING_CONNECT_INFO: 5, CMD_BLE_CONNECT_PARAM: 6,
                 CMD_DISCONNECT: 7, CMD_UNPAIR: 8, CMD_EXCEPTION: 9, CMD_SET_DEVICE_INFO: 10,
                 CMD_GET_DEVICE_INFO: 11, CMD_FACTORY_RESET: 12, CMD_KEEPALIVE: 13,
                 CMD_QUICK_RESTART: 14, CMD_TIME_SYNC: 128, CMD_AUD_CONTROL: 129}

# Which field of each sub-message is its `result` (eErrorCode) in a reply.
_RESULT_FIELD = {CMD_AUTH: 3, CMD_ROLE_CHANGE: 2, CMD_RING_CONNECT_INFO: 5, CMD_BLE_CONNECT_PARAM: 5,
                 CMD_DISCONNECT: 3, CMD_UNPAIR: 3, CMD_EXCEPTION: 2, CMD_SET_DEVICE_INFO: 2,
                 CMD_GET_DEVICE_INFO: 2, CMD_FACTORY_RESET: 1, CMD_KEEPALIVE: 1,
                 CMD_QUICK_RESTART: 1, CMD_TIME_SYNC: 3, CMD_AUD_CONTROL: 3}

# eGlassesLR (PipeRoleChange.asCmdRole)
ROLE_BOTH = 0
ROLE_RIGHT = 1
ROLE_LEFT = 2
ROLE_NAMES = {ROLE_BOTH: 'BOTH', ROLE_RIGHT: 'RIGHT', ROLE_LEFT: 'LEFT'}

# eDevice (AuthMgr.phoneType, DisconnectInfo.dev, UnpairInfo.dev)
DEV_RING = 0
DEV_GLASSES = 1
DEV_RING_GLASSES = 2
DEV_PHONE_IOS = 3
DEV_PHONE_ANDROID = 4
DEV_NAMES = {DEV_RING: 'RING', DEV_GLASSES: 'GLASSES', DEV_RING_GLASSES: 'RING_GLASSES',
             DEV_PHONE_IOS: 'PHONE_IOS', DEV_PHONE_ANDROID: 'PHONE_ANDROID'}

# eDeviceConfigInfoItem (DeviceInfoValue.cfgInfoItem)
INFO_ALL = 0
INFO_BRIGHTNESS = 1
INFO_ANTI_SHAKE = 2
INFO_DISPLAY_MODE = 3
INFO_WORK_MODE = 4
INFO_WAKEUP_ANGLE = 5
INFO_GLASSES_SN = 6
INFO_DEVICE_SN = 7
INFO_BLE_MAC = 8
INFO_NAMES = {INFO_ALL: 'ALL_INFO', INFO_BRIGHTNESS: 'DEVICE_BRIGHTNESS',
              INFO_ANTI_SHAKE: 'DEVICE_ANTI_SHAKE_ENABLE', INFO_DISPLAY_MODE: 'DEVICE_DISPLAY_MODE',
              INFO_WORK_MODE: 'DEVICE_WORK_MODE', INFO_WAKEUP_ANGLE: 'DEVICE_WAKEUP_ANGLE',
              INFO_GLASSES_SN: 'DEVICE_GLASSES_SN', INFO_DEVICE_SN: 'DEVICE_DEVICE_SN',
              INFO_BLE_MAC: 'DEVICE_BLE_MAC'}

# eErrorCode (the `result` of every reply; 0 = SUCCESS and is left out of the reply)
ERROR_NAMES = {0: 'SUCCESS', 1: 'CRC', 2: 'PKT_LOST', 3: 'TIMEOUT', 4: 'NO_RESOURCES', 5: 'PB_ERROR',
               6: 'NULL', 7: 'FAIL', 8: 'NOT_SUPPORT', 9: 'SUPPORT', 10: 'HEAD_ID',
               11: 'INVALID_LENGTH', 12: 'INVALID_SDID', 13: 'DUPLICATE_PACKET',
               90: 'RING_CONNECT_TIMEOUT'}

KEEPALIVE_INTERVAL_S = 60.0   # EvenHeartbeatPool.startHeartbeat(120 Smi); capture 2 shows 60 s
KEEPALIVE_LEFT_LEAD_S = 2.0   # in capture 2 the left lens gets it ~2 s before the right lens

TZ_UNIT_MINUTES = 15          # TimeSync.timezone unit (g2TimezoneUnitsFromOffset)
TZ_MIN, TZ_MAX = -128, 127    # the app clamps to this

# ── onboarding (SID 0x10) ─────────────────────────────────────────────────────────────────────
OB_CMD_CONFIG = 1
OB_CMD_HEARTBEAT = 2
OB_CMD_EVENT = 3
OB_CMD_NAMES = {OB_CMD_CONFIG: 'CONFIG', OB_CMD_HEARTBEAT: 'HEARTBEAT', OB_CMD_EVENT: 'EVENT'}
OB_FIELD_FOR_CMD = {OB_CMD_CONFIG: 3, OB_CMD_HEARTBEAT: 4, OB_CMD_EVENT: 5}

# eOnboardingProcessId
OB_START_UP = 1
OB_TOUCH = 2
OB_HEAD_UP = 3
OB_FINISH = 4
OB_PROCESS_NAMES = {OB_START_UP: 'START_UP', OB_TOUCH: 'TOUCH', OB_HEAD_UP: 'HEAD_UP',
                    OB_FINISH: 'FINISH'}

# eOnboardingEvent
OB_EVENT_WEAR_STATUS = 1      # GLS_WEAR_STATUS; reply eventParam 1 = worn, 0 = not worn

# ── case (SID 0x81) ───────────────────────────────────────────────────────────────────────────
CASE_CMD_INFO = 1


def _signed_varint(value):
    """int32/int64 varint: negative numbers are sent as their 64-bit two's complement (10 bytes)."""
    return eh.encode_varint(value + (1 << 64) if value < 0 else value)


def _decode_signed(value):
    """Undo _signed_varint for an int32 field read back as an unsigned varint."""
    return value - (1 << 64) if value >= 1 << 63 else value


def flag_for(sid, pb):
    """The frame flag byte (evenhub.frame_pb `flag`) the official app uses for this message:
    FLAG_PLAIN (0x00) for 0x80 auth and keep-alive, FLAG_REQUEST (0x20) for everything else."""
    if sid == SID and eh.read_varint_field(pb, 1, -1) in (CMD_AUTH, CMD_KEEPALIVE):
        return FLAG_PLAIN
    return FLAG_REQUEST


def build_package(cmd, magic, inner):
    """DevCfgDataPackage {1 cmd, 2 magic, FIELD_FOR_CMD[cmd] inner}. `inner` may be b''."""
    return (eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(FIELD_FOR_CMD[cmd], inner))


def build_auth(magic, phone_type=DEV_PHONE_ANDROID):
    """0x80 cmd 4 AuthMgr{1 secAuth=1, 2 phoneType}. LIVE + CAPTURED (flag 0x00, sent to each lens).
    Included for completeness; g2.session already sends it. The lens replies {3:{}} and then pushes
    {3:{1:1}} (flag 0x01) once authenticated."""
    return build_package(CMD_AUTH, magic, eh.encode_varint_field(1, 1) +
                         eh.encode_varint_field(2, phone_type))


def build_role_change(magic, role=ROLE_RIGHT):
    """0x80 cmd 5 PIPE_ROLE_CHANGE {4:{1 asCmdRole}}: tells the pair which lens is the command
    lens. CAPTURED: the app sends role RIGHT to the right lens right after its auth. The reply is
    {4:{}} (result SUCCESS)."""
    if role not in ROLE_NAMES:
        raise ValueError(f'role must be one of {sorted(ROLE_NAMES)}, got {role!r}')
    return build_package(CMD_ROLE_CHANGE, magic, eh.encode_varint_field(1, role))


def tz_units_from_offset(offset_seconds):
    """UTC offset in seconds → TimeSync.timezone units of 15 minutes, truncated toward zero like
    Dart's ~/ and clamped to -128..127. Examples: +3600 → 4, -18000 (UTC-5) → -20, +19800 → 22."""
    q = int(offset_seconds / (TZ_UNIT_MINUTES * 60))
    return max(TZ_MIN, min(TZ_MAX, q))


def local_tz_units(now=None):
    """This machine's current UTC offset (daylight saving included) in 15-minute units, at unix time
    `now` (default: now). Uses time.localtime().tm_gmtoff, which already includes DST."""
    t = _time.time() if now is None else now
    return tz_units_from_offset(_time.localtime(t).tm_gmtoff)


def build_time_sync(magic, now=None, tz_units=None):
    """0x80 cmd 128 TimeSync{1 unix seconds (uint32), 2 timezone (int32, 15-min units)}.
    CAPTURED (sent on every connect, right lens, flag 0x20; the capture phone was on UTC so tz=0,
    which the app still writes as 10 00). The app re-sends it on a time-zone, DST or clock change;
    the hub should also resend it hourly. `now` defaults to time.time(), `tz_units` to
    local_tz_units(now). Reply: {128:{}}."""
    secs = int(_time.time() if now is None else now)
    if not 0 <= secs <= 0xFFFFFFFF:
        raise ValueError(f'unix seconds out of uint32 range: {secs}')
    tz = local_tz_units(secs) if tz_units is None else int(tz_units)
    if not TZ_MIN <= tz <= TZ_MAX:
        raise ValueError(f'tz_units must be in {TZ_MIN}..{TZ_MAX}, got {tz}')
    inner = eh.encode_varint_field(1, secs) + eh.encode_key(2, 0) + _signed_varint(tz)
    return build_package(CMD_TIME_SYNC, magic, inner)


def build_keepalive(magic):
    """0x80 cmd 14 BASE_CONNECT_HEART_BEAT {13:{}}. CAPTURED (capture 2 and 3).
    Send it every KEEPALIVE_INTERVAL_S (60 s) to BOTH lenses (left first, ~2 s before the right),
    with FLAG_PLAIN (0x00) — each lens echoes the identical frame back. Two missed echoes = dead
    link. Use a separate magic per lens, as the app does."""
    return build_package(CMD_KEEPALIVE, magic, b'')


def build_quick_restart(magic):
    """0x80 cmd 15 QUICK_RESTART {14:{}}: reboots the glasses. DECODED (untested).
    DISRUPTIVE: the BLE link drops; reconnect and re-run connect_sequence afterwards."""
    return build_package(CMD_QUICK_RESTART, magic, b'')


def build_factory_reset(magic):
    """0x80 cmd 13 RESTORE_TO_FACTORY_SETTINGS {12:{}}. DECODED (untested).
    *** DESTRUCTIVE ***: wipes all settings and (presumably) the Bluetooth bonds, so both lenses
    must be paired again. The official app sends it when the Even server blacklists the serial.
    Only call this behind an explicit user confirmation."""
    return build_package(CMD_FACTORY_RESET, magic, b'')


def _dev_ring(dev, ring_mac):
    if dev not in (DEV_RING, DEV_GLASSES, DEV_RING_GLASSES):
        raise ValueError(f'dev must be DEV_RING, DEV_GLASSES or DEV_RING_GLASSES, got {dev!r}')
    inner = eh.encode_varint_field(1, dev)
    if ring_mac:
        if len(ring_mac) != 6:
            raise ValueError('ring_mac must be 6 bytes')
        inner += eh.encode_bytes_field(2, bytes(ring_mac))
    return inner


def build_disconnect(magic, dev=DEV_RING, ring_mac=None):
    """0x80 cmd 8 DISCONNECT_INFO {7:{1 dev, 2 ringMac}}. DECODED (untested). From the field names
    this mainly manages the ring link. DISRUPTIVE for dev=DEV_GLASSES (drops the link)."""
    return build_package(CMD_DISCONNECT, magic, _dev_ring(dev, ring_mac))


def build_unpair(magic, dev=DEV_RING, ring_mac=None):
    """0x80 cmd 9 UNPAIR_INFO {8:{1 dev, 2 ringMac}}. DECODED (untested).
    *** DESTRUCTIVE ***: removes a pairing (the ring's by default; dev=DEV_GLASSES may drop the
    glasses' bond, which then needs a re-pair). g2-kit notes an unknown 0x80 write once left a
    pair needing "power-cycle + re-pair". Only call behind an explicit user confirmation."""
    return build_package(CMD_UNPAIR, magic, _dev_ring(dev, ring_mac))


def build_get_device_info(magic, item=INFO_ALL):
    """0x80 cmd 12 GET_DEVICE_INFO {11:{1:{1 cfgInfoItem}}}: ask for serial numbers, BLE MAC,
    ALS brightness, wake angle... DECODED (untested; never seen in a capture, the app may not use
    it on the G2). Decode the answer with decode()['info']."""
    if item not in INFO_NAMES:
        raise ValueError(f'item must be one of {sorted(INFO_NAMES)}, got {item!r}')
    value = eh.encode_varint_field(1, item)
    return build_package(CMD_GET_DEVICE_INFO, magic, eh.encode_message_field(1, value))


# ── onboarding (SID 0x10) ─────────────────────────────────────────────────────────────────────

def build_onboarding_config(magic, process_id=OB_FINISH):
    """SID 0x10 cmd 1 CONFIG {3:{1 processId}}. CAPTURED for FINISH: the app sends it on EVERY
    connect (self-heal against a stuck tutorial); the glasses echo {3:{1:4}}. Other process ids
    (START_UP starts the on-glasses tutorial) are DECODED only — do not send START_UP casually."""
    if process_id not in OB_PROCESS_NAMES:
        raise ValueError(f'process_id must be one of {sorted(OB_PROCESS_NAMES)}, got {process_id!r}')
    return (eh.encode_varint_field(1, OB_CMD_CONFIG) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(3, eh.encode_varint_field(1, process_id)))


def build_onboarding_finish(magic):
    """Onboarding FINISH (SID 0x10 cmd 1 {3:{1:4}}). CAPTURED. See build_onboarding_config."""
    return build_onboarding_config(magic, OB_FINISH)


def build_wear_status_query(magic):
    """SID 0x10 cmd 3 EVENT {5:{1:1 GLS_WEAR_STATUS}}: "are the glasses on a head?". CAPTURED.
    Reply {5:{1:1, 2:<1 worn | 0 not>}} → decode_onboarding()['worn']."""
    return (eh.encode_varint_field(1, OB_CMD_EVENT) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(5, eh.encode_varint_field(1, OB_EVENT_WEAR_STATUS)))


# ── case (SID 0x81) ───────────────────────────────────────────────────────────────────────────

def build_case_info_query(magic):
    """SID 0x81 cmd 1 {3:{}}: charging-case battery. CAPTURED. The right lens answers {3:{1 soc}};
    decode with decode_case()."""
    return (eh.encode_varint_field(1, CASE_CMD_INFO) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(3, b''))


# ── the official app's connect sequence ───────────────────────────────────────────────────────

def _fixed(cmd, magic, tail):
    return eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic) + tail


def connect_sequence(magic_fn, now=None, tz_units=None, full=False):
    """The ordered list of (sid, payload, lens) the official app sends after auth, as seen in all
    three real captures (first seconds of real_btsnoop_hci.log / hci3.log). lens is 'right' |
    'left' | 'both'; every step goes to the right lens. `magic_fn()` returns the next magic
    (the app uses the frame seq for it). Frame each with flag_for(sid, payload). CAPTURED.

    Default (full=False) — device/settings housekeeping only:
        0x80 cmd 5   role RIGHT                  0x80 cmd 128 time sync (now, tz_units)
        0x09 cmd 2   read basic settings         0x09 cmd 1   units {0,0,1,0,1}
        0x10 cmd 1   onboarding FINISH           0x10 cmd 3   wear-status query
        0x81 cmd 1   case battery query          0x09 cmd 1   gesture-control list
    full=True also inserts, at their captured positions, the other fixed-payload steps:
        0x07 cmd 10 Even AI CONFIG {13:{1:0,2:80,4:0}}, 0x0c cmd 2 QuickList empty FULL_UPDATE,
        0x0d cmd 0 app-sync request, 0x1f cmd 0 watchface version check {3:{1:1}},
        0x04 cmd 1 notification control {3:{1:1,2:0,3:5,5:0}}.
    Content pushes (menu, dashboard weather/calendar/news) are left to the caller. Repeats seen
    in the capture (two extra settings reads, a second menu push, cap. 3's second time sync) are
    dropped. The left lens's auth, which the app does between time sync and Even AI CONFIG, is
    not included: authenticate both lenses before replaying this."""
    from . import settings as st   # local import: settings imports nothing from here
    seq = []

    def add(sid, build, lens='right'):
        seq.append((sid, build(magic_fn()), lens))

    add(SID, build_role_change)
    add(SID, lambda m: build_time_sync(m, now, tz_units))
    if full:
        add(0x07, lambda m: _fixed(10, m, eh.encode_message_field(
            13, eh.encode_varint_field(1, 0) + eh.encode_varint_field(2, 80) +
            eh.encode_varint_field(4, 0))))
        add(0x0C, lambda m: _fixed(2, m, eh.encode_message_field(
            4, eh.encode_varint_field(1, 1) + eh.encode_varint_field(2, 0))))
        add(0x0D, lambda m: _fixed(0, m, b''))
    add(st.SID, st.build_read_settings)
    add(st.SID, st.build_units)
    add(SID_ONBOARDING, build_onboarding_finish)
    add(SID_ONBOARDING, build_wear_status_query)
    if full:
        add(0x1F, lambda m: _fixed(0, m, eh.encode_message_field(3, eh.encode_varint_field(1, 1))))
    add(SID_CASE, build_case_info_query)
    if full:
        add(0x04, lambda m: _fixed(1, m, eh.encode_message_field(
            3, eh.encode_varint_field(1, 1) + eh.encode_varint_field(2, 0) +
            eh.encode_varint_field(3, 5) + eh.encode_varint_field(5, 0))))
    add(st.SID, st.build_gestures)
    return seq


def keepalive_steps(magic_fn):
    """One keep-alive round as (sid, payload, lens): left first, then right, each with its own
    magic, both FLAG_PLAIN. Space the two by KEEPALIVE_LEFT_LEAD_S if you want to copy the app
    exactly; repeat every KEEPALIVE_INTERVAL_S. CAPTURED."""
    return [(SID, build_keepalive(magic_fn()), 'left'), (SID, build_keepalive(magic_fn()), 'right')]


# ── decoders ──────────────────────────────────────────────────────────────────────────────────

def _decode_device_info(sub):
    items = []
    for f, wire, v in eh._iter_fields(sub):
        if f != 1 or wire != 2:
            continue
        item = {'item': eh.read_varint_field(v, 1, 0)}
        item['name'] = INFO_NAMES.get(item['item'], item['item'])
        als = eh.read_bytes_field(v, 2)
        if als is not None:
            item['als_brightness'] = eh.read_varint_field(als, 1, 0)
            item['als_auto'] = eh.read_varint_field(als, 2, 0)
        if eh.read_varint_field(v, 3, -1) >= 0:
            item['anti_shake'] = eh.read_varint_field(v, 3, 0)
        for fld, key in ((4, 'display_mode'), (5, 'wake_mode')):
            m = eh.read_bytes_field(v, fld)
            if m is not None:
                item[key] = eh.read_varint_field(m, 1, 0)
        ang = eh.read_bytes_field(v, 6)
        if ang is not None:
            item['wakeup_angle'] = eh.read_varint_field(ang, 1, 0)
            item['wakeup_offset'] = eh.read_varint_field(ang, 2, 0)
        for fld, key in ((7, 'glasses_sn'), (8, 'device_sn')):
            m = eh.read_bytes_field(v, fld)
            if m is not None:
                raw = eh.read_bytes_field(m, 1) or b''
                item[key] = raw.decode('utf-8', 'replace')
        mac = eh.read_bytes_field(v, 9)
        if mac is not None:
            raw = eh.read_bytes_field(mac, 1) or b''
            item['ble_mac'] = ':'.join(f'{b:02X}' for b in raw)
        items.append(item)
    return items


def decode(pb):
    """Decode a SID 0x80 message from the glasses into a dict {cmd, name, magic, result, label, ...}.
    result is the eErrorCode (0 = SUCCESS; replies leave it out when 0). Extra keys:
      AUTHENTICATION push {3:{1:1}} → authenticated=True     (CAPTURED, flag 0x01)
      TIME_SYNC with fields → timestamp, timezone             (echo is {128:{}}; CAPTURED)
      PIPE_ROLE_CHANGE with fields → role
      GET/SET_DEVICE_INFO → info = [ {item, name, glasses_sn, ble_mac, ...}, ... ]   (DECODED)
    The keep-alive reply is the identical {1:14, 2:magic, 13:{}} (CAPTURED)."""
    cmd = eh.read_varint_field(pb, 1, -1)
    magic = eh.read_varint_field(pb, 2, -1)
    name = CMD_NAMES.get(cmd, f'cmd={cmd}')
    out = {'cmd': cmd, 'name': name, 'magic': magic, 'result': 0}
    field = FIELD_FOR_CMD.get(cmd)
    sub = eh.read_bytes_field(pb, field) if field else None
    if sub is not None:
        rf = _RESULT_FIELD.get(cmd)
        if rf:
            out['result'] = eh.read_varint_field(sub, rf, 0)
        if cmd == CMD_AUTH:
            out['authenticated'] = eh.read_varint_field(sub, 1, 0) == 1
        elif cmd == CMD_TIME_SYNC and sub:
            out['timestamp'] = eh.read_varint_field(sub, 1, 0)
            out['timezone'] = _decode_signed(eh.read_varint_field(sub, 2, 0))
        elif cmd == CMD_ROLE_CHANGE and sub:
            out['role'] = eh.read_varint_field(sub, 1, 0)
        elif cmd in (CMD_GET_DEVICE_INFO, CMD_SET_DEVICE_INFO):
            out['info'] = _decode_device_info(sub)
        elif cmd == CMD_EXCEPTION:
            out['failed_cmd'] = eh.read_varint_field(sub, 1, 0)
    label = name
    if out.get('authenticated'):
        label += ' authenticated'
    if out['result']:
        label += f" result={ERROR_NAMES.get(out['result'], out['result'])}"
    out['label'] = label
    return out


def decode_onboarding(pb):
    """Decode a SID 0x10 message: {cmd, name, magic, process_id, event, event_param, worn, error,
    label}. CAPTURED shapes: FINISH echo {1:1, 3:{1:4}}; wear reply {1:3, 5:{1:1, 2:1}} → worn=True.
    `worn` is None unless the message is a wear-status event."""
    cmd = eh.read_varint_field(pb, 1, -1)
    out = {'cmd': cmd, 'name': OB_CMD_NAMES.get(cmd, f'cmd={cmd}'),
           'magic': eh.read_varint_field(pb, 2, -1), 'process_id': None, 'event': None,
           'event_param': None, 'worn': None, 'error': 0}
    label = out['name']
    cfg = eh.read_bytes_field(pb, 3)
    if cfg is not None:
        out['process_id'] = eh.read_varint_field(cfg, 1, 0)
        out['error'] = eh.read_varint_field(cfg, 2, 0)
        label += f" {OB_PROCESS_NAMES.get(out['process_id'], out['process_id'])}"
    hb = eh.read_bytes_field(pb, 4)
    if hb is not None:
        out['error'] = eh.read_varint_field(hb, 1, 0)
    ev = eh.read_bytes_field(pb, 5)
    if ev is not None:
        out['event'] = eh.read_varint_field(ev, 1, 0)
        out['event_param'] = eh.read_varint_field(ev, 2, 0)
        out['error'] = eh.read_varint_field(ev, 3, 0)
        if out['event'] == OB_EVENT_WEAR_STATUS:
            out['worn'] = out['event_param'] == 1
            label += ' WEAR ' + ('worn' if out['worn'] else 'not worn')
    if out['error']:
        label += f" error={ERROR_NAMES.get(out['error'], out['error'])}"
    out['label'] = label
    return out


def decode_case(pb):
    """Decode a SID 0x81 reply or push: {magic, battery, charging, lid, in_case, error}. Fields
    the glasses leave out are None (the query reply carries only soc; pushes add lid/in-case).
    CAPTURED: reply {3:{1:70}}; push {3:{1:70, 3:1, 4:1}}. lid/in_case meanings INFERRED (1 = open /
    in case). Returns None if there is no caseInfo."""
    info = eh.read_bytes_field(pb, 3)
    if info is None:
        return None

    def opt(f):
        v = eh.read_varint_field(info, f, -1)
        return None if v < 0 else v
    return {'magic': eh.read_varint_field(pb, 2, -1), 'battery': opt(1), 'charging': opt(2),
            'lid': opt(3), 'in_case': opt(4), 'error': eh.read_varint_field(info, 5, 0)}
