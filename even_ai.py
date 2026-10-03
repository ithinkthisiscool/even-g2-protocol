"""Even AI (service 0x07) — the "Hey Even" voice assistant screen on the glasses. LIVE-CONFIRMED.

Plain English: when the user says "Hey Even" (or opens Even AI from the menu) the glasses tell
the phone "wake up". If the phone answers ENTER within a few seconds, the Even AI screen opens
and the microphone streams audio to the phone. The phone turns the audio into text, asks an AI,
and sends the answer back as text for the glasses to display. This module lets our hub play the
phone's part. Everything below, including the mic audio after ENTER, has been seen working live.

How a conversation works (confirmed live 2026-09-28, and matches the official app's decompiled
state machine in blutter `even/common/services/ai_agent/`):

    glasses → CTRL WAKE_UP        user said "Hey Even" or opened Even AI from the menu
    app     → CTRL ENTER          accept; the glasses exit on their own after a few seconds without it
    app     → HEARTBEAT every 2 s keeps the session open (the glasses echo each one)
    glasses → (mic audio)         LC3 audio on notify characteristic 0x6402 — see
                                  GlassesSession.start_audio_stream() / audio_packets
    (host detects silence)       the user stopped talking. The glasses never sent VAD_INFO in
                                  any live session, so the hub runs its own silence detector.
    app     → ASK {text}          shows the user's recognised words
    app     → ANALYSE             shows the "thinking" screen
    app     → REPLY {text, end}   the answer, ≤512 UTF-8 bytes per packet, ~150 ms apart
    glasses → EVENT STREAM_COMPLETE
    either  → CTRL EXIT           closes the screen

Every message is an `EvenAIDataPackage` protobuf:
    1 commandId   which command this is (CMD_* below)
    2 magicRandom a request id; replies echo it
    3..13         exactly one sub-message, chosen by the command (FIELD_FOR_CMD below)

Functions here only build or decode bytes. Sending them is the caller's job:
    frames = evenhub.frame_pb(even_ai.build_ctrl(magic, even_ai.CTRL_ENTER), even_ai.SID,
                              evenhub.FLAG_REQUEST, session.next_seq())
    await session._send_right(frames)

Receiving: watch incoming frames with GlassesSession.on_raw_frame (never read session.notes from
a second task); when the SID is 0x07, even_ai.decode(pb) tells you what the glasses said, e.g.
{'name': 'CTRL', 'value': CTRL_WAKE_UP, 'label': 'CTRL WAKE_UP', ...}.
The heartbeat, pacing and the full voice loop are implemented in hub/routes/even_ai.py
and hub/even_ai_assistant.py.
"""
from . import evenhub as eh

SID = 0x07

# commandId (eEvenAICommandId)
CMD_CTRL = 1
CMD_VAD_INFO = 2
CMD_ASK = 3
CMD_ANALYSE = 4
CMD_REPLY = 5
CMD_SKILL = 6
CMD_PROMPT = 7
CMD_EVENT = 8
CMD_HEARTBEAT = 9
CMD_CONFIG = 10
CMD_COMM_RSP = 0xA1

CMD_NAMES = {CMD_CTRL: 'CTRL', CMD_VAD_INFO: 'VAD_INFO', CMD_ASK: 'ASK', CMD_ANALYSE: 'ANALYSE',
             CMD_REPLY: 'REPLY', CMD_SKILL: 'SKILL', CMD_PROMPT: 'PROMPT', CMD_EVENT: 'EVENT',
             CMD_HEARTBEAT: 'HEARTBEAT', CMD_CONFIG: 'CONFIG', CMD_COMM_RSP: 'COMM_RSP'}

# Which protobuf field carries the sub-message for each command.
FIELD_FOR_CMD = {CMD_CTRL: 3, CMD_VAD_INFO: 4, CMD_ASK: 5, CMD_ANALYSE: 6, CMD_REPLY: 7,
                 CMD_SKILL: 8, CMD_PROMPT: 9, CMD_EVENT: 10, CMD_HEARTBEAT: 11,
                 CMD_COMM_RSP: 12, CMD_CONFIG: 13}

# EvenAIControl.status
CTRL_WAKE_UP = 1
CTRL_ENTER = 2
CTRL_EXIT = 3
CTRL_NAMES = {CTRL_WAKE_UP: 'WAKE_UP', CTRL_ENTER: 'ENTER', CTRL_EXIT: 'EXIT'}

# VADInfo.vadStatus — voice activity detection, reported by the glasses
VAD_START = 1
VAD_END = 2
VAD_TIMEOUT = 3
VAD_NAMES = {VAD_START: 'VAD_START', VAD_END: 'VAD_END', VAD_TIMEOUT: 'VAD_TIMEOUT'}

# Event.event
EVENT_SCROLL = 1
EVENT_STREAM_COMPLETE = 2
EVENT_NAMES = {EVENT_SCROLL: 'SCROLL', EVENT_STREAM_COMPLETE: 'STREAM_COMPLETE'}

# PromptInfo.promptType — built-in error screens
PROMPT_NAMES = {1: 'NETWORK_ERR', 2: 'BLE_DISCONNECT', 3: 'SERVER_ERR', 4: 'TROUBLE_UNDERSTAND',
                5: 'COMMAND_UNSUPPORT', 6: 'AUDIO_ERROR', 7: 'ASR_SERVER_ERR', 8: 'AI_SERVER_ERR',
                9: 'COMMAND_EXE_FAIL'}

REPLY_MAX_BYTES = 512      # the app logs "sendAIReplay text exceeds protocol limit" above 0x200
REPLY_PACE_S = 0.15        # gap between REPLY packets used by the app
HEARTBEAT_INTERVAL_S = 2.0
DEFAULT_STREAM_SPEED = 80  # CONFIG.streamSpeed the official app sends (capture; older notes said 32)


def build_package(cmd, magic, inner):
    """Wrap a sub-message in EvenAIDataPackage. `inner` may be b'' (e.g. ANALYSE)."""
    return (eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(FIELD_FOR_CMD[cmd], inner))


def build_ctrl(magic, status):
    """CTRL {1 status}: CTRL_ENTER to accept a wake, CTRL_EXIT to close."""
    return build_package(CMD_CTRL, magic, eh.encode_varint_field(1, status))


def build_heartbeat(magic, count):
    """HEARTBEAT {1 hbCnt}: send every HEARTBEAT_INTERVAL_S while a session is open."""
    return build_package(CMD_HEARTBEAT, magic, eh.encode_varint_field(1, count))


def build_config(magic, voice_switch=0, stream_speed=DEFAULT_STREAM_SPEED):
    """CONFIG {1 voiceSwitch, 2 streamSpeed}."""
    return build_package(CMD_CONFIG, magic, eh.encode_varint_field(1, voice_switch) +
                         eh.encode_varint_field(2, stream_speed))


def _text_info(text_bytes, final=False):
    """AskInfo/ReplyInfo {4 text, 6 fTextEnd}. cmdCnt(1), streamEnable(2) and textMode(3) are
    0 in the official app, and proto3 omits zero values, so they are not written."""
    inner = eh.encode_message_field(4, text_bytes)
    if final:
        inner += eh.encode_varint_field(6, 1)
    return inner


def build_ask(magic, text):
    """ASK: show the user's recognised question."""
    return build_package(CMD_ASK, magic, _text_info(text.encode('utf-8')))


def build_analyse(magic):
    """ANALYSE (empty): show the "thinking" screen."""
    return build_package(CMD_ANALYSE, magic, b'')


def build_reply(magic, chunk, final):
    """REPLY with one chunk of answer text (bytes, ≤REPLY_MAX_BYTES). `final` marks the last."""
    return build_package(CMD_REPLY, magic, _text_info(chunk, final))


def reply_chunks(text, limit=REPLY_MAX_BYTES):
    """Split answer text into ≤limit-byte UTF-8 chunks without cutting a multi-byte character.
    Always returns at least one chunk."""
    limit = max(1, min(limit, REPLY_MAX_BYTES))
    chunks, cur, size = [], [], 0
    for ch in text:
        n = len(ch.encode('utf-8'))
        if size + n > limit and cur:
            chunks.append(''.join(cur).encode('utf-8'))
            cur, size = [], 0
        cur.append(ch)
        size += n
    chunks.append(''.join(cur).encode('utf-8'))
    return chunks


def decode(pb):
    """Decode any Even AI message from the glasses into a dict:
    {cmd, name, magic, value, error, label} where `value` is the sub-message's first field
    (CTRL status, VAD status, EVENT kind, …) or None, and `label` is human-readable."""
    cmd = eh.read_varint_field(pb, 1, -1)
    magic = eh.read_varint_field(pb, 2, -1)
    name = CMD_NAMES.get(cmd, f'cmd={cmd}')
    value = error = None
    field = FIELD_FOR_CMD.get(cmd)
    sub = eh.read_bytes_field(pb, field) if field else None
    label = name
    if sub is not None:
        if cmd == CMD_COMM_RSP:
            error = eh.read_varint_field(sub, 1, 0)
        else:
            value = eh.read_varint_field(sub, 1, 0)
            error = eh.read_varint_field(sub, 2, 0)
        names = {CMD_CTRL: CTRL_NAMES, CMD_VAD_INFO: VAD_NAMES, CMD_EVENT: EVENT_NAMES,
                 CMD_PROMPT: PROMPT_NAMES}.get(cmd)
        if names is not None:
            label += f' {names.get(value, value)}'
        if error:
            label += f' errorCode={error}'
    return {'cmd': cmd, 'name': name, 'magic': magic, 'value': value, 'error': error, 'label': label}
