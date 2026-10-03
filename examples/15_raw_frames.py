"""A protocol tour: build and decode raw G2 frames offline. No glasses, no Bluetooth.

Walks through, with real bytes:
  1. protobuf basics: varints, keys, a float, a nested message;
  2. the frame format, field by field, on the Even AI ENTER frame the glasses echoed live;
  3. decoding a frame FROM the glasses (a "Hey Even" wake-up) with parse_frame / even_ai.decode;
  4. splitting a long payload into packets and joining a multi-packet reply (FrameAssembler);
  5. the auth frame, the CRCs (CRC-16/CCITT for frames, the custom CRC32 for files), SID names.

--decode HEX decodes your own frame(s) instead, e.g. ones copied from a log.
"""
import sys

import _common as c
from g2 import dashboard, even_ai as ai, evenhub as eh, file_service as fs, session, sids, transport as tp

STATUS = c.OFFLINE + ' (every byte below is golden-tested in tests/)'


def build_parser():
    p = c.parser(__doc__, STATUS, glasses=False)
    p.add_argument('--decode', nargs='+', metavar='HEX', help='decode these frame(s) and exit')
    return p


def hx(b):
    return b.hex(' ')


def section(title):
    print(f'\n── {title} ' + '─' * max(0, 70 - len(title)))


def explain_fields(pb, indent='  '):
    """Print every top-level protobuf field; recurse into nested messages that parse cleanly."""
    for field, wire, value in eh._iter_fields(pb):
        kind = {0: 'varint', 1: 'fixed64', 2: 'bytes', 5: 'fixed32'}[wire]
        if wire == 2:
            nested = _try_nested(value)
            if nested:
                print(f'{indent}field {field} ({kind}, {len(value)} B): message')
                explain_fields(value, indent + '    ')
                continue
            print(f'{indent}field {field} ({kind}): {value!r}')
        elif wire == 5:
            print(f'{indent}field {field} ({kind}): {hx(value)} = float {eh.read_float_field(pb, field):g}')
        else:
            print(f'{indent}field {field} ({kind}): {value}')


def _try_nested(value):
    """True if `value` parses cleanly as a protobuf message (and is not just readable text)."""
    if not value:
        return False
    try:
        if value.decode('utf-8').isprintable():
            return False                          # a string such as "Sunny"
    except UnicodeDecodeError:
        pass
    i = 0
    try:
        while i < len(value):
            key, i = eh.decode_varint_at(value, i)
            field, wire = key >> 3, key & 7
            if field == 0 or wire not in (0, 1, 2, 5):
                return False
            if wire == 0:
                _, i = eh.decode_varint_at(value, i)
            elif wire == 2:
                n, i = eh.decode_varint_at(value, i)
                i += n
            else:
                i += 8 if wire == 1 else 4
    except IndexError:
        return False
    return i == len(value)


def decode_frame(frame):
    """Describe one frame (either direction)."""
    if len(frame) < 10 or frame[0] != 0xAA:
        print(f'  not a G2 frame: {hx(frame)}')
        return
    direction = {0x21: 'phone → glasses', 0x12: 'glasses → phone'}.get(frame[1], f'route 0x{frame[1]:02x}')
    sid, flag, pb = session.parse_frame(frame)
    print(f'  {direction}, seq {frame[2]}, packet {frame[5]}/{frame[4]}, SID 0x{sid:02x} ({sids.name(sid)}), '
          f'flag 0x{flag:02x}, {len(pb)} payload bytes')
    if frame[4] == 1:
        a, b = tp.payload_span(frame)
        crc_ok = eh.crc16_ccitt(frame[a:b]) == frame[b:b + 2]
        print(f'  CRC16 {hx(frame[b:b + 2])} → {"OK" if crc_ok else "MISMATCH"}')
    if sid == ai.SID:
        print(f'  Even AI: {ai.decode(pb)["label"]}')
    explain_fields(pb)


def tour():
    section('1. Protobuf basics')
    for v in (1, 150, 300):
        print(f'  varint {v:>3} → {hx(eh.encode_varint(v))}')
    print(f'  key for field 2, wire type 2 = (2 << 3) | 2 = 0x{(2 << 3) | 2:02x}')
    print(f'  field 2 = "hi"   → {hx(eh.encode_string_field(2, "hi"))}')
    print(f'  field 1 = 21.0 as FLOAT (wire 5) → {hx(dashboard.encode_float_field(1, 21.0))}'
          '   (weather temperature; a double here makes the glasses drop the message)')
    nested = eh.encode_message_field(3, eh.encode_varint_field(1, 2))
    print(f'  field 3 = message {{1: 2}} → {hx(nested)}')

    section('2. A frame, byte by byte: Even AI ENTER, magic 0x42, seq 5')
    pb = ai.build_ctrl(0x42, ai.CTRL_ENTER)
    frame = eh.frame_pb(pb, ai.SID, eh.FLAG_REQUEST, 5)[0]
    print(f'  {hx(frame)}')
    rows = [(0, 1, 'start of frame'), (1, 2, 'route: to glasses (2) from us (1)'), (2, 3, 'seq (frame sync byte)'),
            (3, 4, 'length of this packet: payload + 2 CRC bytes'), (4, 5, 'total packets'),
            (5, 6, 'packet number (from 1)'), (6, 7, 'SID 0x07 = Even AI'), (7, 8, 'flag 0x20 = request'),
            (8, 10, 'field 1 = 1: command CTRL'), (10, 12, 'field 2 = 0x42: magic (echoed in the reply)'),
            (12, 14, 'field 3, 2 bytes: the CTRL sub-message'), (14, 16, '  inside: field 1 = 2 = ENTER'),
            (16, 18, 'CRC-16/CCITT of the payload, low byte first')]
    for a, b, what in rows:
        where = f'{a}' if b - a == 1 else f'{a}-{b - 1}'
        print(f'  byte {where:5} {hx(frame[a:b]):8}  {what}')

    section('3. Decoding a frame from the glasses: "Hey Even"')
    wake = bytes([0xAA, 0x12]) + eh.frame_pb(bytes.fromhex('080110141a020801'), ai.SID, 0x01, 0x97)[0][2:]
    print(f'  {hx(wake)}')
    decode_frame(wake)
    print('  → answer with CTRL ENTER within a few seconds (examples/library/10_even_ai.py)')

    section('4. Long payloads: split into packets, joined again')
    big = dashboard.build_news_push_v3(7, 0, 'A long article', 1790000000000, 'g2', 'x' * 500)
    packets = eh.frame_pb(big, dashboard.SID_DASHBOARD, eh.FLAG_REQUEST, 9)
    print(f'  {len(big)} payload bytes + 2 CRC → {len(packets)} packets (≤232 payload bytes each): '
          f'{[(p[5], p[4], len(p)) for p in packets]} (num, total, packet size)')
    asm = tp.FrameAssembler()
    joined = [out for out in (asm.feed(p) for p in packets) if out is not None]
    sid, flag, pb2 = session.parse_frame(joined[0])
    print(f'  FrameAssembler → 1 frame, SID 0x{sid:02x}, payload identical: {pb2 == big}, CRC errors: {asm.crc_errors}')

    section('5. Auth, CRCs, SIDs')
    magic, auth = tp.auth_frame()
    print(f'  auth frame (magic {magic}): {hx(auth)}  (sent to each lens first; flag 0x00)')
    print(f'  CRC-16/CCITT(b"123456789") = 0x{tp.crc16_ccitt(b"123456789"):04x}')
    print(f'  file CRC32 (poly 0x1EDC6F41, not zlib) of b"123456789" = 0x{fs.crc32_efs(b"123456789"):08x}')
    print('  SIDs: ' + ', '.join(f'0x{s:02x} {sids.name(s)}' for s in
                                 (sids.DASHBOARD, sids.MENU, sids.EVEN_AI, sids.SETTINGS, sids.QUICKLIST,
                                  sids.APP_SYNC, sids.DEVICE_CONFIG, sids.EVENHUB)))


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    if args.decode:
        for h in args.decode:
            try:
                frame = bytes.fromhex(h.replace(':', '').replace(' ', ''))
            except ValueError:
                print(f'Error: not hex: {h!r}', file=sys.stderr)
                return 2
            print(hx(frame))
            decode_frame(frame)
        return 0
    c.banner('Protocol tour', STATUS)
    tour()
    return 0


if __name__ == '__main__':
    sys.exit(c.run(main))
