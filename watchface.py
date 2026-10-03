"""Watchface layout (SID 0x1f, "dashboard ext"): what sits next to the clock on the dashboard.

Plain English: the dashboard's clock area is configured by a small "layout file" the phone
uploads to the glasses. The file says where the clock goes (left / center / right), which
little extras sit beside it (temperature, sunset, heart rate, steps...), and the order of the
widget carousel. To show the weather temperature next to the clock, upload a layout that asks
for it (build_temperature_layout), then push weather as usual (dashboard.build_weather_push).

Every message is a `DashboardExtPackage` protobuf (schema CONFIRMED, fields_dashboard_ext.txt):
    1 cmdId   CMD_* below        2 magicRandom
    3..10     one sub-message; the field number is cmd + 3 (FIELD_FOR_CMD)

Upload flow (CONFIRMED from dashboard_layout_config_sync_to_glass_helper.dart; details INFERRED
where marked). "app" = us, "OS" = the glasses:
    app -> cmd 0 APP_REQUEST_PB_FILE_INFO {1 cmd=1}              build_info_request
    OS  -> cmd 1 OS_RESPONSE_PB_FILE_INFO {1 exists, 2 version, 3 hash}
           if version and hash equal ours, stop: nothing to do
    app -> cmd 2 APP_REQUEST_UPGRADE_PB_FILE {1 cmd=1 (INFERRED value)}   build_upgrade_request
    OS  -> cmd 3 OS_RESPONSE_UPGRADE_CMD {1 status}  (0 = OK, INFERRED)
    OS  -> cmd 4 OS_NOTIFY_PB_FILE_TRANSMIT_START {1 cmd}
    app -> cmd 5 APP_SEND_PB_FILE_DATA per fragment                 build_file_fragments
           {1 sessionId, 2 totalSize, 3 compressMode (0 = raw, INFERRED), 4 fragmentIndex,
            5 fragmentPacketSize, 6 rawData}; wait for each cmd 6 (6 s timeout, 3 retries)
    OS  -> cmd 6 OS_RECEIVE_PB_FILE_DATA {echoes 1-5, 6 OsStatus}
    OS  -> cmd 7 OS_NOTIFY_PB_FILE_UPDATE_SUCCESS {1 cmd}
Commands 1, 3, 4, 6 and 7 come FROM the glasses (the enum names say OS_...). decode() reads them;
build_transmit_start / build_update_success exist only so tests can fake the glasses' side.

Fragment size: the app uses 4094 or 2047 bytes (ambiguous in the decompile); we use <= 2047. The
file limit is 60000 or 30000 bytes. A typical layout file is under 100 bytes: one fragment.

The layout file, `DashboardExtLayoutConfigureFile`:
    1 pbFileVersion str   2 pbFileHashCode str (INFERRED: any string that changes with content)
    3 pos (POS_*)         4 widgetCount        6 widgetSeq[] (SEQ_*, packed, INFERRED packing)
    7 watchfaceLayout{ one of 1 Layout1{1 clockType, 2 leftSelect, 3 rightSelect}
                              2 Layout2{1 alignIndex, 2 clockType, 3 dateEn, 4 list[]}
                              3 Layout3{1 alignIndex, 2 dateEn, 3 temperatureEn}
                              4 Layout4{1 alignIndex, 2 worldClockCnt, 3 names[], 4 tzOffset[] float} }
widgetSeq uses its OWN numbering (0 NEWS, 1 STOCK, 2 SCHEDULE, 3 HEALTH, 4 QUICKLIST, 5 USER_DEF),
not the dashboard's WidgetType numbers. INFERRED: it should list the same widgets as the
dashboard's widgetDisplayOrder.

RISK: a bad layout file may leave the dashboard blank until a valid one is uploaded. Read the
current version (cmd 0) first, and ideally capture the official app's file once to diff.

Status legend (as in g2/device.py): LIVE / CAPTURED / DECODED.
  Everything here is DECODED and UNTESTED -- nothing has been sent to the glasses. The info
  request (cmd 0) matches the connect-time check seen in a capture (standalone_gaps section 1).
"""
from . import evenhub as eh

SID = 0x1f

# dashboard_ext_cmd_list
CMD_REQUEST_INFO = 0          # app -> OS
CMD_INFO_RESPONSE = 1         # OS -> app
CMD_REQUEST_UPGRADE = 2       # app -> OS
CMD_UPGRADE_RESPONSE = 3      # OS -> app
CMD_TRANSMIT_START = 4        # OS -> app
CMD_FILE_DATA = 5             # app -> OS
CMD_FILE_DATA_ACK = 6         # OS -> app
CMD_UPDATE_SUCCESS = 7        # OS -> app
CMD_NAMES = {0: 'REQUEST_INFO', 1: 'INFO_RESPONSE', 2: 'REQUEST_UPGRADE', 3: 'UPGRADE_RESPONSE',
             4: 'TRANSMIT_START', 5: 'FILE_DATA', 6: 'FILE_DATA_ACK', 7: 'UPDATE_SUCCESS'}
FIELD_FOR_CMD = {c: c + 3 for c in CMD_NAMES}

# watchface_pos
POS_LEFT = 0
POS_CENTER = 1
POS_RIGHT = 2

# widget_component_seq (NOT the same numbers as dashboard.WIDGET_*)
SEQ_NEWS = 0
SEQ_STOCK = 1
SEQ_SCHEDULE = 2
SEQ_HEALTH = 3
SEQ_QUICKLIST = 4
SEQ_USER_DEF = 5

# watchfaceComponentList: what can sit beside the clock
COMP_NONE = 0
COMP_TEMPERATURE = 1
COMP_SUNSET = 2
COMP_NEXTRAIN = 3
COMP_NOTIFICATION = 4
COMP_HEARTRATE = 5
COMP_STEPS = 6
COMP_CALORIES = 7
COMP_BLOOD_OXYGEN = 8
COMP_SLEEP = 9

COMPRESS_RAW = 0              # INFERRED
MAX_FRAGMENT = 2047           # safe choice between the decompile's 4094 / 2047
MAX_FILE_SIZE = 30000         # safe choice between 60000 / 30000


def build_package(cmd, magic, inner):
    """DashboardExtPackage{1 cmd, 2 magic, <cmd + 3>: inner}. cmd 0 is written explicitly (the
    capture of the info request has 08 00)."""
    return (eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic)
            + eh.encode_message_field(FIELD_FOR_CMD[cmd], inner))


def build_info_request(magic):
    """cmd 0 {3:{1:1}}: ask the glasses which layout file they have. -> 08 00 10 MM 1a 02 08 01."""
    return build_package(CMD_REQUEST_INFO, magic, eh.encode_varint_field(1, 1))


def build_upgrade_request(magic, value=1):
    """cmd 2 {5:{1:value}}: ask to upload a new file. value=1 is INFERRED."""
    return build_package(CMD_REQUEST_UPGRADE, magic, eh.encode_varint_field(1, value))


def build_file_data(magic, session_id, total_size, fragment_index, data, compress_mode=COMPRESS_RAW):
    """cmd 5 {8:{1 sessionId, 2 totalSize, 3 compressMode, 4 fragmentIndex, 5 packetSize, 6 data}}.
    Every field is written, even zeros, so the glasses see an explicit fragment index 0."""
    inner = (eh.encode_varint_field(1, session_id) + eh.encode_varint_field(2, total_size)
             + eh.encode_varint_field(3, compress_mode) + eh.encode_varint_field(4, fragment_index)
             + eh.encode_varint_field(5, len(data)) + eh.encode_bytes_field(6, data))
    return build_package(CMD_FILE_DATA, magic, inner)


def build_file_fragments(magic, file_bytes, session_id=1, fragment_size=MAX_FRAGMENT,
                         compress_mode=COMPRESS_RAW):
    """Cut a layout file into cmd 5 payloads. `magic` is an int or a callable (session.next_magic).
    Send one, wait for its cmd 6 ack (decode(...)['status'] == 0 presumably OK), then the next."""
    if len(file_bytes) > MAX_FILE_SIZE:
        raise ValueError(f'layout file is {len(file_bytes)} bytes; limit {MAX_FILE_SIZE}')
    next_magic = magic if callable(magic) else (lambda: magic)
    chunks = [file_bytes[i:i + fragment_size] for i in range(0, len(file_bytes), fragment_size)] or [b'']
    return [build_file_data(next_magic(), session_id, len(file_bytes), i, c, compress_mode)
            for i, c in enumerate(chunks)]


def build_transmit_start(magic, value=1):
    """cmd 4 {7:{1:value}}. The GLASSES send this; for offline tests only."""
    return build_package(CMD_TRANSMIT_START, magic, eh.encode_varint_field(1, value))


def build_update_success(magic, value=1):
    """cmd 7 {10:{1:value}}. The GLASSES send this; for offline tests only."""
    return build_package(CMD_UPDATE_SUCCESS, magic, eh.encode_varint_field(1, value))


# ---- the layout file ----

def encode_layout1(clock_type=0, left=COMP_NONE, right=COMP_NONE):
    """Layout1{1 clockType, 2 leftSelect, 3 rightSelect}: a clock with one extra on each side."""
    return (eh.encode_varint_field(1, clock_type) + eh.encode_varint_field(2, left)
            + eh.encode_varint_field(3, right))


def encode_layout3(align_index=0, date_en=1, temperature_en=1):
    """Layout3{1 alignIndex, 2 dateEn, 3 temperatureEn}: clock with optional date and temperature."""
    return (eh.encode_varint_field(1, align_index) + eh.encode_varint_field(2, date_en)
            + eh.encode_varint_field(3, temperature_en))


def build_layout_file(version, hash_code, layout_number, layout_bytes, pos=POS_CENTER,
                      widget_seq=(SEQ_NEWS, SEQ_SCHEDULE, SEQ_QUICKLIST)):
    """Serialize a DashboardExtLayoutConfigureFile. layout_number is 1-4 (which oneof member of
    watchfaceLayout), layout_bytes the encode_layoutN() result. widget_seq is packed (INFERRED).
    Field 5 does not exist in the schema; that is not a typo here."""
    out = eh.encode_string_field(1, version) + eh.encode_string_field(2, hash_code)
    out += eh.encode_varint_field(3, pos) + eh.encode_varint_field(4, len(widget_seq))
    if widget_seq:
        out += eh.encode_bytes_field(6, b''.join(eh.encode_varint(v) for v in widget_seq))
    out += eh.encode_message_field(7, eh.encode_message_field(layout_number, layout_bytes))
    return out


def build_temperature_layout(version='1', hash_code=None, style=1, right=COMP_SUNSET,
                             pos=POS_CENTER, widget_seq=(SEQ_NEWS, SEQ_SCHEDULE, SEQ_QUICKLIST)):
    """A layout that shows the weather temperature by the clock. DECODED, UNTESTED.
    style=1: Layout1 with leftSelect=TEMPERATURE (and `right` on the other side).
    style=3: Layout3 with temperatureEn=1 and the date on.
    hash_code defaults to a short hex digest of the content, so it changes when the content does
    (INFERRED semantics)."""
    if style == 1:
        number, layout = 1, encode_layout1(0, COMP_TEMPERATURE, right)
    elif style == 3:
        number, layout = 3, encode_layout3(0, 1, 1)
    else:
        raise ValueError('style must be 1 or 3')
    if hash_code is None:
        body = build_layout_file(version, '', number, layout, pos, widget_seq)
        hash_code = f'{eh.crc16_ccitt(body).hex()}'
    return build_layout_file(version, hash_code, number, layout, pos, widget_seq)


# ---- decoding ----

def decode_layout_file(data):
    """DashboardExtLayoutConfigureFile bytes -> dict (for tests and for diffing captures)."""
    out = {'version': eh.read_string_field(data, 1), 'hash': eh.read_string_field(data, 2),
           'pos': eh.read_varint_field(data, 3, 0), 'widget_count': eh.read_varint_field(data, 4, 0),
           'widget_seq': []}
    for f, w, v in eh._iter_fields(data):
        if f == 6 and w == 2:                   # packed
            i = 0
            while i < len(v):
                x, i = eh.decode_varint_at(v, i)
                out['widget_seq'].append(x)
        elif f == 6 and w == 0:                 # unpacked
            out['widget_seq'].append(v)
    wrapper = eh.read_bytes_field(data, 7)
    if wrapper:
        for f, w, v in eh._iter_fields(wrapper):
            if w == 2:
                out['layout'] = f
                out['layout_fields'] = {ff: vv for ff, ww, vv in eh._iter_fields(v) if ww == 0}
                break
    return out


def decode(pb):
    """Decode any DashboardExtPackage into {cmd, name, magic, ...}:
      1 INFO_RESPONSE  -> exists, version, hash
      3 UPGRADE_RESPONSE -> status
      4 TRANSMIT_START / 7 UPDATE_SUCCESS / 0 / 2 -> value
      5 FILE_DATA / 6 FILE_DATA_ACK -> session_id, total_size, compress_mode, fragment_index,
                                       packet_size, and data (5) or status (6)"""
    cmd = eh.read_varint_field(pb, 1, 0)
    out = {'cmd': cmd, 'name': CMD_NAMES.get(cmd, f'cmd={cmd}'), 'magic': eh.read_varint_field(pb, 2, -1)}
    sub = eh.read_bytes_field(pb, FIELD_FOR_CMD[cmd]) if cmd in FIELD_FOR_CMD else None
    if sub is None:
        return out
    if cmd == CMD_INFO_RESPONSE:
        out.update(exists=eh.read_varint_field(sub, 1, 0), version=eh.read_string_field(sub, 2),
                   hash=eh.read_string_field(sub, 3))
    elif cmd == CMD_UPGRADE_RESPONSE:
        out['status'] = eh.read_varint_field(sub, 1, 0)
    elif cmd in (CMD_FILE_DATA, CMD_FILE_DATA_ACK):
        out.update(session_id=eh.read_varint_field(sub, 1, 0), total_size=eh.read_varint_field(sub, 2, 0),
                   compress_mode=eh.read_varint_field(sub, 3, 0),
                   fragment_index=eh.read_varint_field(sub, 4, 0),
                   packet_size=eh.read_varint_field(sub, 5, 0))
        if cmd == CMD_FILE_DATA:
            out['data'] = eh.read_bytes_field(sub, 6) or b''
        else:
            out['status'] = eh.read_varint_field(sub, 6, 0)
    else:
        out['value'] = eh.read_varint_field(sub, 1, 0)
    return out
