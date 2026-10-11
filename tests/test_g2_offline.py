"""Offline tests for the g2 protocol library — no glasses or Bluetooth needed.

Run from the repo root:  .venv/bin/python -m pytest tests      (or)   .venv/bin/python tests/test_g2_offline.py

Each "golden" byte string below was either captured from the real glasses, taken from the
official app's decompiled code, or confirmed working live. If one of these tests fails, an
encoder has changed its output — check the change against the docs before "fixing" the test.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from g2 import dashboard, even_ai, evenhub, file_service, menu, quicklist, sids  # noqa: E402


# ── evenhub (framing) ────────────────────────────────────────────────────────────────────────

def test_frame_header_and_crc():
    frames = evenhub.frame_pb(b'\x08\x01', 0x07, evenhub.FLAG_REQUEST, 5)
    assert len(frames) == 1
    f = frames[0]
    assert f[:8] == bytes([0xAA, 0x21, 5, 4, 1, 1, 0x07, 0x20])   # magic, len(pb+crc), total, num, sid, flag
    assert f[8:10] == b'\x08\x01'
    assert f[10:] == evenhub.crc16_ccitt(b'\x08\x01')


def test_frame_splits_long_payloads():
    frames = evenhub.frame_pb(bytes(500), 0x01, evenhub.FLAG_REQUEST, 1)
    assert [fr[4] for fr in frames] == [3, 3, 3]          # total packets
    assert [fr[5] for fr in frames] == [1, 2, 3]          # packet numbers


# ── Even AI (SID 0x07) ───────────────────────────────────────────────────────────────────────

def test_even_ai_enter_matches_live_capture():
    # Sent by the hub and echoed by the glasses live on 2026-09-28: 080110..1a020802
    assert even_ai.build_ctrl(0x42, even_ai.CTRL_ENTER).hex() == '080110421a020802'


def test_even_ai_decode_wake_and_exit_from_glasses():
    wake = even_ai.decode(bytes.fromhex('080110141a020801'))
    assert (wake['cmd'], wake['value'], wake['label']) == (even_ai.CMD_CTRL, even_ai.CTRL_WAKE_UP, 'CTRL WAKE_UP')
    done = even_ai.decode(bytes.fromhex('0808101252020802'))
    assert done['label'] == 'EVENT STREAM_COMPLETE'


def test_even_ai_ask_analyse_reply_shapes():
    assert even_ai.build_ask(0x10, 'hi').hex() == '080310102a0422026869'
    assert even_ai.build_analyse(0x10).hex() == '080410103200'
    reply = even_ai.build_reply(0x10, b'ok', final=True)
    sub = evenhub.read_bytes_field(reply, 7)
    assert evenhub.read_bytes_field(sub, 4) == b'ok' and evenhub.read_varint_field(sub, 6, 0) == 1


def test_even_ai_reply_chunks_respect_512_bytes_and_utf8():
    chunks = even_ai.reply_chunks('é' * 300)                  # 600 bytes
    assert [len(c) for c in chunks] == [512, 88]
    assert ''.join(c.decode('utf-8') for c in chunks) == 'é' * 300
    assert even_ai.reply_chunks('') == [b'']


# ── Dashboard (SID 0x01) ─────────────────────────────────────────────────────────────────────

def test_weather_matches_app_bytes():
    # Built from the app's WeatherService in the decompile (docs/research/unknowns_findings.md §1).
    pb = dashboard.build_weather_push(0x1234, 21.0, unit=1, weather_type=1, update_time=1790000000000,
                                      status_string='Sunny', rainfall_icon=3, rainfall_hint='No rain',
                                      sunset_select=1, sunset_hint='18:42')
    assert pb.hex() == ('080210b42422311a2f0a2d0a2b0d0000a841100118012080d8c1a28c342a0553756e6e79'
                        '30033a074e6f207261696e40014a0531383a3432')


# ── Menu (SID 0x03) ──────────────────────────────────────────────────────────────────────────

def test_builtin_menu_items_carry_only_app_id():
    assert menu.SID_MENU == sids.MENU == 0x03
    assert menu.build_builtin_menu_push(9, [7, 6]).hex() == '080010091a0e0802120408002007120408002006'


# ── Quicklist (SID 0x0c) ─────────────────────────────────────────────────────────────────────

def test_quicklist_title_is_nul_terminated_bytes():
    item = quicklist.encode_quicklist_item(3, 0, False, 0, 'Buy milk', quicklist.TS_TYPE_TIME)
    assert item.hex() == '08031000180020002a09427579206d696c6b003802'


def test_quicklist_status_updates_and_paging():
    item = evenhub.encode_varint_field(1, 9) + evenhub.encode_varint_field(3, 1)
    pb = evenhub.encode_varint_field(1, 1) + evenhub.encode_varint_field(2, 7) + evenhub.encode_message_field(3, item)
    assert quicklist.decode_status_updates(pb) == [{'uid': 9, 'index': 0, 'completed': True, 'title': None}]
    ev = evenhub.encode_varint_field(1, quicklist.EVENT_UPDATE_DATA_DOWN) + evenhub.encode_varint_field(2, 2)
    pb = evenhub.encode_varint_field(1, 3) + evenhub.encode_varint_field(2, 8) + evenhub.encode_message_field(5, ev)
    assert quicklist.decode_event(pb) == (quicklist.EVENT_UPDATE_DATA_DOWN, 2)
    tasks = [{'uid': u} for u in range(1, 30)]
    assert [t['uid'] for t in quicklist.page_around(tasks, quicklist.EVENT_UPDATE_DATA_DOWN, 2)] == list(range(3, 23))
    assert quicklist.page_around(tasks, quicklist.EVENT_UPDATE_DATA_UP, 999) == []


# ── File service + notifications (SID 0xC4/0xC5) ─────────────────────────────────────────────

NOTIFICATION_JSON = (b'{"android_notification":{"msg_id":1,"action":0,"app_identifier":"com.even.sg",'
                     b'"title":"Test","subtitle":"","message":"Hello","time_s":1790000000,'
                     b'"date":"20260921T133320","display_name":"Even Realities"}}')


def test_file_crc32_check_value():
    assert file_service.crc32_efs(b'123456789') == 0xC052A8C8
    assert file_service.crc32_efs(NOTIFICATION_JSON) == 0xAC7F53F1


def test_file_frames_match_worked_example():
    start = file_service.build_start(0x10, file_service.FILE_TYPE_NOTIFICATION, NOTIFICATION_JSON)
    assert start[:20].hex(' ') == 'aa 21 10 5f 01 01 c4 00 00 01 00 00 00 cb 00 00 00 f1 53 7f'
    assert start[-2:].hex() == '1b0d' and len(start) == 103
    assert file_service.build_data_prefix(0x11).hex() == 'aa2111030101c40001d1f1'
    data = file_service.build_data_packets(0x11, NOTIFICATION_JSON)
    assert len(data) == 1 and data[0][:8].hex() == 'aa2111cd0101c500' and data[0][-2:].hex() == 'f322'
    assert file_service.build_result_check(0x12).hex() == 'aa2112030101c40002b2c1'


def test_file_packets_add_crc_only_packet_at_boundary():
    assert [len(p) - 8 for p in file_service.build_data_packets(1, bytes(232))] == [232, 2]
    assert [len(p) - 8 for p in file_service.build_data_packets(1, bytes(300))] == [232, 70]


def test_send_file_retries_on_crc_error():
    class FakeGlasses:
        def __init__(self):
            self.file_notes, self.seq, self.fail_once = asyncio.Queue(), 0, True

        def next_seq(self):
            self.seq += 1
            return self.seq

        async def enable_file_channel(self):
            pass

        async def send_file_frames(self, frames, gap_s=0.0):
            for fr in frames:
                sid, payload = fr[6], fr[8:]
                if sid == file_service.SID_FILE_CMD and payload[0] in (0, 2):
                    self.file_notes.put_nowait((sid, 0, bytes([payload[0], 0])))
                if sid == file_service.SID_FILE_DATA and fr[4] == fr[5]:
                    rsp = 2 if self.fail_once else 0
                    self.fail_once = False
                    self.file_notes.put_nowait((sid, 0, bytes([1, rsp])))

    ok, msg = asyncio.run(file_service.send_file(FakeGlasses(), 1, bytes(5000)))
    assert ok and msg == 'RESULT_CHECK SUCCESS'


def test_notification_enable_matches_app_bytes():
    assert file_service.build_notification_ctrl(0x33).hex() == '080110331a06080110011805'


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
