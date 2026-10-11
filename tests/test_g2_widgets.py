"""Offline tests for the stocks, health and watchface modules and the EvenHub builder fixes.
No glasses or Bluetooth needed.

Run from the repo root:  .venv/bin/python -m pytest tests      (or)   .venv/bin/python tests/test_g2_widgets.py

The golden hex strings come from esp32_ai/docs/research/remaining_features.md, where they were
built by hand from the app's decompiled proto schema. None of them has been sent to the glasses.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from g2 import dashboard, evenhub, health, stocks, watchface  # noqa: E402


def h(s):
    return bytes.fromhex(s.replace(' ', ''))


def wire_types(pb):
    return {f: w for f, w, _ in evenhub._iter_fields(pb)}


# ── Stocks (SID 0x01 cmd 2) ──────────────────────────────────────────────────────────────────

def test_stock_push_matches_spec_hex():
    pb = stocks.build_stock_push(0x2a, {'code': 'AAPL', 'price': 190.5, 'change_percent': 1.25,
                                        'name': 'Apple'})
    assert pb == h('08 02 10 2a 22 23 1a 21 12 1f 12 1d 08 01 10 00 1a 17'
                   '0a 04 41 41 50 4c 25 00 00 a0 3f 2d 00 80 3e 43 32 05 41 70 70 6c 65')


def test_stock_float_and_double_wire_types():
    s = stocks.build_stock({'code': 'X', 'market_cap': 1e12, 'change_percent': -2, 'price': 3,
                            'day_high': 4, 'day_low': 2, 'open': 3, 'volume': 10**12,
                            'market_value': 5e11, 'pe_ratio': 20, 'trend': 1, 'dark_price': 2.5,
                            'points': [1, 2, 3]})
    wt = wire_types(s)
    for f in (4, 5, 7, 8, 9, 12, 15):
        assert wt[f] == 5, f           # float
    for f in (3, 11):
        assert wt[f] == 1, f           # double
    assert wt[10] == 0 and wt[13] == 0 and wt[14] == 0 and wt[16] == 2
    back = stocks.decode_stock(s)
    assert back['points'] == [1.0, 2.0, 3.0] and back['point_total'] == 3
    assert back['market_cap'] == 1e12 and back['volume'] == 10**12


def test_stock_packed_bright_price_key():
    assert stocks.encode_packed_float_field(16, [1.0]) == h('82 01 04 00 00 80 3f')


def test_stock_list_one_message_per_stock_zero_based():
    magics = iter([7, 8])
    pbs = stocks.build_stock_pushes(lambda: next(magics), [{'code': 'A'}, {'code': 'B'}])
    got = [stocks.decode_stock_push(pb) for pb in pbs]
    assert [(g['magic'], g['total'], g['num'], g['stock']['code']) for g in got] == \
        [(7, 2, 0, 'A'), (8, 2, 1, 'B')]


def test_stock_code_and_name_are_capped():
    back = stocks.decode_stock(stocks.build_stock({'code': 'C' * 100, 'name': 'N' * 200}))
    assert len(back['code']) == 62 and len(back['name']) == 126


def test_stock_display_enable_lists_widget_stock():
    pb = stocks.build_stock_display_enable(1)
    setting = evenhub.read_bytes_field(evenhub.read_bytes_field(pb, 4), 2)
    assert evenhub.read_bytes_field(setting, 5) == bytes([1, 3, 2, 2])
    # identical to the LIVE base-settings packet
    assert pb == dashboard.build_dashboard_init(1)
    try:
        stocks.build_stock_display_enable(1, widget_order=[dashboard.WIDGET_NEWS])
        raise AssertionError('expected ValueError')
    except ValueError:
        pass


# ── Health (SID 0x0e) ────────────────────────────────────────────────────────────────────────

def test_health_mult_matches_spec_hex():
    items = [health.build_single_data(health.TYPE_STEPS, 5234, goal=8000),
             health.build_single_data(health.TYPE_HEART_RATE, 72, avg_value=68)]
    assert health.build_mult_data(0x2a, items) == h(
        '08 02 10 2a 22 1c 08 01 12 0a 08 02 10 c0 3e 1d 00 90 a3 45'
        '12 0c 08 05 1d 00 00 90 42 25 00 00 88 42')


def test_health_snapshot_order_types_and_roundtrip():
    pb = health.build_dashboard_snapshot(5, steps=5234, step_goal=8000, calories=321,
                                         calorie_goal=500, sleep=7.5, sleep_minutes=450,
                                         heart_rate=72, heart_rate_avg=68, spo2=97, spo2_avg=96,
                                         productivity=80, productivity_trend=health.TREND_UP)
    d = health.decode(pb)
    assert (d['cmd'], d['name'], d['magic'], d['data_type']) == (2, 'MULT_DATA', 5, health.TYPE_ALL)
    assert [i['name'] for i in d['items']] == ['STEPS', 'CALORIES', 'SLEEP', 'HEART_RATE',
                                              'BLOOD_OXYGEN', 'PRODUCTIVITY']
    sleep = d['items'][2]
    assert (sleep['value'], sleep['duration']) == (7.5, 450)
    assert d['items'][5]['trend'] == health.TREND_UP
    # value / avgValue are FLOAT (wire type 5)
    sub = evenhub.read_bytes_field(evenhub.read_bytes_field(pb, 4), 2)
    assert wire_types(sub) == {1: 0, 2: 0, 3: 5}


def test_health_highlights_and_magic():
    d = health.decode(health.build_highlights(1, [(health.TYPE_SLEEP, 'Slept well')]))
    assert d['highlights'] == [(health.TYPE_SLEEP, 'Slept well')] and d['status'] == health.HL_HAS_ITEMS
    assert health.app_magic(now_us=1_234_567) == (1234 & 0xFF)


# ── Watchface (SID 0x1f) ─────────────────────────────────────────────────────────────────────

def test_watchface_info_request_matches_spec():
    assert watchface.build_info_request(0x2a) == h('08 00 10 2a 1a 02 08 01')


def test_watchface_layout_file_matches_spec_hex():
    body = watchface.build_layout_file('1', 'a', 1, watchface.encode_layout1(
        0, watchface.COMP_TEMPERATURE, watchface.COMP_SUNSET))
    assert body == h('0a 01 31 12 01 61 18 01 20 03 32 03 00 02 04 3a 08 0a 06 08 00 10 01 18 02')
    d = watchface.decode_layout_file(body)
    assert d['widget_seq'] == [0, 2, 4] and d['layout'] == 1 and d['layout_fields'] == {1: 0, 2: 1, 3: 2}


def test_watchface_temperature_layouts():
    d1 = watchface.decode_layout_file(watchface.build_temperature_layout())
    assert d1['layout'] == 1 and d1['layout_fields'][2] == watchface.COMP_TEMPERATURE and d1['hash']
    d3 = watchface.decode_layout_file(watchface.build_temperature_layout(style=3))
    assert d3['layout'] == 3 and d3['layout_fields'][3] == 1
    assert d1['hash'] != d3['hash']


def test_watchface_upgrade_fragments_and_decode():
    assert watchface.build_upgrade_request(0x2a) == h('08 02 10 2a 2a 02 08 01')
    frags = watchface.build_file_fragments(9, bytes(range(256)) * 10, session_id=3)   # 2560 bytes
    ds = [watchface.decode(f) for f in frags]
    assert [(d['cmd'], d['fragment_index'], d['packet_size']) for d in ds] == [(5, 0, 2047), (5, 1, 513)]
    assert all(d['total_size'] == 2560 and d['session_id'] == 3 for d in ds)
    assert b''.join(d['data'] for d in ds) == bytes(range(256)) * 10
    assert watchface.decode(watchface.build_transmit_start(1))['name'] == 'TRANSMIT_START'
    assert watchface.decode(watchface.build_update_success(1))['name'] == 'UPDATE_SUCCESS'
    ack = h('08 06 10 09 4a 0e 08 03 10 80 14 18 00 20 01 28 81 04 30 00')
    assert watchface.decode(ack)['status'] == 0 and watchface.decode(ack)['fragment_index'] == 1
    info = watchface.decode(h('08 01 10 01 22 08 08 01 12 01 31 1a 01 61'))
    assert (info['exists'], info['version'], info['hash']) == (1, '1', 'a')


# ── EvenHub corrections (SID 0xE0) ───────────────────────────────────────────────────────────

def test_rebuild_page_uses_field_7_and_no_varint_menu():
    text = evenhub.encode_text_object('t', 1, 0, 0, 576, 288, 'Hi', True)
    pb = evenhub.build_rebuild_page(0x2a, None, [text])
    assert pb == h('08 07 10 2a 3a 19 08 01 1a 15 08 00 10 00 18 c0 04 20 a0 02 48 01 52 01 74'
                   '58 01 62 02 48 69')
    inner = evenhub.read_bytes_field(pb, 7)
    assert 5 not in wire_types(inner) and evenhub.read_bytes_field(pb, 3) is None


def test_rebuild_page_with_image_objects():
    img = evenhub.encode_image_object('img', 2, 0, 0, 100, 50)
    inner = evenhub.read_bytes_field(evenhub.build_rebuild_page(1, None, [], [img]), 7)
    assert evenhub.read_varint_field(inner, 1) == 1 and evenhub.read_bytes_field(inner, 4) == img


def test_create_image_page_matches_spec_hex():
    pb = evenhub.build_create_image_page(0x2a, 'img', 2, 0, 0, 100, 50, session_id=1)
    assert pb == h('08 00 10 2a 1a 18 08 01 22 0f 08 00 10 00 18 64 20 32 28 02 32 03 69 6d 67'
                   '28 90 4e 38 01')


def test_image_raw_data_matches_spec_hex():
    pb = evenhub.build_image_raw_data(0x2a, 2, 'img', 4, 0, h('00 11 22 33'), map_session_id=1)
    assert pb == h('08 03 10 2a 2a 17 08 02 12 03 69 6d 67 18 01 20 04 28 00 30 00 38 04'
                   '42 04 00 11 22 33')
    other = evenhub.build_image_raw_data(1, 2, 'img', 4, 0, b'x', map_session_id=7)
    assert evenhub.read_varint_field(evenhub.read_bytes_field(other, 5), 3) == 7


def test_decode_long_press_on_text_container():
    ev = evenhub.decode_device_event(h('08 02 10 01 6a 09 12 07 08 01 12 01 74 18 09'))
    assert ev['type'] == 'text-click' and ev['container_id'] == 1 and ev['container_name'] == 't'
    assert ev['event_type'] == evenhub.EVENT_LONG_PRESS and ev['long_press'] and ev['event_name'] == 'LONG_PRESS'


def test_decode_list_event_index_zero_and_container_id():
    # list event with only container id 4 and name "m": index 0 and CLICK are omitted on the wire
    ev = evenhub.decode_device_event(h('08 02 10 01 6a 07 0a 05 08 04 12 01 6d'))
    assert (ev['type'], ev['container_id'], ev['item_index'], ev['event_type'], ev['long_press']) == \
        ('list-click', 4, 0, 0, False)


def test_decode_sys_event_widget_and_session():
    inner = (evenhub.encode_varint_field(1, evenhub.EVENT_FOREGROUND_EXIT)
             + evenhub.encode_varint_field(5, 10000) + evenhub.encode_varint_field(6, 3))
    pb = evenhub.wrap_even_hub(2, 1, 13, evenhub.encode_message_field(3, inner))
    ev = evenhub.decode_device_event(pb)
    assert (ev['type'], ev['event_name'], ev['widget_id'], ev['session_id']) == \
        ('sys-event', 'FOREGROUND_EXIT', 10000, 3)
    imu = evenhub.encode_message_field(3, b''.join(evenhub.encode_key(i, 5) + struct.pack('<f', v)
                                                   for i, v in ((1, 1.0), (2, 2.0), (3, 3.0))))
    ev = evenhub.decode_device_event(evenhub.wrap_even_hub(2, 1, 13, evenhub.encode_message_field(
        3, evenhub.encode_varint_field(1, 8) + imu)))
    assert ev['imu'] == {'x': 1.0, 'y': 2.0, 'z': 3.0}


def test_decode_menu_events_and_ignores_heartbeat():
    assert evenhub.decode_device_event(evenhub.wrap_even_hub(21, 1, 24, evenhub.encode_varint_field(1, 5))) == \
        {'type': 'menu-item-click', 'item_id': 5}
    assert evenhub.decode_device_event(evenhub.wrap_even_hub(17, 1, 20, evenhub.encode_varint_field(1, 9))) == \
        {'type': 'menu-startup', 'widget_id': 9}
    assert evenhub.decode_device_event(evenhub.build_heartbeat(3)) is None
    assert evenhub.decode_device_event(evenhub.wrap_even_hub(13, 3, 15, evenhub.encode_varint_field(1, 0))) is None


def test_decode_page_acks():
    assert evenhub.decode_page_ack(evenhub.wrap_even_hub(8, 1, 8, evenhub.encode_varint_field(1, 6)))['ok']
    img_ack = evenhub.wrap_even_hub(4, 1, 6, evenhub.encode_varint_field(3, 1) + evenhub.encode_varint_field(8, 5))
    a = evenhub.decode_page_ack(img_ack)
    assert (a['ok'], a['result'], a['map_session_id']) == (False, 5, 1)
    assert evenhub.decode_page_ack(evenhub.build_heartbeat(1)) is None


def test_calendar_push_omits_display_setting_by_default():
    """The app never sends DashboardDisplaySetting with calendar content (resolved 2026-10-01),
    so by default a calendar push must not reset the main widget / widget order."""
    ev = ('Standup', 'Room 1', '09:00-09:15', 1790000000)
    plain = dashboard.build_calendar_event_push(7, ev, 1, 0)
    with_order = dashboard.build_calendar_event_push(7, ev, 1, 0, set_display_order=True)
    setting = dashboard.encode_dashboard_display_setting([dashboard.WIDGET_SCHEDULE])
    assert setting not in plain and setting in with_order
    assert len(with_order) > len(plain)


def test_navigation_short_direction_aliases():
    from g2 import navigation as nav
    assert nav.direction_index('right') == nav.TURN_RIGHT
    assert nav.direction_index('left') == nav.TURN_LEFT
    assert nav.direction_index('turn right') == nav.TURN_RIGHT
    assert nav.direction_index('straight') == nav.STRAIGHT


if __name__ == '__main__':
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'PASS {name}')
        except Exception as e:
            failed += 1
            print(f'FAIL {name}: {e!r}')
    print(f'{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)
