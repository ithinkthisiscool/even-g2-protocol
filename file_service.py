"""File service (EFS, SIDs 0xC4/0xC5) and phone-style notifications (SID 0x04). UNTESTED LIVE.

Plain English
-------------
Some things are sent to the glasses as a "file" instead of a protobuf message. The most useful
one: a notification (title + message) is a small JSON file of type 1. Sending a file takes
three steps, each answered by the glasses with two bytes [command id, result] (0 = SUCCESS):

    START         "a file of type T, N bytes, checksum C is coming"
    DATA          the bytes, in 4096-byte blocks, each block split into 232-byte packets
    RESULT_CHECK  "that was all -- please verify and commit"

send_file(session, file_type, data) runs the whole exchange, with the official app's retries.

To show a notification (spec from the decompile; bytes match the golden tests, never tried live):
    1. once: SID 0x04 build_notification_ctrl(magic) (turn notifications + auto-display on), and
       optionally build_whitelist_disable(magic) -- both framed with evenhub.frame_pb(...,
       SID_NOTIFICATION, evenhub.FLAG_REQUEST, seq) on the normal control channel;
    2. await send_file(session, FILE_TYPE_NOTIFICATION, notification_json('Title', 'Message')).

Differences from normal control frames: the file frames go to characteristic 0x7401 (replies on
0x7402), the flag byte is 0x00 not 0x20, the payloads are raw bytes (not protobuf), and the file
checksum is a custom CRC32 (crc32_efs), not zlib's.

Source: the blutter decompile, see esp32_ai/docs/research/file_service_notifications.md (it has a
worked byte-by-byte example that tests/test_g2_offline.py checks). UNTESTED LIVE as of
2026-09-28.

Transport: same 8-byte header as control frames ([AA 21 magic len total num SID flag], CRC-16/CCITT
LE on the last packet) but flag=0x00, written to the FILE characteristic 0x7401 (replies on 0x7402).
Payloads are raw bytes, not protobuf: commands are [CID]+data, replies are [CID, RSP].

  START        SID 0xC4  [00] + meta(92B LE: u32 fileType, u32 len, u32 CRC32', 80B path)
  DATA prefix  SID 0xC4  [01]  (same magic as the raw packets, not acked, then ~10 ms)
  DATA         SID 0xC5  raw file bytes, 232B/packet, one CRC16 over the 4096B block on the last
  RESULT_CHECK SID 0xC4  [02]  (commit)

Notifications are fileType 1: compact UTF-8 JSON {"android_notification": {...}}. The START path is
always "user/notify_whitelist.json" -- the glasses route by fileType.
"""
import json
import struct
import sys
import os
import time

from . import evenhub as eh

CHAR_FILE_WRITE_ID = 0x7401
CHAR_FILE_NOTIFY_ID = 0x7402
SID_FILE_CMD = 0xC4
SID_FILE_DATA = 0xC5
SID_NOTIFICATION = 0x04
FLAG_FILE = 0x00

# Command ids (first payload byte) and reply codes (second reply byte).
CID_START, CID_DATA, CID_RESULT_CHECK = 0, 1, 2
RSP_NAMES = {0: 'SUCCESS', 2: 'DATA_CRC_ERR', 4: 'TIMEOUT', 5: 'NO_RESOURCES', 6: 'RESULT_CHECK_FAIL'}
RSP_RETRY = {2, 4, 5}      # DATA_CRC_ERR, TIMEOUT, NO_RESOURCES -> resend the block (max 3 retries)

# eEvenFileServiceType: 0 = notification whitelist JSON, 1 = Android notification JSON.
FILE_TYPE_WHITELIST, FILE_TYPE_NOTIFICATION = 0, 1
FILE_PATH = b'user/notify_whitelist.json'
BLOCK_SIZE = 4096
PACKET_DATA = 232          # file bytes per DATA packet (MTU 247 - 3 - 4 - 8)

_CRC32_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1EDC6F41) & 0xFFFFFFFF if _c & 0x80000000 else (_c << 1) & 0xFFFFFFFF
    _CRC32_TABLE.append(_c)


def crc32_efs(data):
    """CRC32' (Uint8listExt.toCrc32): poly 0x1EDC6F41, MSB-first, init 0, no final XOR.
    Check value for b'123456789' is 0xC052A8C8."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC32_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


def _crc16_int(data):
    lo, hi = eh.crc16_ccitt(data)
    return lo | (hi << 8)


def _header(magic, length, total, num, sid):
    """8-byte EFS frame header: AA 21 magic len total num sid 00 (flag always 0x00)."""
    return bytes([0xAA, 0x21, magic & 0xFF, length & 0xFF, total & 0xFF, num & 0xFF, sid & 0xFF, FLAG_FILE])


def single_frame(magic, sid, payload):
    """One complete EFS frame: header + payload + CRC16 of the payload. For START, the DATA
    prefix and RESULT_CHECK, which always fit in one packet."""
    body = payload + eh.crc16_ccitt(payload)
    return _header(magic, len(body), 1, 1, sid) + body


def build_start(magic, file_type, data):
    """START frame (SID 0xC4): [00] + 92 bytes of metadata (u32 fileType, u32 length,
    u32 crc32_efs(data), 80-byte path "user/notify_whitelist.json" zero-padded), all
    little-endian. The path is always the same; the glasses route by fileType. Expect [00, 00]."""
    meta = struct.pack('<III', file_type, len(data), crc32_efs(data)) + FILE_PATH[:80].ljust(80, b'\x00')
    return single_frame(magic, SID_FILE_CMD, bytes([CID_START]) + meta)


def build_data_prefix(magic):
    """DATA prefix frame (SID 0xC4, payload [01]). Sent before each 4096-byte block with the SAME
    magic as that block's packets. Not acked; the app waits ~10 ms after it."""
    return single_frame(magic, SID_FILE_CMD, bytes([CID_DATA]))


def build_data_packets(magic, block):
    """App packetization (@0x1482410): ceil(len/232) packets, plus one CRC-only packet when
    len % 232 is 0 or 231; the whole CRC16 rides on the last packet."""
    crc = eh.crc16_ccitt(block)
    chunks = [block[i:i + PACKET_DATA] for i in range(0, len(block), PACKET_DATA)] or [b'']
    if len(block) % PACKET_DATA in (0, PACKET_DATA - 1) and block:
        chunks.append(b'')
    frames = []
    for n, chunk in enumerate(chunks, 1):
        body = chunk + (crc if n == len(chunks) else b'')
        frames.append(_header(magic, len(body), len(chunks), n, SID_FILE_DATA) + body)
    return frames


def build_result_check(magic):
    """RESULT_CHECK frame (SID 0xC4, payload [02]): commit the file. Expect [02, 00]; 6 means the
    length/CRC32 check failed."""
    return single_frame(magic, SID_FILE_CMD, bytes([CID_RESULT_CHECK]))


def notification_json(title, message, subtitle='', display_name='Even Realities',
                      app_identifier='com.even.sg', msg_id=None, now=None):
    """Build the notification file body (bytes): compact UTF-8 JSON
    {"android_notification": {msg_id, action, app_identifier, title, subtitle, message, time_s,
    date, display_name}}. Pass it to send_file(session, FILE_TYPE_NOTIFICATION, ...).
    msg_id defaults to the current time; `now` (Unix seconds) is for reproducible tests.
    Key order matters (AppNotification.toJson). `date` format yyyyMMddTHHmmss is INFERRED."""
    now = int(now if now is not None else time.time())
    body = {'msg_id': msg_id if msg_id is not None else now & 0x7FFFFFFF, 'action': 0,
            'app_identifier': app_identifier, 'title': title, 'subtitle': subtitle,
            'message': message, 'time_s': now,
            'date': time.strftime('%Y%m%dT%H%M%S', time.localtime(now)),
            'display_name': display_name}
    return json.dumps({'android_notification': body}, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def build_notification_ctrl(magic, enable=True, auto_display=True, disp_time=5, dnd=False):
    """Turn notifications on (protobuf bytes for SID 0x04, frame with FLAG_REQUEST). Golden
    bytes match the app; untested live. disp_time unit is presumably seconds (INFERRED).

    SID 0x04 NotificationDataPackage{1 cmd=1 CTRL, 2 magic, 3 {1 notifEnable, 2 autoDispEnable,
    3 dispTime, 5 avoidDisturbEnable}} -- sent on the normal control channel (5401, flag 0x20)."""
    ctrl = (eh.encode_varint_field(1, int(enable)) + eh.encode_varint_field(2, int(auto_display)) +
            eh.encode_varint_field(3, disp_time))
    if dnd:
        ctrl += eh.encode_varint_field(5, 1)
    return eh.encode_varint_field(1, 1) + eh.encode_varint_field(2, magic) + eh.encode_message_field(3, ctrl)


def build_whitelist_disable(magic, disable=True):
    """SID 0x04 cmd 3 {6: {1 whitelistDisable}} -- bypasses the glasses-side app
    whitelist (confirmed by the firmware analysis; sent on every hub connect)."""
    return (eh.encode_varint_field(1, 3) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(6, eh.encode_varint_field(1, int(disable))))


def parse_file_reply(pb):
    """Replies are [cid, rsp]. Returns (cid, rsp) or None if the payload is too short.
    rsp 0 = SUCCESS; see RSP_NAMES."""
    if len(pb) < 2:
        return None
    return pb[0], pb[1]


# ── Sending a whole file ─────────────────────────────────────────────────────────────────────
import asyncio as _asyncio
import time as _time

_send_lock = _asyncio.Lock()


async def _wait_reply(session, cid, timeout_s):
    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        try:
            _hi, _lo, pb = await _asyncio.wait_for(session.file_notes.get(), timeout=deadline - _time.time())
        except _asyncio.TimeoutError:
            break
        reply = parse_file_reply(pb)
        if reply and reply[0] == cid:
            return reply[1]
    return None


async def send_file(session, file_type, data, log=None):
    """Send `data` to the glasses as a file of `file_type`, the way the official app does:
    START → for each 4096-byte block: DATA prefix + raw packets (retried up to 3 times on
    CRC error / timeout / no resources) → RESULT_CHECK (commit).

    `session` is a connected g2.session.GlassesSession. `log(message, ok)` is an optional
    progress callback. Returns (ok, message). Only one transfer runs at a time."""
    def say(msg, ok):
        if log:
            log(msg, ok)

    def rsp_name(r):
        return 'no reply' if r is None else RSP_NAMES.get(r, f'rsp={r}')

    async with _send_lock:
        await session.enable_file_channel()
        while not session.file_notes.empty():
            session.file_notes.get_nowait()
        await session.send_file_frames([build_start(session.next_seq(), file_type, data)])
        rsp = await _wait_reply(session, CID_START, 8.0)
        say(f'EFS START type={file_type} len={len(data)}: {rsp_name(rsp)}', rsp == 0)
        if rsp != 0:
            return False, f'START {rsp_name(rsp)}'
        for off in range(0, len(data), BLOCK_SIZE):
            block = data[off:off + BLOCK_SIZE]
            for attempt in range(4):
                magic = session.next_seq()
                await session.send_file_frames([build_data_prefix(magic)])
                await session.send_file_frames(build_data_packets(magic, block))
                rsp = await _wait_reply(session, CID_DATA, 8.0)
                say(f'EFS DATA @{off} try {attempt + 1}: {rsp_name(rsp)}', rsp == 0)
                if rsp == 0:
                    break
                if rsp is not None and rsp not in RSP_RETRY:
                    return False, f'DATA {rsp_name(rsp)}'
                if rsp == 5:
                    await _asyncio.sleep(5)
            else:
                return False, 'DATA retries exhausted'
        await session.send_file_frames([build_result_check(session.next_seq())])
        rsp = await _wait_reply(session, CID_RESULT_CHECK, 8.0)
        say(f'EFS RESULT_CHECK: {rsp_name(rsp)}', rsp == 0)
        return rsp == 0, f'RESULT_CHECK {rsp_name(rsp)}'
