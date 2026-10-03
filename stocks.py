"""Stocks widget on the native dashboard (SID 0x01, cmd 2): one stock quote per message.

Plain English: the dashboard's widget carousel has a "stocks" card. The phone fills it by
pushing quotes, ONE stock per message, each tagged "this is stock #n of N". The glasses never
fetch prices themselves. This module builds those messages.

Path inside the normal dashboard envelope (g2.dashboard builds the outer layers):

    DashboardDataPackage{1 cmd=2, 2 magic,
      4 dashboardReceive{3 bashboardConfig=DashboardContent{
        2 widgetComponents=rWidgetComponent{
          2 stock=rStockWidget{1 stockTotal, 2 stockNum, 3 stock=Stock}}}}}

Stock fields (numbers and types from the app's generated BuilderInfo, CONFIRMED):

     1 stockCode            string   (the app caps it at 62 characters)
     2 marketSuffix         string   (e.g. "US")
     3 marketCap            DOUBLE   wire type 1 (the app never sets it)
     4 priceChangePercent   float    wire type 5 (1.25 means +1.25 %)
     5 currentPrice         float
     6 companyName          string   (capped at 126 characters)
     7 dayHigh              float
     8 dayLow               float
     9 openPrice            float
    10 volume               uint64   varint
    11 marketValue          DOUBLE   wire type 1
    12 peRatio              float
    13 changingTrend        uint32   (0 flat / 1 up / 2 down: INFERRED)
    14 pointTotal           uint32   number of chart points (= len(brightPrice), INFERRED)
    15 darkPrice            float    previous close / chart baseline (INFERRED)
    16 brightPrice          repeated float, packed (intraday chart points, INFERRED)

Float vs double matters: a double where a float belongs (or the reverse) makes the glasses drop
the WHOLE message silently -- the same failure seen earlier with the weather temperature.

Rules
  * One Stock per message (rStockWidget.stock is not repeated). CONFIRMED.
  * stockNum is the 0-based index of this stock, stockTotal the count. INFERRED (by symmetry with
    scheduleNum, which a capture proved is a 0-based start index). Test 0-based first.
  * The widget only shows if WIDGET_STOCK (2) is in DashboardDisplaySetting.widgetDisplayOrder.
    dashboard.build_dashboard_init() already lists it ([NEWS, SCHEDULE, STOCK, STOCK]), so send
    that first; or send build_stock_display_enable() here.
  * Same preconditions as every other dashboard push: see g2/dashboard.py (heartbeat running,
    base settings sent). The ack is DashboardDataPackage field 3, flag 0 = OK.

Sending example:
    for pb in stocks.build_stock_pushes(session.next_magic, [{'code': 'AAPL', 'price': 190.5,
                                                              'change_percent': 1.25}]):
        ... frame with evenhub.frame_pb(pb, stocks.SID, evenhub.FLAG_REQUEST, seq) and send ...

Status legend (as in g2/device.py):
  LIVE      seen working from our own hub
  CAPTURED  byte-for-byte what the official app sends
  DECODED   from the decompiled app's schema only; never sent by us or seen in a capture

  build_stock / build_stock_push / build_stock_pushes   DECODED (never sent; golden test
                                                        matches the hand-built spec hex)
  build_stock_display_enable                            DECODED wrapper around the LIVE
                                                        dashboard.build_display_enable
"""
import struct

from . import dashboard as db
from . import evenhub as eh

SID = db.SID_DASHBOARD            # 0x01
CMD = db.CMD_DASHBOARD_RECEIVE    # 2
WIDGET_STOCK = db.WIDGET_STOCK    # 2, the widgetDisplayOrder entry that enables the card
DISPLAY_MODE_STOCK = 2            # DashboardMainWidgetMode index (unknowns_findings.md section 1)

MAX_CODE_CHARS = 62
MAX_NAME_CHARS = 126

TREND_FLAT = 0     # INFERRED
TREND_UP = 1       # INFERRED
TREND_DOWN = 2     # INFERRED

# Stock field numbers
F_CODE = 1
F_MARKET_SUFFIX = 2
F_MARKET_CAP = 3
F_CHANGE_PERCENT = 4
F_PRICE = 5
F_NAME = 6
F_DAY_HIGH = 7
F_DAY_LOW = 8
F_OPEN = 9
F_VOLUME = 10
F_MARKET_VALUE = 11
F_PE_RATIO = 12
F_TREND = 13
F_POINT_TOTAL = 14
F_DARK_PRICE = 15
F_BRIGHT_PRICE = 16

# dict key -> (field number, kind). Kinds: str, float (wire 5), double (wire 1), varint.
# Encoded in field-number order; a key that is missing or None is left out.
STOCK_FIELDS = [
    ('code', F_CODE, 'str'),
    ('market_suffix', F_MARKET_SUFFIX, 'str'),
    ('market_cap', F_MARKET_CAP, 'double'),
    ('change_percent', F_CHANGE_PERCENT, 'float'),
    ('price', F_PRICE, 'float'),
    ('name', F_NAME, 'str'),
    ('day_high', F_DAY_HIGH, 'float'),
    ('day_low', F_DAY_LOW, 'float'),
    ('open', F_OPEN, 'float'),
    ('volume', F_VOLUME, 'varint'),
    ('market_value', F_MARKET_VALUE, 'double'),
    ('pe_ratio', F_PE_RATIO, 'float'),
    ('trend', F_TREND, 'varint'),
    ('point_total', F_POINT_TOTAL, 'varint'),
    ('dark_price', F_DARK_PRICE, 'float'),
]


def encode_packed_float_field(field_number, values):
    """Packed repeated float: one length-delimited field holding n x 4-byte little-endian floats.
    encode_packed_float_field(16, [1.0]) -> 82 01 04 00 00 80 3f."""
    return eh.encode_bytes_field(field_number, b''.join(struct.pack('<f', float(v)) for v in values))


def build_stock(stock):
    """Encode one Stock message from a dict. Keys (all optional except 'code'):
    code, market_suffix, market_cap, change_percent, price, name, day_high, day_low, open,
    volume, market_value, pe_ratio, trend, point_total, dark_price, points (list of floats).
    If 'points' is given and 'point_total' is not, point_total = len(points). Strings longer than
    the app's caps are truncated the same way the app does (by characters). DECODED."""
    s = dict(stock)
    if s.get('points') and s.get('point_total') is None:
        s['point_total'] = len(s['points'])
    out = b''
    for key, field, kind in STOCK_FIELDS:
        v = s.get(key)
        if v is None:
            continue
        if kind == 'str':
            cap = MAX_NAME_CHARS if key == 'name' else MAX_CODE_CHARS
            out += eh.encode_string_field(field, str(v)[:cap])
        elif kind == 'float':
            out += db.encode_float_field(field, v)
        elif kind == 'double':
            out += db.encode_double_field(field, v)
        else:
            out += eh.encode_varint_field(field, int(v))
    if s.get('points'):
        out += encode_packed_float_field(F_BRIGHT_PRICE, s['points'])
    return out


def encode_stock_widget(stock_bytes, stock_total, stock_num):
    """rStockWidget{1 stockTotal, 2 stockNum, 3 stock}. Both counters are always written, even
    when 0, to match the spec's hand-built example."""
    return (eh.encode_varint_field(1, stock_total) + eh.encode_varint_field(2, stock_num)
            + eh.encode_message_field(3, stock_bytes))


def build_stock_push(magic, stock, stock_total=1, stock_num=0, package_id=None):
    """Full SID 0x01 payload carrying ONE stock. `stock` is a dict for build_stock() (or already
    encoded Stock bytes). stock_num is the 0-based index (INFERRED), stock_total the count.
    package_id: the spec leaves it unset; the LIVE calendar push uses 1. DECODED."""
    stock_bytes = stock if isinstance(stock, (bytes, bytearray)) else build_stock(stock)
    widget = encode_stock_widget(bytes(stock_bytes), stock_total, stock_num)
    component = eh.encode_message_field(2, widget)          # rWidgetComponent.stock
    content = db.encode_dashboard_content_widget(component)
    receive = db.encode_dashboard_receive_from_app(package_id, content)
    return db.encode_dashboard_data_package(CMD, magic, receive)


def build_stock_pushes(magic, stocks, package_id=None):
    """One payload per stock, in order: stockTotal = len(stocks), stockNum = 0, 1, 2 ...
    `magic` is either an int (the same value is used for each; fine offline, but use a fresh
    magic per packet on the glasses) or a zero-argument callable such as session.next_magic.
    Returns a list of protobuf payloads. Send them one at a time, waiting for each ack."""
    next_magic = magic if callable(magic) else (lambda: magic)
    total = len(stocks)
    return [build_stock_push(next_magic(), s, total, i, package_id) for i, s in enumerate(stocks)]


def build_stock_display_enable(magic, widget_order=None, package_id=1, display_mode=4):
    """Base-settings message that makes the stock card visible: widgetDisplayOrder must contain
    WIDGET_STOCK (2). Defaults = the app's own base settings (order [NEWS, SCHEDULE, STOCK, STOCK],
    displayMode 4 = news main; byte-identical to dashboard.build_dashboard_init). Pass
    display_mode=DISPLAY_MODE_STOCK to make stocks the main widget (INFERRED value)."""
    order = widget_order or [db.WIDGET_NEWS, db.WIDGET_SCHEDULE, WIDGET_STOCK, WIDGET_STOCK]
    if WIDGET_STOCK not in order:
        raise ValueError('widget_order must contain WIDGET_STOCK (2) or the card never shows')
    setting = db.encode_dashboard_display_setting(order, status_display_order=[1, 2, 3],
                                                  display_mode=display_mode, half_day_format=1,
                                                  temperature_unit=db.TEMP_UNIT_CELSIUS)
    receive = db.encode_dashboard_receive_from_app(package_id, b'', setting)
    return db.encode_dashboard_data_package(CMD, magic, receive)


def decode_stock(stock_bytes):
    """Inverse of build_stock (for tests and debugging): Stock bytes -> dict with the same keys.
    Floats come back at float32 precision."""
    by_field = {f: (k, kind) for k, f, kind in STOCK_FIELDS}
    out = {}
    for f, wire, v in eh._iter_fields(stock_bytes):
        if f == F_BRIGHT_PRICE and wire == 2:
            out['points'] = [x[0] for x in struct.iter_unpack('<f', v)]
            continue
        if f not in by_field:
            continue
        key, kind = by_field[f]
        if kind == 'str' and wire == 2:
            out[key] = v.decode('utf-8', 'replace')
        elif kind == 'float' and wire == 5:
            out[key] = struct.unpack('<f', v)[0]
        elif kind == 'double' and wire == 1:
            out[key] = struct.unpack('<d', v)[0]
        elif kind == 'varint' and wire == 0:
            out[key] = v
    return out


def decode_stock_push(pb):
    """Unwrap a build_stock_push payload -> {'magic', 'total', 'num', 'stock': {...}}, or None."""
    receive = eh.read_bytes_field(pb, 4)
    content = receive and eh.read_bytes_field(receive, 3)
    component = content and eh.read_bytes_field(content, 2)
    widget = component and eh.read_bytes_field(component, 2)
    if widget is None:
        return None
    return {'magic': eh.read_varint_field(pb, 2, -1),
            'total': eh.read_varint_field(widget, 1, 0),
            'num': eh.read_varint_field(widget, 2, 0),
            'stock': decode_stock(eh.read_bytes_field(widget, 3) or b'')}
