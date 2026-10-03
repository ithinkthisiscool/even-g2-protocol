"""Translate (service 0x05) — live two-language subtitles on the glasses. DECODED, untested live.

Plain English: the phone listens to speech, translates it, and sends each pair of lines
(original + translation) to the glasses, which show them as subtitles. This module lets our hub
play the phone's part. Every byte layout below was read from the official app's decompile
(blutter, `ProtoTranslateExt` / `TranslationService`); none of it has been seen working live yet.
Spec: esp32_ai/docs/research/native_apps_findings.md §1.

How a session works:

    app     → CTRL OPEN {languagePair "EN>ES", srcKey, dstKey}   opens the Translate screen
    glasses → COMM_RESP {errorCode}          0 = OK (every app send gets one of these)
    app     → HEARTBEAT every 3–5 s          keeps the session open (glasses send their own too)
    app     → RESULT {srcText, dstText, endFlag}   one per update; endFlag 1 commits the line
    glasses → NOTIFY PAUSE / RESUME / CLOSE  the user acted on the glasses
    glasses → LANG_SWITCH_REQ {langKey}      the user picked a language; app answers LANG_SWITCH_RSP
    app     → CTRL CLOSE                     closes the screen

Every message is a `TranslateDataPackage` protobuf:
    1 commandId   which command this is (CMD_* below)
    2 magicRandom request id. Use a FRESH value for every packet: the glasses reject a repeated
                  one with error 66 ("magic random duplicate"); the app then waits 1 s and retries
                  with a new magic (up to 5 tries).
    3..10         exactly one sub-message, chosen by the command (FIELD_FOR_CMD below)

Text is UTF-8 with NO NUL terminator. Functions here only build or decode bytes; framing and
sending are the caller's job:
    frames = evenhub.frame_pb(translate.build_open(magic, 'EN', 'ES'), translate.SID,
                              evenhub.FLAG_REQUEST, session.next_seq())
"""
from . import evenhub as eh

SID = 0x05

# commandId (eTranslateCommandId)
CMD_CTRL = 1                 # app→glasses
CMD_RESULT = 2               # app→glasses
CMD_NOTIFY = 161             # glasses→app: OPEN / CLOSE / PAUSE / RESUME
CMD_COMM_RESP = 162          # glasses→app: reply to every app send
CMD_MODE_SWITCH = 163        # glasses→app
CMD_LANG_SWITCH_REQ = 164    # glasses→app: user chose a language on the glasses
CMD_LANG_SWITCH_RSP = 165    # app→glasses: answer to 164
CMD_HEARTBEAT = 255          # both directions

CMD_NAMES = {CMD_CTRL: 'CTRL', CMD_RESULT: 'RESULT', CMD_NOTIFY: 'NOTIFY',
             CMD_COMM_RESP: 'COMM_RESP', CMD_MODE_SWITCH: 'MODE_SWITCH',
             CMD_LANG_SWITCH_REQ: 'LANG_SWITCH_REQ', CMD_LANG_SWITCH_RSP: 'LANG_SWITCH_RSP',
             CMD_HEARTBEAT: 'HEARTBEAT'}

# Which package field carries the sub-message for each command.
FIELD_FOR_CMD = {CMD_CTRL: 3, CMD_RESULT: 4, CMD_NOTIFY: 5, CMD_MODE_SWITCH: 6,
                 CMD_COMM_RESP: 7, CMD_HEARTBEAT: 8, CMD_LANG_SWITCH_REQ: 9,
                 CMD_LANG_SWITCH_RSP: 10}
ERROR_FIELD = 7              # COMM_RESP: the field the app reads for success (0)

# TranslateCmd (TranslateControl.cmd and TranslateNotify.cmd)
TCMD_NONE = 0
TCMD_OPEN = 1
TCMD_CLOSE = 2
TCMD_PAUSE = 3
TCMD_RESUME = 4
TCMD_NAMES = {TCMD_NONE: 'NONE', TCMD_OPEN: 'OPEN', TCMD_CLOSE: 'CLOSE',
              TCMD_PAUSE: 'PAUSE', TCMD_RESUME: 'RESUME'}

# TranslateErrorCode (plus 66, which the app's retry loop checks for)
ERR_OK = 0
ERR_FAIL = 1
ERR_NETWORK = 2
ERR_NOT_SUPPORT = 3
ERR_FALLBACK_FAIL = 4
ERR_MAGIC_DUPLICATE = 66
ERROR_NAMES = {ERR_OK: 'OK', ERR_FAIL: 'FAIL', ERR_NETWORK: 'NETWORK',
               ERR_NOT_SUPPORT: 'NOT_SUPPORT', ERR_FALLBACK_FAIL: 'FALLBACK_FAIL',
               ERR_MAGIC_DUPLICATE: 'MAGIC_DUPLICATE'}

HEARTBEAT_INTERVAL_S = 4.0       # app uses a 3 s keep-alive and a 6 s staleness check; 3–5 s is safe
MAGIC_RETRY_DELAY_S = 1.0        # app waits this long before retrying after ERR_MAGIC_DUPLICATE
MAGIC_RETRY_MAX = 5


def build_package(cmd, magic, inner):
    """Wrap a sub-message in TranslateDataPackage. `inner` may be b'' (e.g. HEARTBEAT)."""
    return (eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(FIELD_FOR_CMD[cmd], inner))


def _code(lang):
    return lang.strip().upper()


def build_open(magic, src_lang, dst_lang, src_key=None, dst_key=None, lang_list=None):
    """CTRL OPEN {1 cmd=OPEN, 2 languagePair "SRC>DST", 5 langList, 6 srcKey, 7 dstKey}.
    Language codes are upper-cased like the app does ("en" → "EN"; special cases "AUTO", "PT").
    src_key/dst_key default to the codes. lang_list is an optional list of (key, text) pairs for
    the on-glasses language menu. useAudio (3) is deliberately left unset so the glasses do not
    stream their mic. STATUS: DECODED (sendStartTranslate), untested live."""
    src, dst = _code(src_lang), _code(dst_lang)
    inner = eh.encode_varint_field(1, TCMD_OPEN) + eh.encode_string_field(2, f'{src}>{dst}')
    for key, text in lang_list or ():
        inner += eh.encode_message_field(5, eh.encode_string_field(1, key) +
                                         eh.encode_string_field(2, text))
    inner += eh.encode_string_field(6, _code(src_key) if src_key else src)
    inner += eh.encode_string_field(7, _code(dst_key) if dst_key else dst)
    return build_package(CMD_CTRL, magic, inner)


def build_ctrl(magic, tcmd):
    """CTRL {1 cmd}: TCMD_PAUSE / TCMD_RESUME / TCMD_CLOSE. STATUS: DECODED, untested live."""
    return build_package(CMD_CTRL, magic, eh.encode_varint_field(1, tcmd))


def build_pause(magic):
    """CTRL PAUSE. STATUS: DECODED, untested live."""
    return build_ctrl(magic, TCMD_PAUSE)


def build_resume(magic):
    """CTRL RESUME. STATUS: DECODED, untested live."""
    return build_ctrl(magic, TCMD_RESUME)


def build_close(magic):
    """CTRL CLOSE (sendStopTranslate; errorCode 0 is omitted). STATUS: DECODED, untested live."""
    return build_ctrl(magic, TCMD_CLOSE)


def build_text(magic, src_text, dst_text, final=True, speaker=None):
    """RESULT {1 srcText, 2 dstText, 4 endFlag, 5 speaker}. Both texts are trimmed like the app
    does, except a lone " " translation which is kept. final=False (endFlag 0) is a live partial
    that the glasses should replace; final=True (endFlag 1) commits the line (INFERRED).
    errorCode 0 and endFlag 0 are proto3 defaults and are not written.
    STATUS: DECODED (sendTranslateResult), untested live."""
    dst = dst_text if dst_text == ' ' else dst_text.strip()
    inner = eh.encode_string_field(1, src_text.strip()) + eh.encode_string_field(2, dst)
    if final:
        inner += eh.encode_varint_field(4, 1)
    if speaker:
        inner += eh.encode_bytes_field(5, speaker if isinstance(speaker, bytes) else speaker.encode('utf-8'))
    return build_package(CMD_RESULT, magic, inner)


def build_heartbeat(magic):
    """HEARTBEAT {8:{}}: send every HEARTBEAT_INTERVAL_S. STATUS: DECODED, untested live."""
    return build_package(CMD_HEARTBEAT, magic, b'')


def build_lang_switch_response(magic, error=ERR_OK):
    """LANG_SWITCH_RSP {10:{1 errorCode}}: answer to a glasses LANG_SWITCH_REQ (cmd 164).
    STATUS: DECODED (sendTranslateLangSwitchResponse), untested live."""
    inner = eh.encode_varint_field(1, error) if error else b''
    return build_package(CMD_LANG_SWITCH_RSP, magic, inner)


def decode(pb):
    """Decode a Translate message from the glasses into a dict:
    {cmd, name, magic, value, lang_key, error, label}. `value` is the NOTIFY TranslateCmd or the
    MODE_SWITCH mode (else None); `lang_key` is set for LANG_SWITCH_REQ; `error` is the errorCode
    (COMM_RESP / NOTIFY) or None. STATUS: DECODED, untested live."""
    cmd = eh.read_varint_field(pb, 1, -1)
    magic = eh.read_varint_field(pb, 2, -1)
    name = CMD_NAMES.get(cmd, f'cmd={cmd}')
    field = FIELD_FOR_CMD.get(cmd)
    sub = eh.read_bytes_field(pb, field) if field else None
    value = error = lang_key = None
    label = name
    if sub is not None:
        if cmd == CMD_NOTIFY:
            value = eh.read_varint_field(sub, 1, 0)
            error = eh.read_varint_field(sub, 2, 0)
            label += f' {TCMD_NAMES.get(value, value)}'
        elif cmd == CMD_MODE_SWITCH:
            value = eh.read_varint_field(sub, 1, 0)
            label += f' mode={value}'
        elif cmd == CMD_LANG_SWITCH_REQ:
            lang_key = eh.read_string_field(sub, 1, '')
            label += f' {lang_key}'
        elif cmd in (CMD_COMM_RESP, CMD_LANG_SWITCH_RSP):
            error = eh.read_varint_field(sub, 1, 0)
            if cmd == CMD_COMM_RESP:
                label += f' {ERROR_NAMES.get(error, error)}'
        elif cmd == CMD_CTRL:
            value = eh.read_varint_field(sub, 1, 0)
            error = eh.read_varint_field(sub, 4, 0)
            label += f' {TCMD_NAMES.get(value, value)}'
        if error and cmd != CMD_COMM_RESP:
            label += f' errorCode={ERROR_NAMES.get(error, error)}'
    return {'cmd': cmd, 'name': name, 'magic': magic, 'value': value, 'lang_key': lang_key,
            'error': error, 'label': label}
