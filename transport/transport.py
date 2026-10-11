"""Transport layer: the Bluetooth "mailboxes" of the glasses and the login (auth) handshake.

Plain English
-------------
Each lens of the G2 is a separate Bluetooth Low Energy (BLE) device. A BLE device exposes
"characteristics": small mailboxes, each with a UUID. You write bytes into one and the device
answers by "notifying" you on another. The G2 uses one UUID pattern for all of them:

    UUID_BASE = "00002760-08c2-11e1-9073-0e8ac72e{:04x}"   (the last 4 hex digits vary)

    0x5401  CHAR_WRITE   main control mailbox: we write commands here   (almost everything)
    0x5402  CHAR_NOTIFY  main control replies: the glasses notify us here
    0x6402               microphone audio stream (see g2.session.RENDER_NOTIFY_UUID)
    0x7401 / 0x7402      file service write / notify (see g2.file_service)

Before the glasses accept commands, each lens must be "authenticated": we send one small frame
on service 0x80 and the right lens answers "OK". auth_frame() builds that frame and parse_rx()
reads the reply. This single-packet auth sequence comes from jimrandomh/g2flash's g2flash.py and
was verified against the real glasses on 2026-09-24.

What to use from here
---------------------
    UUID_BASE, CHAR_WRITE, CHAR_NOTIFY   the characteristic UUIDs        (live-confirmed)
    auth_frame(), parse_rx()             auth handshake                  (live-confirmed)
    FrameAssembler                       multi-packet reply reassembly   (offline-tested)
    crc16_ccitt()                        frame checksum (same algorithm as g2.evenhub.crc16_ccitt,
                                         but returns an int instead of 2 bytes)

You normally never call these yourself: g2.session.GlassesSession.connect() does it.

Incoming multi-packet messages
------------------------------
Long replies from the glasses are split exactly like ours (total > 1, num = 1..total, the CRC of
the whole payload after the last chunk). FrameAssembler joins them back into ONE frame before any
parser sees them; GlassesSession feeds every notification through it. Ported from the eveng2
library. Untested live: no multi-packet reply from the glasses has been logged yet.

The older teleprompter experiment that used to live at the end of this file (build_display_config,
build_teleprompter_init, ... send_text, main) was removed: it never displayed text and nothing used
it. The real teleprompter is g2.teleprompter.
"""

UUID_BASE = "00002760-08c2-11e1-9073-0e8ac72e{:04x}"
CHAR_WRITE = UUID_BASE.format(0x5401)
CHAR_NOTIFY = UUID_BASE.format(0x5402)


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE checksum of `data` (poly 0x1021, init 0xFFFF), returned as an int.

    A CRC is a short fingerprint of some bytes; the glasses recompute it and silently drop any
    frame whose fingerprint does not match. On the wire it is written little-endian (low byte
    first). g2.evenhub.crc16_ccitt is the same algorithm returning the 2 wire bytes directly."""
    # even-g2-protocol's original version of this function never XORed the byte
    # value into the register at all (just shifted once per byte) - a real bug,
    # confirmed by comparing against g2flash.py's crc16() (which matches the
    # real firmware's cfw_message_crc in patches/message_transport.c) and by the
    # fact every packet built with the broken version got no response, while
    # packets using the correct algorithm below got clean, repeatable ACKs.
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def add_crc(packet: bytes) -> bytes:
    """Return `packet` + CRC16 (little-endian) of everything after the 8-byte frame header.
    Used by auth_frame() to finish a single-packet frame."""
    crc = crc16_ccitt(packet[8:])
    return packet + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def encode_varint(value: int) -> bytes:
    """Protobuf varint encoding of a non-negative int (7 bits per byte, high bit = "more").
    Same as g2.evenhub.encode_varint; kept here for the experimental builders below."""
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


CHUNK = 232  # matches g2flash.py's real, working chunk size for the aa21 envelope


def build_frames(seq: int, service_hi: int, service_lo: int, payload: bytes) -> list[bytes]:
    """EXPERIMENTAL (teleprompter experiment only). Use g2.evenhub.frame_pb for real work.

    Split `payload` across one or more aa21 envelope packets sharing `seq`, with
    correct pkt_total/pkt_serial fields - even-g2-protocol's original build_packet()
    always hardcoded pkt_total=pkt_serial=1 regardless of actual payload size, which
    silently corrupts anything bigger than one MTU-ish chunk (a full 10-line text
    page easily exceeds it). This mirrors g2flash.py's frames(), the one version of
    this logic actually confirmed against real hardware."""
    crc = crc16_ccitt(payload)
    body = payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    total = max(1, -(-len(body) // CHUNK))
    out = []
    offset = 0
    for i in range(total):
        chunk = body[offset:offset + CHUNK]
        offset += len(chunk)
        out.append(bytes([0xAA, 0x21, seq, len(chunk), total, i + 1, service_hi, service_lo]) + chunk)
    return out


# ---- g2flash-derived auth (verified working) ----
_seq = [0]


def next_seq():
    """Module-level counter 1..255 (wraps) used only as the auth frame's magic/sequence byte."""
    _seq[0] = (_seq[0] + 1) & 0xFF
    return _seq[0]


def auth_frame():
    """Build the one-packet authentication frame for a lens. LIVE-CONFIRMED.

    Returns (magic, frame_bytes). Write frame_bytes to CHAR_WRITE on a lens; the right lens
    replies on CHAR_NOTIFY with a SID 0x80 frame whose protobuf starts 08 04 10 <magic> and
    ends 1a 00 when auth succeeded (check it with parse_rx()). The frame is
    AA 21 <magic> <len> 01 01 80 00 + protobuf {1: 4, 2: magic, 3: {1: 1, 2: 4}} + CRC16.
    GlassesSession.connect() sends this to both lenses."""
    magic = next_seq()
    pb = bytes([0x08, 0x04, 0x10, magic, 0x1A, 0x04, 0x08, 0x01, 0x10, 0x04])
    return magic, add_crc(bytes([0xAA, 0x21, magic, len(pb) + 2, 0x01, 0x01, 0x80, 0x00]) + pb)


def payload_span(frame: bytes):
    """(start, end) of the protobuf payload inside one AA frame: after the 8-byte header, minus
    the 2 CRC bytes. Byte 3 (len) gives the end for a real packet (at most 232 + 2 bytes, limited
    by the MTU); a frame built by FrameAssembler (len byte ASSEMBLED_LEN) runs to its last 2 bytes."""
    ln = frame[3]
    if ln == ASSEMBLED_LEN:
        return 8, len(frame) - 2
    return 8, 8 + max(0, ln - 2)


def parse_rx(frame: bytes):
    """Split a frame FROM the glasses into (sid, protobuf_bytes), or (None, b'') if it is not one.

    Frames from the glasses start AA 12 (source/destination nibbles swapped compared with our
    AA 21). Byte 3 is the payload length including the 2 CRC bytes, byte 6 is the SID. The CRC is
    not checked. Works for single packets and for FrameAssembler output; g2.session.parse_frame
    is the general version used everywhere else."""
    if len(frame) >= 10 and frame[0] == 0xAA and frame[1] == 0x12:
        sid = frame[6]
        a, b = payload_span(frame)
        return sid, frame[a:b]
    return None, b""


# ---- multi-packet reassembly (ported from eveng2.protocol.framing.FrameAssembler) ----
ASSEMBLED_LEN = 0xFF   # len byte of a reassembled frame (a real packet carries at most 232 + CRC)


class FrameAssembler:
    """Reassemble multi-packet messages from the notify channel.

    feed(packet) returns the bytes to hand on, or None while a message is incomplete:
      * anything that is not a multi-packet AA frame (single packets, non-AA data) comes back
        unchanged, so existing parsers see exactly what they saw before;
      * packets 1..total-1 of a multi-packet message are buffered (returns None);
      * the last packet returns ONE synthetic frame
            AA <dir> <seq> FF 01 01 <sid> <flag> <whole payload> <crc 2 bytes>
        (len byte ASSEMBLED_LEN; see payload_span()), which g2.session.parse_frame and parse_rx
        split like a single packet.
    Packets of one message share (sid, seq). A gap or a packet out of order drops the partial
    message; a new packet 1 for the same key restarts it. crc_errors counts reassembled messages
    whose CRC did not match (they are still passed on, like single packets, whose CRC is not
    checked either)."""

    def __init__(self):
        self._parts = {}
        self.crc_errors = 0

    def feed(self, data):
        data = bytes(data)
        if len(data) < 8 or data[0] != 0xAA:
            return data
        total, num = data[4], data[5]
        if total <= 1:
            return data
        key = (data[6], data[2])
        chunk = data[8:8 + data[3]]
        if num == 1:
            self._parts[key] = [chunk]
        elif key in self._parts and len(self._parts[key]) == num - 1:
            self._parts[key].append(chunk)
        else:
            self._parts.pop(key, None)
            return None
        if num < total:
            return None
        body = b"".join(self._parts.pop(key))
        payload, crc = body[:-2], body[-2:]
        c = crc16_ccitt(payload)
        if crc != bytes([c & 0xFF, (c >> 8) & 0xFF]):
            self.crc_errors += 1
        return bytes([0xAA, data[1], data[2], ASSEMBLED_LEN, 1, 1, data[6], data[7]]) + body
