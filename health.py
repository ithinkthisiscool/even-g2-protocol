"""Health data (SID 0x0e): steps, calories, sleep, heart rate, SpO2 and productivity for the
dashboard's health card and the watchface health slots.

Plain English: the glasses have no step counter or heart-rate sensor of their own that the
dashboard reads. The phone (from a ring or a phone health app) pushes a "snapshot" of today's
numbers, and the glasses draw them. This module builds that snapshot and decodes it back.

Every message is a `HealthDataPackage` protobuf (schema CONFIRMED, fields_health.txt):
    1 commandId   CMD_* below
    2 magicRandom request id (the app uses the low byte of its millisecond clock)
    3..6         one sub-message; the field number is cmd + 2 (FIELD_FOR_CMD)

    HealthSingleData{1 dataType, 2 goal u32, 3 value FLOAT, 4 avgValue FLOAT, 5 duration u32,
                     6 errorCode, 7 trend}
    HealthMultData{1 dataType, 2 dataSet[] HealthSingleData, 3 errorCode}
    HealthSingleHighlight{1 dataType, 2 text bytes, 3 errorCode}
    HealthMultHighlight{1 Highlight[], 2 errorCode, 3 status}

value and avgValue are protobuf FLOAT (wire type 5), not double -- getting that wrong makes the
glasses drop the message.

The dashboard snapshot (ProtoHealthExt.sendDashboardSnapshot -> sendHealthMult, CONFIRMED
structure) is one MULT_DATA message with six entries in this order:

    #  dataType       goal         value                     avgValue     duration
    1  STEPS          step goal    steps (count)             -            -
    2  CALORIES       kcal goal    calories (kcal)           -            -
    3  SLEEP          0            sleep value (see below)   -            sleep minutes
    4  HEART_RATE     0            latest bpm                average bpm  -
    5  BLOOD_OXYGEN   0            SpO2 %                    average %    -
    6  PRODUCTIVITY   0            productivity score        -            -   (+ trend arg)

The type-to-row mapping is INFERRED from the order and names. Units are INFERRED: steps = count,
calories = kcal, heart rate = bpm, SpO2 = percent, duration = minutes. The sleep value's unit is
UNKNOWN (a 0-100 score or hours; test 7.5 and 85). The app keeps at most 8 entries and sets the
outer dataType to one fixed value, INFERRED to be ALL (1).

Showing it: rWidgetComponent has no health slot; health data travels only on this SID. The card
appears when WIDGET_HEALTH (5) is in the dashboard's widgetDisplayOrder (INFERRED that this alone
is enough; see dashboard.build_display_enable([dashboard.WIDGET_HEALTH])). Watchface slots
HEARTRATE / STEPS / CALORIES / BLOOD_OXYGEN / SLEEP (g2.watchface) read the same data.

Status legend (as in g2/device.py): LIVE / CAPTURED / DECODED.
  build_single_data, build_mult_data, build_dashboard_snapshot   DECODED (never sent)
  build_highlights                                               DECODED
  decode                                                         DECODED (round-trips our builders)
"""
import struct
import time as _time

from . import evenhub as eh

SID = 0x0e

# eHealthCommandId
CMD_SINGLE_DATA = 1
CMD_MULT_DATA = 2
CMD_SINGLE_HIGHLIGHT = 3
CMD_MULT_HIGHLIGHT = 4
CMD_NAMES = {CMD_SINGLE_DATA: 'SINGLE_DATA', CMD_MULT_DATA: 'MULT_DATA',
             CMD_SINGLE_HIGHLIGHT: 'SINGLE_HIGHLIGHT', CMD_MULT_HIGHLIGHT: 'MULT_HIGHLIGHT'}
FIELD_FOR_CMD = {c: c + 2 for c in CMD_NAMES}

# eHealthDataType
TYPE_ALL = 1
TYPE_STEPS = 2
TYPE_CALORIES = 3
TYPE_SLEEP = 4
TYPE_HEART_RATE = 5
TYPE_BLOOD_OXYGEN = 6
TYPE_TEMPERATURE = 7
TYPE_HRV = 8
TYPE_PRODUCTIVITY = 9
TYPE_NAMES = {TYPE_ALL: 'ALL', TYPE_STEPS: 'STEPS', TYPE_CALORIES: 'CALORIES', TYPE_SLEEP: 'SLEEP',
              TYPE_HEART_RATE: 'HEART_RATE', TYPE_BLOOD_OXYGEN: 'BLOOD_OXYGEN',
              TYPE_TEMPERATURE: 'TEMPERATURE', TYPE_HRV: 'HRV', TYPE_PRODUCTIVITY: 'PRODUCTIVITY'}

# eHealthDataTrend
TREND_UNKNOWN = 0
TREND_UP = 1
TREND_STEADY = 2
TREND_DOWN = 3

# eHealthHighlightStatus
HL_HAS_ITEMS = 1
HL_ALL_NORMAL = 2
HL_NO_DATA = 3

ERROR_SUCCESS = 0          # eErrorCode.SUCCESS (proto3 leaves 0 out on the wire)
MAX_ITEMS = 8              # sendHealthMult keeps at most 8 entries (CONFIRMED)


def app_magic(now_us=None):
    """The magic the app uses for health packets: (microseconds // 1000) & 0xff (CONFIRMED)."""
    us = int(_time.time() * 1_000_000) if now_us is None else now_us
    return (us // 1000) & 0xFF


def _float(field, value):
    return eh.encode_key(field, 5) + struct.pack('<f', float(value))


def build_single_data(data_type, value=None, goal=None, avg_value=None, duration=None,
                      trend=None, error_code=None):
    """Encode one HealthSingleData. Fields that are None (or 0 for goal/duration/trend/error)
    are left out, as proto3 does. value/avg_value are FLOATs."""
    out = eh.encode_varint_field(1, data_type)
    if goal:
        out += eh.encode_varint_field(2, int(goal))
    if value is not None:
        out += _float(3, value)
    if avg_value is not None:
        out += _float(4, avg_value)
    if duration:
        out += eh.encode_varint_field(5, int(duration))
    if error_code:
        out += eh.encode_varint_field(6, error_code)
    if trend:
        out += eh.encode_varint_field(7, trend)
    return out


def build_package(cmd, magic, inner):
    """HealthDataPackage{1 cmd, 2 magic, <cmd + 2>: inner}."""
    return (eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic)
            + eh.encode_message_field(FIELD_FOR_CMD[cmd], inner))


def build_mult_data(magic, items, data_type=TYPE_ALL):
    """MULT_DATA (cmd 2, field 4): `items` are build_single_data() results; at most 8 are kept,
    like the app. DECODED."""
    inner = eh.encode_varint_field(1, data_type)
    for item in items[:MAX_ITEMS]:
        inner += eh.encode_message_field(2, item)
    return build_package(CMD_MULT_DATA, magic, inner)


def build_dashboard_snapshot(magic, steps=None, step_goal=None, calories=None, calorie_goal=None,
                             sleep=None, sleep_minutes=None, heart_rate=None, heart_rate_avg=None,
                             spo2=None, spo2_avg=None, productivity=None,
                             productivity_trend=None):
    """The app's dashboard snapshot: one MULT_DATA with up to six entries in the app's order
    (STEPS, CALORIES, SLEEP, HEART_RATE, BLOOD_OXYGEN, PRODUCTIVITY). A metric whose value is
    None is skipped (the app always sends all six; sending 0 instead is also fine). Units
    (INFERRED): steps count, kcal, sleep = UNKNOWN unit (score or hours) + minutes, bpm, SpO2 %,
    productivity score. DECODED -- never sent."""
    items = []
    if steps is not None:
        items.append(build_single_data(TYPE_STEPS, steps, goal=step_goal))
    if calories is not None:
        items.append(build_single_data(TYPE_CALORIES, calories, goal=calorie_goal))
    if sleep is not None or sleep_minutes:
        items.append(build_single_data(TYPE_SLEEP, sleep or 0, duration=sleep_minutes))
    if heart_rate is not None:
        items.append(build_single_data(TYPE_HEART_RATE, heart_rate, avg_value=heart_rate_avg))
    if spo2 is not None:
        items.append(build_single_data(TYPE_BLOOD_OXYGEN, spo2, avg_value=spo2_avg))
    if productivity is not None:
        items.append(build_single_data(TYPE_PRODUCTIVITY, productivity, trend=productivity_trend))
    return build_mult_data(magic, items)


def build_highlights(magic, highlights, status=None):
    """MULT_HIGHLIGHT (cmd 4, field 6), the "insights" page: highlights = [(data_type, text), ...].
    status defaults to HL_HAS_ITEMS if there are any, else HL_NO_DATA. DECODED."""
    inner = b''
    for data_type, text in highlights:
        raw = text.encode('utf-8') if isinstance(text, str) else bytes(text)
        inner += eh.encode_message_field(1, eh.encode_varint_field(1, data_type) + eh.encode_bytes_field(2, raw))
    if status is None:
        status = HL_HAS_ITEMS if highlights else HL_NO_DATA
    inner += eh.encode_varint_field(3, status)
    return build_package(CMD_MULT_HIGHLIGHT, magic, inner)


def decode_single_data(sub):
    """HealthSingleData bytes -> dict {type, name, goal, value, avg_value, duration, error, trend}."""
    t = eh.read_varint_field(sub, 1, 0)
    return {'type': t, 'name': TYPE_NAMES.get(t, f'type={t}'),
            'goal': eh.read_varint_field(sub, 2, 0),
            'value': eh.read_float_field(sub, 3, 0.0),
            'avg_value': eh.read_float_field(sub, 4, 0.0),
            'duration': eh.read_varint_field(sub, 5, 0),
            'error': eh.read_varint_field(sub, 6, 0),
            'trend': eh.read_varint_field(sub, 7, 0)}


def decode(pb):
    """Decode any HealthDataPackage into {cmd, name, magic, ...}:
      SINGLE_DATA    -> data = {...}
      MULT_DATA      -> data_type, items = [{...}, ...], error
      SINGLE_HIGHLIGHT / MULT_HIGHLIGHT -> highlights = [(type, text)], status, error"""
    cmd = eh.read_varint_field(pb, 1, 0)
    out = {'cmd': cmd, 'name': CMD_NAMES.get(cmd, f'cmd={cmd}'), 'magic': eh.read_varint_field(pb, 2, -1)}
    sub = eh.read_bytes_field(pb, FIELD_FOR_CMD[cmd]) if cmd in FIELD_FOR_CMD else None
    if sub is None:
        return out
    if cmd == CMD_SINGLE_DATA:
        out['data'] = decode_single_data(sub)
    elif cmd == CMD_MULT_DATA:
        out['data_type'] = eh.read_varint_field(sub, 1, 0)
        out['items'] = [decode_single_data(v) for f, w, v in eh._iter_fields(sub) if f == 2 and w == 2]
        out['error'] = eh.read_varint_field(sub, 3, 0)
    elif cmd == CMD_SINGLE_HIGHLIGHT:
        out['highlights'] = [(eh.read_varint_field(sub, 1, 0), eh.read_string_field(sub, 2))]
        out['error'] = eh.read_varint_field(sub, 3, 0)
    else:
        out['highlights'] = [(eh.read_varint_field(v, 1, 0), eh.read_string_field(v, 2))
                             for f, w, v in eh._iter_fields(sub) if f == 1 and w == 2]
        out['error'] = eh.read_varint_field(sub, 2, 0)
        out['status'] = eh.read_varint_field(sub, 3, 0)
    return out
