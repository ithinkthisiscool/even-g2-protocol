"""Offline tests for g2.device (SID 0x80/0x10/0x81) and g2.settings (SID 0x09/0x20).
No glasses or Bluetooth needed.

Run from the repo root:  .venv/bin/python tests/test_g2_device_settings.py   (or under pytest)

Golden frames below are copied byte-for-byte from the official app's real Bluetooth captures
(docs/captures/scratch-0924-0928/real_btsnoop_hci{,2,3}.log; decode with
tools/btsnoop_frames.py). Each is a whole frame: AA 21 seq len total num sid flag | payload | CRC.
The app uses the same number for the frame seq and the protobuf magic, so frame_pb(pb, sid, flag,
magic) must reproduce the frame exactly, CRC included. Values that naturally differ between runs
(magic, clock) are substituted from the capture into the builder call.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from g2 import device, evenhub, settings  # noqa: E402


def _frame(pb, sid, magic):
    frames = evenhub.frame_pb(pb, sid, device.flag_for(sid, pb), magic)
    assert len(frames) == 1
    return frames[0].hex(' ')


def _payload(frame_hex):
    """Protobuf payload of a captured single-packet frame (drop 8-byte header and 2-byte CRC)."""
    b = bytes.fromhex(frame_hex)
    return b[8:8 + b[3] - 2]


# ── captured frames (app → glasses unless noted) ─────────────────────────────────────────────
CAP_AUTH_R = 'aa 21 36 0c 01 01 80 00 08 04 10 36 1a 04 08 01 10 04 05 16'
CAP_ROLE = 'aa 21 37 0a 01 01 80 20 08 05 10 37 22 02 08 01 33 0a'
CAP_TIME = 'aa 21 38 12 01 01 80 20 08 80 01 10 38 82 08 08 08 88 d8 d7 d5 06 10 00 66 30'
CAP_TIME_CAP3 = 'aa 21 b2 13 01 01 80 20 08 80 01 10 b2 01 82 08 08 08 9c ea d7 d5 06 10 00 4e 45'
CAP_KEEPALIVE_L = 'aa 21 59 08 01 01 80 00 08 0e 10 59 6a 00 e3 f3'
CAP_KEEPALIVE_R_BIG_MAGIC = 'aa 21 81 09 01 01 80 00 08 0e 10 81 01 6a 00 c7 9e'
CAP_READ = 'aa 21 3b 0a 01 01 09 20 08 02 10 3b 22 02 08 01 5c 98'
CAP_UNITS = 'aa 21 3c 14 01 01 09 20 08 01 10 3c 1a 0c 4a 0a 08 00 10 00 18 01 20 00 28 01 9b 9d'
CAP_FINISH = 'aa 21 40 0a 01 01 10 20 08 01 10 40 1a 02 08 04 28 48'
CAP_WEAR = 'aa 21 41 0a 01 01 10 20 08 03 10 41 2a 02 08 01 d6 fe'
CAP_CASE = 'aa 21 46 08 01 01 81 20 08 01 10 46 1a 00 11 f1'
CAP_GESTURES = ('aa 21 4c 22 01 01 09 20 08 01 10 4c 1a 1a 52 18 0a 06 08 00 10 00 18 00 0a 06 08 00 '
                '10 01 18 00 0a 06 08 00 10 02 18 00 3e 88')
CAP_AI_CONFIG = 'aa 21 3e 0e 01 01 07 20 08 0a 10 3e 6a 06 08 00 10 50 20 00 a3 7d'
CAP_QUICKLIST = 'aa 21 3f 0c 01 01 0c 20 08 02 10 3f 22 04 08 01 10 00 0b 05'
CAP_SYNC = 'aa 21 3a 06 01 01 0d 20 08 00 10 3a 69 95'
CAP_WATCHFACE = 'aa 21 42 0a 01 01 1f 20 08 00 10 42 1a 02 08 01 6f e4'
CAP_NOTIF = 'aa 21 47 10 01 01 04 20 08 01 10 47 1a 08 08 01 10 00 18 05 28 00 16 32'

# glasses → app
RX_AUTH_PUSH = 'aa 12 52 0a 01 01 80 01 08 04 10 52 1a 02 08 01 f3 21'
RX_ROLE_ACK = 'aa 12 97 08 01 01 80 00 08 05 10 37 22 00 13 1b'
RX_TIME_ACK = 'aa 12 c7 0a 01 01 80 00 08 80 01 10 38 82 08 00 5e b0'
RX_KEEPALIVE = 'aa 12 2d 08 01 01 80 00 08 0e 10 59 6a 00 e3 f3'
RX_FINISH = 'aa 12 31 0a 01 01 10 00 08 01 10 40 1a 02 08 04 28 48'
RX_WEAR = 'aa 12 7d 0c 01 01 10 00 08 03 10 41 2a 04 08 01 10 01 a2 45'
RX_CASE = 'aa 12 2b 0a 01 01 81 00 08 01 10 46 1a 02 08 46 2b ed'
RX_CASE_PUSH = 'aa 12 aa 0e 01 01 81 01 08 01 10 40 1a 06 08 46 18 01 20 01 86 ef'
RX_READ = ('aa 12 b6 2e 01 01 09 00 08 02 10 3b 22 26 08 01 10 64 18 06 2a 08 32 2e 33 2e 30 2e 32 34 '
           '32 08 32 2e 33 2e 30 2e 32 34 40 1e 50 01 60 64 90 01 01 98 01 01 ae 48')
RX_PUSH_CHARGING = ('aa 12 ec 2f 01 01 09 01 08 02 10 a6 01 22 26 10 64 18 06 2a 08 32 2e 33 2e 30 2e 32 '
                    '34 32 08 32 2e 33 2e 30 2e 32 34 40 1e 50 01 60 64 68 01 90 01 01 98 01 01 47 54')
RX_UNITS_ACK = 'aa 12 03 0e 01 01 09 00 08 01 10 3c 1a 06 4a 04 18 01 28 01 5f cc'
RX_GESTURES_ACK = 'aa 12 bc 14 01 01 09 00 08 01 10 4c 1a 0c 52 0a 0a 00 0a 02 10 01 0a 02 10 02 cd da'


# ── device (SID 0x80 / 0x10 / 0x81): builders vs captures ────────────────────────────────────

def test_auth_matches_capture():
    assert _frame(device.build_auth(0x36), device.SID, 0x36) == CAP_AUTH_R


def test_role_change_matches_capture():
    assert _frame(device.build_role_change(0x37), device.SID, 0x37) == CAP_ROLE


def test_time_sync_matches_both_captures():
    assert _frame(device.build_time_sync(0x38, now=1790307336, tz_units=0), device.SID, 0x38) == CAP_TIME
    # capture 3: magic 0xb2 needs a 2-byte varint
    assert _frame(device.build_time_sync(0xB2, now=1790309660, tz_units=0), device.SID, 0xB2) == CAP_TIME_CAP3


def test_keepalive_matches_capture_and_uses_plain_flag():
    pb = device.build_keepalive(0x59)
    assert device.flag_for(device.SID, pb) == device.FLAG_PLAIN
    assert _frame(pb, device.SID, 0x59) == CAP_KEEPALIVE_L
    assert _frame(device.build_keepalive(0x81), device.SID, 0x81) == CAP_KEEPALIVE_R_BIG_MAGIC
    steps = device.keepalive_steps(iter([1, 2]).__next__)
    assert [s[2] for s in steps] == ['left', 'right']


def test_onboarding_finish_and_wear_match_capture():
    assert _frame(device.build_onboarding_finish(0x40), device.SID_ONBOARDING, 0x40) == CAP_FINISH
    assert _frame(device.build_wear_status_query(0x41), device.SID_ONBOARDING, 0x41) == CAP_WEAR


def test_case_query_matches_capture():
    assert _frame(device.build_case_info_query(0x46), device.SID_CASE, 0x46) == CAP_CASE


def test_connect_sequence_reproduces_capture():
    magics = iter([0x37, 0x38, 0x3B, 0x3C, 0x40, 0x41, 0x46, 0x4C])
    seq = device.connect_sequence(magics.__next__, now=1790307336, tz_units=0)
    expected = [CAP_ROLE, CAP_TIME, CAP_READ, CAP_UNITS, CAP_FINISH, CAP_WEAR, CAP_CASE, CAP_GESTURES]
    assert [lens for _, _, lens in seq] == ['right'] * len(expected)
    got = [_frame(pb, sid, evenhub.read_varint_field(pb, 2)) for sid, pb, _ in seq]
    assert got == expected


def test_connect_sequence_full_reproduces_capture():
    # the app hands out magics before sending, so they are not in send order in the capture
    magics = iter([0x37, 0x38, 0x3E, 0x3F, 0x3A, 0x3B, 0x3C, 0x40, 0x41, 0x42, 0x46, 0x47, 0x4C])
    seq = device.connect_sequence(magics.__next__, now=1790307336, tz_units=0, full=True)
    expected = [CAP_ROLE, CAP_TIME, CAP_AI_CONFIG, CAP_QUICKLIST, CAP_SYNC, CAP_READ, CAP_UNITS,
                CAP_FINISH, CAP_WEAR, CAP_WATCHFACE, CAP_CASE, CAP_NOTIF, CAP_GESTURES]
    got = [_frame(pb, sid, evenhub.read_varint_field(pb, 2)) for sid, pb, _ in seq]
    assert got == expected


# ── device: decoders on captured replies ─────────────────────────────────────────────────────

def test_decode_device_replies():
    push = device.decode(_payload(RX_AUTH_PUSH))
    assert push['name'] == 'AUTHENTICATION' and push['authenticated'] is True
    role = device.decode(_payload(RX_ROLE_ACK))
    assert (role['cmd'], role['magic'], role['result']) == (5, 0x37, 0)
    ts = device.decode(_payload(RX_TIME_ACK))
    assert (ts['name'], ts['magic'], ts['result']) == ('TIME_SYNC', 0x38, 0)
    ka = device.decode(_payload(RX_KEEPALIVE))
    assert (ka['name'], ka['magic']) == ('BASE_CONNECT_HEART_BEAT', 0x59)


def test_decode_time_sync_round_trip_negative_tz():
    pb = device.build_time_sync(7, now=1790307336, tz_units=-20)     # UTC-5
    d = device.decode(pb)
    assert (d['timestamp'], d['timezone']) == (1790307336, -20)


def test_decode_onboarding_and_case_replies():
    fin = device.decode_onboarding(_payload(RX_FINISH))
    assert fin['process_id'] == device.OB_FINISH and fin['label'] == 'CONFIG FINISH'
    wear = device.decode_onboarding(_payload(RX_WEAR))
    assert wear['worn'] is True and wear['magic'] == 0x41
    case = device.decode_case(_payload(RX_CASE))
    assert (case['battery'], case['lid'], case['in_case']) == (70, None, None)
    push = device.decode_case(_payload(RX_CASE_PUSH))
    assert (push['battery'], push['lid'], push['in_case']) == (70, 1, 1)


def test_device_info_round_trip():
    pb = device.build_get_device_info(3, device.INFO_GLASSES_SN)
    assert pb.hex() == '080c10035a040a020806'
    # synthetic reply: SN + BLE MAC
    val = (evenhub.encode_varint_field(1, device.INFO_GLASSES_SN) +
           evenhub.encode_message_field(7, evenhub.encode_bytes_field(1, b'S123')) +
           evenhub.encode_message_field(9, evenhub.encode_bytes_field(1, bytes.fromhex('cc0000123456'))))
    reply = device.build_package(device.CMD_GET_DEVICE_INFO, 3, evenhub.encode_message_field(1, val))
    info = device.decode(reply)['info'][0]
    assert info['glasses_sn'] == 'S123' and info['ble_mac'] == 'CC:00:00:12:34:56'


def test_destructive_commands_shapes():
    assert device.build_factory_reset(5).hex() == '080d10056200'
    assert device.build_quick_restart(5).hex() == '080f10057200'
    assert device.build_unpair(5, device.DEV_RING).hex() == '0809100542020800'
    assert device.build_disconnect(5, device.DEV_GLASSES).hex() == '080810053a020801'


def test_tz_units():
    assert device.tz_units_from_offset(0) == 0
    assert device.tz_units_from_offset(3600) == 4
    assert device.tz_units_from_offset(-5 * 3600) == -20
    assert device.tz_units_from_offset(5 * 3600 + 1800) == 22      # India
    assert device.tz_units_from_offset(5 * 3600 + 2700) == 23      # Nepal
    assert device.tz_units_from_offset(14 * 3600) == 56
    assert device.tz_units_from_offset(99 * 3600) == 127            # clamped
    now = time.time()
    assert device.local_tz_units(now) == device.tz_units_from_offset(time.localtime(now).tm_gmtoff)


def test_device_validation():
    for bad in (lambda: device.build_role_change(1, 3),
                lambda: device.build_time_sync(1, now=-1, tz_units=0),
                lambda: device.build_time_sync(1, now=0, tz_units=200),
                lambda: device.build_onboarding_config(1, 9),
                lambda: device.build_unpair(1, device.DEV_PHONE_IOS),
                lambda: device.build_unpair(1, device.DEV_RING, b'\x01\x02'),
                lambda: device.build_get_device_info(1, 42)):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError('expected ValueError')


# ── settings (SID 0x09 / 0x20): builders ─────────────────────────────────────────────────────

def test_read_units_gestures_match_capture():
    assert _frame(settings.build_read_settings(0x3B), settings.SID, 0x3B) == CAP_READ
    assert _frame(settings.build_units(0x3C), settings.SID, 0x3C) == CAP_UNITS
    assert _frame(settings.build_gestures(0x4C), settings.SID, 0x4C) == CAP_GESTURES


def test_settings_match_documented_hex():
    # docs/research/native_apps_findings.md §2.3, magic 0x2a
    assert settings.build_brightness(0x2A, 25).hex(' ') == '08 01 10 2a 1a 04 0a 02 10 19'
    assert settings.build_auto_brightness(0x2A, True).hex(' ') == '08 01 10 2a 1a 04 0a 02 08 01'
    assert settings.build_silent_mode(0x2A, True).hex(' ') == '08 01 10 2a 1a 04 32 02 08 01'
    assert settings.build_read_settings(0x2A).hex(' ') == '08 02 10 2a 22 02 08 01'
    assert settings.build_display_timeout(0x2A, 10).hex(' ') == '08 02 10 2a 22 02 08 0a'


def test_other_setting_shapes():
    assert settings.build_auto_brightness(1, False).hex() == '080110011a040a020800'   # explicit 0
    assert settings.build_wear_detection(1, 1).hex() == '080110011a042a020801'
    assert settings.build_head_up(1, True, 30).hex() == '080110011a0622040801101e'
    assert settings.build_head_up(1, False).hex() == '080110011a0422020800'
    assert settings.build_display_height(1, 6).hex() == '080110011a0412020806'
    assert settings.build_display_distance(1, 2).hex() == '080110011a041a020802'
    assert settings.build_display_position(1, height=6, distance=2) == [
        settings.build_display_height(1, 6), settings.build_display_distance(1, 2)]
    assert settings.build_dominant_hand(1, 1).hex() == '080110011a045a020801'
    assert settings.build_display_timeout(1, 'never').hex() == '080210012204' + '08d5fe03'  # 65365
    assert settings.decode_module(settings.build_display_timeout(1, None))['timeout'] == 65365
    assert settings.build_read_display_timeout(1).hex() == '080110012200'
    assert settings.build_language(1, 7).hex() == '080010011a020807'
    assert settings.build_language(1, 'korean') == settings.build_language(1, 8)


def test_settings_validation():
    bad_calls = (
        lambda: settings.build_brightness(1, 0), lambda: settings.build_brightness(1, 51),
        lambda: settings.build_brightness(1, 25.5), lambda: settings.build_brightness(1, True),
        lambda: settings.build_auto_brightness(1, 2), lambda: settings.build_silent_mode(1, 'on'),
        lambda: settings.build_head_up(1, True, 61), lambda: settings.build_head_up(1, True, -1),
        lambda: settings.build_display_height(1, -1),
        lambda: settings.build_display_height(1, settings.DISPLAY_LEVEL_MAX + 1),
        lambda: settings.build_display_position(1),
        lambda: settings.build_units(1, time_format=99),
        lambda: settings.build_gestures(1, []), lambda: settings.build_gestures(1, [(0, 0)]),
        lambda: settings.build_gestures(1, [(0, 0, 5)]),
        lambda: settings.build_dominant_hand(1, 2),
        lambda: settings.build_display_timeout(1, 7), lambda: settings.build_display_timeout(1, 0),
        lambda: settings.build_language(1, 9), lambda: settings.build_language(1, 'Klingon'),
        lambda: settings.build_read_settings(1, 2),
    )
    for i, bad in enumerate(bad_calls):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError(f'bad call #{i} did not raise ValueError')
    # boundaries are accepted
    settings.build_brightness(1, 1)
    settings.build_brightness(1, 50)
    settings.build_head_up(1, True, 0)
    settings.build_head_up(1, True, 60)
    for s in (3, 5, 10, 15):
        settings.build_display_timeout(1, s)


# ── settings: decoders on captured replies ───────────────────────────────────────────────────

def test_decode_readback_reply():
    d = settings.decode(_payload(RX_READ))
    assert d['name'] == 'READ' and d['magic'] == 0x3B and d['push'] is False
    v = d['values']
    assert v == {'setting_type': 1, 'brightness_level': 100, 'display_height': 6,
                 'left_version': '2.3.0.24', 'right_version': '2.3.0.24', 'head_up_angle': 30,
                 'wear_detection': 1, 'battery': 100, 'brightness_auto': 1, 'unread_count': 1}
    full = settings.with_defaults(v)
    assert full['silent_mode'] == 0 and full['charging'] == 0 and full['head_up'] == 0


def test_decode_unprompted_push():
    d = settings.decode(_payload(RX_PUSH_CHARGING))
    assert d['push'] is True and d['magic'] == 0xA6
    assert d['values']['charging'] == 1 and d['values']['battery'] == 100


def test_decode_set_acks():
    units = settings.decode(_payload(RX_UNITS_ACK))['set']['units']
    assert units == {'unit_format': 0, 'distance_unit': 0, 'time_format': 1, 'date_format': 0,
                     'temperature_unit': 1}
    gestures = settings.decode(_payload(RX_GESTURES_ACK))['set']['gestures']
    assert gestures == list(settings.GESTURES_APP_DEFAULT)


def test_settings_round_trip():
    s = settings.decode(settings.build_brightness(9, 25))['set']['brightness']
    assert (s['level'], s['auto']) == (25, 0)
    h = settings.decode(settings.build_head_up(9, True, 45))['set']['head_up']
    assert (h['enabled'], h['angle']) == (1, 45)
    assert settings.decode(settings.build_silent_mode(9, True))['set'] == {'silent_mode': 1}
    assert settings.decode(settings.build_wear_detection(9, False))['set'] == {'wear_detection': 0}
    hand = settings.decode(settings.build_dominant_hand(9, 1, bytes(6)))['set']['dominant_hand']
    assert hand['hand'] == 1 and hand['ring_mac'] == '00:00:00:00:00:00'
    lang = settings.decode_module(settings.build_language(9, 3))
    assert (lang['name'], lang['language']) == ('SYSTEM_GENERAL_SETTING', 3)
    t = settings.decode_module(settings.build_display_timeout(9, 15))
    assert (t['timeout'], t['timeout_never']) == (15, False)


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
