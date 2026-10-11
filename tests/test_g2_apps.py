"""Offline tests for the native-app modules (Translate 0x05, Teleprompter 0x06, Navigation 0x08).
No glasses or Bluetooth needed.

Run from the repo root:  .venv/bin/python -m pytest tests      (or)   .venv/bin/python tests/test_g2_apps.py

"Golden" hex strings are the documented examples in esp32_ai/docs/research/native_apps_findings.md
(generated from the decompile with tools/native_apps_hexgen.py, magicRandom = 0x2a). They are
not live captures: if one fails, an encoder changed — check against the doc before "fixing" it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from g2 import evenhub, navigation, teleprompter, translate  # noqa: E402

MG = 0x2a


def h(s):
    return bytes.fromhex(s.replace(' ', ''))


# ── Translate (SID 0x05) ─────────────────────────────────────────────────────────────────────

def test_translate_open_matches_doc():
    assert translate.build_open(MG, 'en', 'es') == h('08 01 10 2a 1a 11 08 01 12 05 45 4e 3e 45 53 32 02 45 4e 3a 02 45 53')


def test_translate_text_matches_doc():
    assert translate.build_text(MG, 'Hello', 'Hola', final=True) == \
        h('08 02 10 2a 22 0f 0a 05 48 65 6c 6c 6f 12 04 48 6f 6c 61 20 01')


def test_translate_heartbeat_and_close_match_doc():
    assert translate.build_heartbeat(MG) == h('08 ff 01 10 2a 42 00')
    assert translate.build_close(MG) == h('08 01 10 2a 1a 02 08 02')


def test_translate_partial_pause_resume_and_lang_answer():
    sub = evenhub.read_bytes_field(translate.build_text(MG, ' hi ', ' ', final=False), 4)
    assert evenhub.read_bytes_field(sub, 1) == b'hi' and evenhub.read_bytes_field(sub, 2) == b' '
    assert evenhub.read_varint_field(sub, 4, 0) == 0
    assert translate.build_pause(MG).hex() == '0801102a1a020803'
    assert translate.build_resume(MG).hex() == '0801102a1a020804'
    assert translate.build_lang_switch_response(MG).hex() == '08a501102a5200'


def test_translate_decode_glasses_messages():
    assert translate.decode(h('08 a1 01 10 05 2a 02 08 02'))['label'] == 'NOTIFY CLOSE'
    rsp = translate.decode(h('08 a2 01 10 05 3a 02 08 42'))
    assert (rsp['error'], rsp['label']) == (66, 'COMM_RESP MAGIC_DUPLICATE')
    req = translate.decode(h('08 a4 01 10 05 4a 04 0a 02 46 52'))
    assert (req['lang_key'], req['label']) == ('FR', 'LANG_SWITCH_REQ FR')


# ── Teleprompter (SID 0x06) ──────────────────────────────────────────────────────────────────

def test_teleprompter_start_matches_doc():
    assert teleprompter.build_start(MG, total_pages=1, total_lines=3) == \
        h('08 01 10 2a 1a 0f 08 01 12 0b 08 01 20 01 28 03 30 b7 04 48 09')


def test_teleprompter_page0_matches_doc():
    pages = teleprompter.paginate('Line one\nLine two\nLine three')
    assert pages == [['Line one', 'Line two', 'Line three']]
    assert teleprompter.totals(pages) == (1, 3)
    assert teleprompter.build_page(MG, 0, pages[0]) == h(
        '08 03 10 2a 2a 22 10 03 1a 1e 4c 69 6e 65 20 6f 6e 65 0a 4c 69 6e 65 20 74 77 6f 0a'
        ' 4c 69 6e 65 20 74 68 72 65 65 20 0a')


def test_teleprompter_heartbeat_and_close_match_doc():
    assert teleprompter.build_heartbeat(MG) == h('08 ff 01 10 2a 6a 00')
    assert teleprompter.build_close(MG) == h('08 01 10 2a 1a 02 08 04')
    assert teleprompter.build_heartbeat(MG, 2, 5).hex() == '08ff01102a6a0408021005'
    assert teleprompter.build_scroll_sync(MG, 1, 3).hex() == '08a501102a5a0408011003'


def test_teleprompter_paginate_wraps_and_splits_pages():
    pages = teleprompter.paginate('word ' * 100, chars_per_line=28, lines_per_page=10)
    lines = [ln for p in pages for ln in p]
    assert all(len(ln) <= 28 for ln in lines) and all(len(p) <= 10 for p in pages)
    assert ' '.join(lines).split() == ['word'] * 100
    assert len(pages) == 2 and len(pages[0]) == 10
    assert all(len(ln) <= 28 for p in teleprompter.paginate('x' * 70) for ln in p)
    assert teleprompter.paginate('') == []


def test_teleprompter_file_list_truncates_names():
    pb = teleprompter.build_file_list(MG, [('f1', 'A' * 40)], menu_mode=teleprompter.MODE_MANUAL)
    fl = evenhub.read_bytes_field(pb, 4)
    info = evenhub.read_bytes_field(fl, 1)
    assert evenhub.read_bytes_field(info, 1) == b'f1' and evenhub.read_bytes_field(info, 2) == b'A' * 30
    assert evenhub.read_bytes_field(fl, 2) == b'\x08\x01'


def test_teleprompter_decode_glasses_messages():
    req = teleprompter.decode(h('08 a4 01 10 07 52 02 08 02'))
    assert (req['page'], req['label']) == (2, 'PAGE_DATA_REQUEST page=2')
    assert teleprompter.decode(h('08 a5 01 10 07 5a 04 08 01 10 04'))['label'] == 'PAGE_SCROLL_SYNC page=1 line=4'
    assert teleprompter.decode(h('08 a7 01 10 07 72 04 08 00 10 02'))['label'] == 'PAGE_LOAD_DONE pages=0..2'
    close = teleprompter.decode(h('08 a1 01 10 07 3a 02 08 04'))
    assert close['label'] == 'STATUS_NOTIFY CLOSE' and teleprompter.is_close(close)
    assert teleprompter.is_close(teleprompter.decode(h('08 aa 01 10 07 8a 01 00')))
    assert teleprompter.decode(h('08 a2 01 10 07 42 00'))['label'] == 'FILE_LIST_REQUEST'
    sel = teleprompter.decode(h('08 a3 01 10 07 4a 04 0a 02 66 31'))
    assert sel['file_id'] == b'f1' and sel['label'] == 'FILE_SELECT file=f1'


# ── Navigation (SID 0x08) ────────────────────────────────────────────────────────────────────

def test_navigation_start_heartbeat_stop_match_doc():
    assert navigation.build_start(MG) == h('08 05 10 2a 1a 00')
    assert navigation.build_heartbeat(MG) == h('08 00 10 2a 1a 00')
    assert navigation.build_stop(MG) == h('08 0c 10 2a 1a 00')


def test_navigation_turn_right_matches_doc():
    pb = navigation.build_basic_info(MG, navigation.TURN_RIGHT, '200 m', 'Main St', '5 min', '1.2 km',
                                     '14:35', '5 km/h', navigation.METHOD_WALKING, counter=1)
    assert pb == h('08 07 10 2a 2a 39 08 03 12 05 32 30 30 20 6d 1a 07 4d 61 69 6e 20 53 74 22 05 35'
                   ' 20 6d 69 6e 2a 06 31 2e 32 20 6b 6d 32 0a 45 54 41 3a 20 31 34 3a 33 35 3a 06'
                   ' 35 20 6b 6d 2f 68 40 01 50 01')
    assert navigation.build_basic_info(MG, 'turn right', '200 m', 'Main St', '5 min', '1.2 km',
                                       'ETA: 14:35', '5 km/h', counter=1) == pb


def test_navigation_direction_table_and_text_limits():
    assert len(navigation.DIRECTION) == 35
    assert sorted(navigation.DIRECTION.values()) == list(range(1, 36))
    assert (navigation.TURN_RIGHT, navigation.TURN_LEFT) == (3, 4)
    info = evenhub.read_bytes_field(navigation.build_basic_info(MG, 2, road='é' * 40), 5)
    assert evenhub.read_bytes_field(info, 3) == ('é' * 30).encode()          # 60 bytes, no split char
    assert evenhub.read_bytes_field(info, 2) == b'--'                         # missing → "--"
    assert navigation.build_recalculating(MG).hex() == '080a102a1a00'
    assert navigation.build_arrive(MG).hex() == '080b102a1a00'


def test_navigation_location_list():
    lst = evenhub.read_bytes_field(navigation.build_location_list(MG, ['Home', 'Work'], [1, 2]), 4)
    names = [v for f, w, v in evenhub._iter_fields(lst) if f == 2]
    assert evenhub.read_varint_field(lst, 1) == 2 and names == [b'Home', b'Work']


def test_navigation_decode_glasses_messages():
    ex = navigation.decode(h('08 0d 10 07'))
    assert ex['label'] == 'NOTIFY_EXIT' and navigation.is_exit(ex)
    assert navigation.is_exit(navigation.decode(h('08 14 10 07')))
    assert navigation.decode(h('08 01 10 07'))['label'] == 'MENU_STARTUP'
    sel = navigation.decode(h('08 04 10 07 42 0a 0a 04 48 6f 6d 65 18 01 20 02'))
    assert (sel['location'], sel['value'], sel['method']) == ('Home', 1, navigation.METHOD_CYCLING)
    rsp = navigation.decode(h('08 05 10 2a 1a 02 08 07'))
    assert (rsp['error'], rsp['label']) == (7, 'START errorCode=FAIL')


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
