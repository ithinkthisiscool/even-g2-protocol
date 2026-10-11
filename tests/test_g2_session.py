"""Offline tests for g2.session's ack router, write lock and multi-packet reassembly (no glasses needed).

Run from the repo root:   python tests/test_g2_session.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from g2 import evenhub as eh  # noqa: E402
from g2 import transport as tp  # noqa: E402
from g2.session import GlassesSession, parse_frame, read_ack_magic  # noqa: E402


class FakeClient:
    """Records writes; a scripted 'glasses' answers requests after a delay, out of order."""

    def __init__(self, session, delays):
        self.session, self.delays, self.writes = session, delays, []

    async def write_gatt_char(self, _uuid, pkt, response=False):
        self.writes.append(pkt)
        sid, magic_pb = pkt[6], pkt[8:-2] if pkt[4] == pkt[5] == 1 else None
        if magic_pb is None:
            return
        magic = eh.read_varint_field(magic_pb, 2, -1)
        delay = self.delays.get(magic)
        if delay is None:
            return                         # this request is never answered (timeout case)

        async def reply():
            await asyncio.sleep(delay)
            noise = eh.frame_pb(eh.encode_varint_field(1, 9) + eh.encode_varint_field(2, 999), sid, 0x01, 1)[0]
            self.session._on_notify_for_test(noise)
            ack = eh.frame_pb(eh.encode_varint_field(1, 2) + eh.encode_varint_field(2, magic) +
                              eh.encode_varint_field(7, magic * 10), sid, 0x01, 1)[0]
            self.session._on_notify_for_test(ack)
        asyncio.ensure_future(reply())


def make_session(delays):
    s = GlassesSession(initial_text='')

    def on_notify(data):
        s._route_ack(bytes(data))
        s.notes.put_nowait(bytes(data))
    s._on_notify_for_test = on_notify
    s.right_client = FakeClient(s, delays)
    return s


def test_concurrent_requests_get_their_own_acks():
    async def run():
        s = make_session({101: 0.15, 102: 0.05, 103: 0.10})    # answered 102, 103, 101
        reqs = [s._send_and_get_payload(0x01, eh.FLAG_REQUEST, m, eh.encode_varint_field(1, 2) +
                                        eh.encode_varint_field(2, m), 1, f'req{m}', timeout_s=2)
                for m in (101, 102, 103)]
        results = await asyncio.gather(*reqs)
        return [eh.read_varint_field(pb, 7, -1) for pb in results], s
    values, s = asyncio.run(run())
    assert values == [1010, 1020, 1030], values
    assert not s._pending


def test_unanswered_request_times_out_and_cleans_up():
    async def run():
        s = make_session({})
        pb = await s._send_and_get_payload(0x01, eh.FLAG_REQUEST, 555, eh.encode_varint_field(1, 2) +
                                           eh.encode_varint_field(2, 555), 1, 'lost', timeout_s=0.2)
        return pb, s
    pb, s = asyncio.run(run())
    assert pb is None and not s._pending


def test_multi_packet_messages_are_not_interleaved():
    async def run():
        s = make_session({})
        big_a = eh.frame_pb(bytes([0xAA]) * 600, 0x01, eh.FLAG_REQUEST, 1)   # 3 packets
        big_b = eh.frame_pb(bytes([0xBB]) * 600, 0x01, eh.FLAG_REQUEST, 2)
        await asyncio.gather(s._send_right(big_a), s._send_right(big_b))
        return s.right_client.writes
    writes = asyncio.run(run())
    seqs = [w[2] for w in writes]
    assert seqs in ([1, 1, 1, 2, 2, 2], [2, 2, 2, 1, 1, 1]), seqs


def test_ack_with_command_id_zero_is_matched():
    from g2.session import read_ack_magic
    assert read_ack_magic(eh.encode_varint_field(2, 77) + eh.encode_message_field(3, b'')) == 77


# ── multi-packet replies (FrameAssembler, ported from eveng2) ─────────────────────────────────

def reply_packets(pb, sid=0x01, flag=0x00, seq=0x37):
    """Split `pb` like the glasses would (same framing as ours, direction byte AA 12)."""
    return [bytes([0xAA, 0x12]) + pkt[2:] for pkt in eh.frame_pb(pb, sid, flag, seq)]


def test_single_packets_pass_through_unchanged():
    a = tp.FrameAssembler()
    pkt = reply_packets(eh.encode_varint_field(1, 2) + eh.encode_varint_field(2, 7))[0]
    assert a.feed(pkt) == pkt
    assert a.feed(b'\x01\x02') == b'\x01\x02'                # non-AA data is handed on as before


def test_multi_packet_reply_is_reassembled():
    pb = eh.encode_varint_field(1, 2) + eh.encode_varint_field(2, 321) + eh.encode_bytes_field(5, bytes(range(256)) * 2)
    pkts = reply_packets(pb, sid=0x09)
    assert len(pkts) == 3
    a = tp.FrameAssembler()
    assert a.feed(pkts[0]) is None and a.feed(pkts[1]) is None
    out = a.feed(pkts[2])
    assert parse_frame(out) == (0x09, 0x00, pb)
    assert tp.parse_rx(out) == (0x09, pb)
    assert read_ack_magic(parse_frame(out)[2]) == 321 and a.crc_errors == 0


def test_crc_split_across_last_two_packets():
    pb = bytes([0x08, 0x02]) + bytes(229)                  # 231 bytes: CRC straddles packets 1 and 2
    pkts = reply_packets(pb)
    assert len(pkts) == 2 and pkts[1][3] == 1
    a = tp.FrameAssembler()
    assert a.feed(pkts[0]) is None
    assert parse_frame(a.feed(pkts[1]))[2] == pb and a.crc_errors == 0


def test_gaps_drop_the_partial_message_and_bad_crc_is_counted():
    pb = bytes(500)
    p1, p2, p3 = reply_packets(pb, seq=5)
    a = tp.FrameAssembler()
    assert a.feed(p2) is None                                 # no packet 1 yet: ignored
    assert a.feed(p1) is None and a.feed(p3) is None          # packet 2 missing: dropped
    assert a.feed(p1) is None and a.feed(p2) is None          # restart works
    bad = p3[:-1] + bytes([p3[-1] ^ 0xFF])
    assert parse_frame(a.feed(bad))[2] == pb and a.crc_errors == 1
    # two messages on different SIDs may interleave
    q1, q2, q3 = reply_packets(bytes(500), sid=0x0c, seq=5)
    assert a.feed(p1) is None and a.feed(q1) is None and a.feed(p2) is None and a.feed(q2) is None
    assert parse_frame(a.feed(q3))[0] == 0x0c and parse_frame(a.feed(p3))[0] == 0x01


def test_session_routes_reassembled_ack_and_taps_once():
    async def run():
        s = GlassesSession(initial_text='')
        taps = []
        s.on_raw_frame = lambda sid, flag, pb: taps.append((sid, pb))
        fut = asyncio.get_running_loop().create_future()
        s._pending[(0x01, 4242)] = fut
        pb = eh.encode_varint_field(1, 2) + eh.encode_varint_field(2, 4242) + eh.encode_bytes_field(9, bytes(700))
        for pkt in reply_packets(pb):
            s._on_control_notify(bytearray(pkt))
        return await asyncio.wait_for(fut, 1), taps, pb, s.notes.qsize()
    got, taps, pb, queued = asyncio.run(run())
    assert got == pb and taps == [(0x01, pb)] and queued == 1


def test_truncated_ack_varint_does_not_raise():
    assert read_ack_magic(bytes([0x08, 0x02, 0x10, 0x80])) is None
    assert read_ack_magic(bytes([0x10, 0xFF])) is None


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
