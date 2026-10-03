"""Teleprompter (service 0x06) — a scrolling script on the glasses. DECODED, untested live.

Plain English: the phone splits a script into pages of up to 10 pre-wrapped lines, tells the
glasses how many pages and lines there are, and then answers the glasses' requests for each page.
The glasses scroll through the text themselves (by touch in MANUAL mode, on a timer in AUTO mode)
and report where they are. Every byte layout below was read from the official app's decompile
(blutter, `ProtoTeleprompterExt` / `TeleprompterServiceV2`); none of it has been seen working
live yet. Spec: esp32_ai/docs/research/native_apps_findings.md §3.

How a session works (app-initiated):

    app     → CONTROL START {settings: mode, totalPages, totalLines, displayWidth 567, displayLines 9}
    glasses → COMM_RESP {errCode}          0 = OK (the app retries START once)
    glasses → PAGE_DATA_REQUEST {pageId}   0-based; the app answers PAGE_DATA
    app     → PAGE_DATA {pageId, lineCount, "line\\nline\\nline \\n"}
    glasses → PAGE_LOAD_DONE {start, end}  which pages the glasses now hold
    app     → HEARTBEAT every 6 s {appPageId, appLineId}  (app gives up after 10 lost)
    glasses → PAGE_SCROLL_SYNC {page, line}  the user scrolled (MANUAL mode)
    app     → CONTROL PAUSE / RESUME / CLOSE
    glasses → STATUS_NOTIFY CLOSE or END_SESSION_REQUEST   the user exited; stop the session

Launched from the glasses menu instead: glasses FILE_LIST_REQUEST (162) → app FILE_LIST (2) →
glasses FILE_SELECT (163) {fileId} → app CONTROL START → page requests as above.

Every message is a `TelepromptDataPackage` protobuf:
    1 commandId   which command this is (CMD_* below)
    2 magicRandom request id; use a fresh one per packet
    3..17         exactly one sub-message, chosen by the command (FIELD_FOR_CMD below)

Text is UTF-8 with NO NUL terminator. Zero-valued numbers are proto3 defaults and are not
written (the decompiled examples omit them too). Functions here only build or decode bytes.
"""
import textwrap

from . import evenhub as eh

SID = 0x06

# commandId (TelepromptCommandId)
CMD_CONTROL = 1              # both
CMD_FILE_LIST = 2            # app→glasses
CMD_PAGE_DATA = 3            # app→glasses
CMD_PAGE_AI_SYNC = 4         # app→glasses
CMD_STATUS_NOTIFY = 161      # glasses→app
CMD_FILE_LIST_REQUEST = 162  # glasses→app (menu launch)
CMD_FILE_SELECT = 163        # glasses→app (menu launch)
CMD_PAGE_DATA_REQUEST = 164  # glasses→app
CMD_PAGE_SCROLL_SYNC = 165   # both
CMD_COMM_RESP = 166          # glasses→app
CMD_PAGE_LOAD_DONE = 167     # glasses→app
CMD_MODE_SWITCH_REQ = 168    # glasses→app
CMD_MODE_SWITCH_RSP = 169    # app→glasses
CMD_END_SESSION_REQUEST = 170  # glasses→app
CMD_HEARTBEAT = 255          # both

CMD_NAMES = {CMD_CONTROL: 'CONTROL', CMD_FILE_LIST: 'FILE_LIST', CMD_PAGE_DATA: 'PAGE_DATA',
             CMD_PAGE_AI_SYNC: 'PAGE_AI_SYNC', CMD_STATUS_NOTIFY: 'STATUS_NOTIFY',
             CMD_FILE_LIST_REQUEST: 'FILE_LIST_REQUEST', CMD_FILE_SELECT: 'FILE_SELECT',
             CMD_PAGE_DATA_REQUEST: 'PAGE_DATA_REQUEST', CMD_PAGE_SCROLL_SYNC: 'PAGE_SCROLL_SYNC',
             CMD_COMM_RESP: 'COMM_RESP', CMD_PAGE_LOAD_DONE: 'PAGE_LOAD_DONE',
             CMD_MODE_SWITCH_REQ: 'MODE_SWITCH_REQ', CMD_MODE_SWITCH_RSP: 'MODE_SWITCH_RSP',
             CMD_END_SESSION_REQUEST: 'END_SESSION_REQUEST', CMD_HEARTBEAT: 'HEARTBEAT'}

FIELD_FOR_CMD = {CMD_CONTROL: 3, CMD_FILE_LIST: 4, CMD_PAGE_DATA: 5, CMD_PAGE_AI_SYNC: 6,
                 CMD_STATUS_NOTIFY: 7, CMD_FILE_LIST_REQUEST: 8, CMD_FILE_SELECT: 9,
                 CMD_PAGE_DATA_REQUEST: 10, CMD_PAGE_SCROLL_SYNC: 11, CMD_COMM_RESP: 12,
                 CMD_HEARTBEAT: 13, CMD_PAGE_LOAD_DONE: 14, CMD_MODE_SWITCH_REQ: 15,
                 CMD_MODE_SWITCH_RSP: 16, CMD_END_SESSION_REQUEST: 17}
ERROR_FIELD = 12             # COMM_RESP: the field the app reads for success (0)

# TelepromptCtrlCmd
CTRL_NONE = 0
CTRL_START = 1
CTRL_PAUSE = 2
CTRL_RESUME = 3
CTRL_CLOSE = 4
CTRL_NAMES = {CTRL_NONE: 'NONE', CTRL_START: 'START', CTRL_PAUSE: 'PAUSE',
              CTRL_RESUME: 'RESUME', CTRL_CLOSE: 'CLOSE'}

# TelepromptMode
MODE_AI = 0
MODE_MANUAL = 1
MODE_AUTO = 2
MODE_NAMES = {MODE_AI: 'AI', MODE_MANUAL: 'MANUAL', MODE_AUTO: 'AUTO'}

# TelepromptErrorCode
ERROR_NAMES = {0: 'SUCCESS', 1: 'FAIL', 2: 'CLOSED', 3: 'REPEATED_MESSAGE',
               4: 'PD_DECODE_FAIL', 5: 'FALLBACK_FAIL'}

DISPLAY_WIDTH = 567          # LayoutConfigs.visionWidth default
DISPLAY_LINES = 9            # pageLine default (lines visible at once)
LINES_PER_PAGE = 10          # setupDataList splits the script into pages of 10 lines
CHARS_PER_LINE = 28          # INFERRED: the real width in characters is unknown (try 25–30)
FILENAME_MAX_CHARS = 30      # getLimitString(name, 30)
HEARTBEAT_INTERVAL_S = 6.0
HEARTBEAT_MAX_LOST = 10


def build_package(cmd, magic, inner):
    """Wrap a sub-message in TelepromptDataPackage. `inner` may be b'' (e.g. HEARTBEAT)."""
    return (eh.encode_varint_field(1, cmd) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(FIELD_FOR_CMD[cmd], inner))


def _nz(field, value):
    """Varint field, or nothing when value is 0 (proto3 default)."""
    return eh.encode_varint_field(field, value) if value else b''


def paginate(script, chars_per_line=CHARS_PER_LINE, lines_per_page=LINES_PER_PAGE):
    """Word-wrap `script` into lines of ≤chars_per_line characters and group them into pages of
    ≤lines_per_page lines. Existing line breaks are kept; blank lines stay as empty lines; words
    longer than a line are split. Returns a list of pages, each a list of line strings
    ([] for an empty script)."""
    lines = []
    for para in script.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        para = para.expandtabs(4).strip()
        lines.extend(textwrap.wrap(para, chars_per_line, break_on_hyphens=False) or [''])
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)]


def totals(pages):
    """(totalPages, totalLines) for build_start, from paginate()'s output."""
    return len(pages), sum(len(p) for p in pages)


def build_settings(total_pages, total_lines, mode=MODE_MANUAL, start_page=0, start_line=0,
                   display_width=DISPLAY_WIDTH, scroll_interval_ms=0, countdown_seconds=0,
                   display_lines=DISPLAY_LINES):
    """TelepromptSetting {1 mode, 2 startPageId, 3 startLineId, 4 totalPages, 5 totalLines,
    6 displayWidth, 7 scrollIntervalMs, 8 countdownSeconds, 9 displayLines}. scrollMode (10) is
    always CONTINUOUS = 0 and so never written. scroll_interval_ms matters for MODE_AUTO."""
    return (_nz(1, mode) + _nz(2, start_page) + _nz(3, start_line) + _nz(4, total_pages) +
            _nz(5, total_lines) + _nz(6, display_width) + _nz(7, scroll_interval_ms) +
            _nz(8, countdown_seconds) + _nz(9, display_lines))


def build_start(magic, total_pages, total_lines, mode=MODE_MANUAL, **settings):
    """CONTROL START {1 cmd=START, 2 settings}. Extra keyword arguments go to build_settings.
    STATUS: DECODED (startTeleprompter 0x2b68950), untested live."""
    inner = (eh.encode_varint_field(1, CTRL_START) +
             eh.encode_message_field(2, build_settings(total_pages, total_lines, mode, **settings)))
    return build_package(CMD_CONTROL, magic, inner)


def build_ctrl(magic, ctrl):
    """CONTROL {1 cmd}: CTRL_PAUSE / CTRL_RESUME / CTRL_CLOSE. STATUS: DECODED, untested live."""
    return build_package(CMD_CONTROL, magic, eh.encode_varint_field(1, ctrl))


def build_pause(magic):
    """CONTROL PAUSE. STATUS: DECODED, untested live."""
    return build_ctrl(magic, CTRL_PAUSE)


def build_resume(magic):
    """CONTROL RESUME. STATUS: DECODED, untested live."""
    return build_ctrl(magic, CTRL_RESUME)


def build_close(magic):
    """CONTROL CLOSE. STATUS: DECODED, untested live."""
    return build_ctrl(magic, CTRL_CLOSE)


def page_text(lines):
    """The pageText bytes for one page: lines joined with "\\n" plus a trailing " \\n"."""
    return ('\n'.join(lines) + ' \n').encode('utf-8')


def build_page(magic, page_id, lines):
    """PAGE_DATA {1 pageId, 2 pageLineCount, 3 pageText}: the answer to PAGE_DATA_REQUEST.
    `lines` is one page from paginate(). STATUS: DECODED (sendTeleprompterPageData), untested live."""
    inner = _nz(1, page_id) + _nz(2, len(lines)) + eh.encode_bytes_field(3, page_text(lines))
    return build_package(CMD_PAGE_DATA, magic, inner)


def build_heartbeat(magic, page_id=0, line_id=0):
    """HEARTBEAT {13:{1 appPageId, 2 appLineId}}: send every HEARTBEAT_INTERVAL_S.
    STATUS: DECODED (_startCustomHeartbeat), untested live."""
    return build_package(CMD_HEARTBEAT, magic, _nz(1, page_id) + _nz(2, line_id))


def build_scroll_sync(magic, page_id, line_id):
    """PAGE_SCROLL_SYNC {11:{1 pageId, 2 lineId}}: move the glasses' view.
    STATUS: DECODED (sendTeleprompterScrollSyncEvent), untested live."""
    return build_package(CMD_PAGE_SCROLL_SYNC, magic, _nz(1, page_id) + _nz(2, line_id))


def build_ai_sync(magic, page_id, line_id, char_id):
    """PAGE_AI_SYNC {6:{1 pageId, 2 lineId, 3 charId}}: position for MODE_AI (speech following).
    STATUS: DECODED from the schema only, untested live."""
    return build_package(CMD_PAGE_AI_SYNC, magic, _nz(1, page_id) + _nz(2, line_id) + _nz(3, char_id))


def build_file_list(magic, files, menu_mode=None):
    """FILE_LIST {4:{1 files[] {1 fileId, 2 filename}, 2 menuConfig{1 mode}}}: the answer to
    FILE_LIST_REQUEST (menu launch). `files` is a list of (file_id, name); names are cut to
    FILENAME_MAX_CHARS characters. menuConfig is written only when menu_mode is given.
    STATUS: DECODED, untested live."""
    inner = b''
    for file_id, name in files:
        fid = file_id if isinstance(file_id, bytes) else str(file_id).encode('utf-8')
        inner += eh.encode_message_field(1, eh.encode_bytes_field(1, fid) +
                                         eh.encode_string_field(2, name[:FILENAME_MAX_CHARS]))
    if menu_mode is not None:
        inner += eh.encode_message_field(2, _nz(1, menu_mode))
    return build_package(CMD_FILE_LIST, magic, inner)


def build_mode_switch_response(magic, error=0):
    """MODE_SWITCH_RSP {16:{1 errCode}}: the answer to MODE_SWITCH_REQ (168).
    STATUS: DECODED from the schema, untested live."""
    return build_package(CMD_MODE_SWITCH_RSP, magic, _nz(1, error))


def decode(pb):
    """Decode a Teleprompter message from the glasses into a dict:
    {cmd, name, magic, page, line, start_page, end_page, value, file_id, error, label}.
    Unused keys are None. `value` is the ctrl cmd (STATUS_NOTIFY / CONTROL) or the mode
    (MODE_SWITCH_REQ). STATUS: DECODED, untested live."""
    cmd = eh.read_varint_field(pb, 1, -1)
    magic = eh.read_varint_field(pb, 2, -1)
    name = CMD_NAMES.get(cmd, f'cmd={cmd}')
    field = FIELD_FOR_CMD.get(cmd)
    sub = eh.read_bytes_field(pb, field) if field else None
    out = {'cmd': cmd, 'name': name, 'magic': magic, 'page': None, 'line': None,
           'start_page': None, 'end_page': None, 'value': None, 'file_id': None, 'error': None}
    label = name
    if sub is not None:
        v = lambda n: eh.read_varint_field(sub, n, 0)  # noqa: E731
        if cmd == CMD_PAGE_DATA_REQUEST:
            out['page'] = v(1)
            label += f' page={out["page"]}'
        elif cmd == CMD_PAGE_SCROLL_SYNC:
            out['page'], out['line'] = v(1), v(2)
            label += f' page={out["page"]} line={out["line"]}'
        elif cmd == CMD_HEARTBEAT:
            out['page'], out['line'] = v(3), v(4)          # osPageId / osLineId
            label += f' page={out["page"]} line={out["line"]}'
        elif cmd == CMD_PAGE_LOAD_DONE:
            out['start_page'], out['end_page'] = v(1), v(2)
            label += f' pages={out["start_page"]}..{out["end_page"]}'
        elif cmd == CMD_STATUS_NOTIFY:
            out['value'], out['error'] = v(1), v(2)
            label += f' {CTRL_NAMES.get(out["value"], out["value"])}'
        elif cmd == CMD_CONTROL:
            out['value'], out['error'] = v(1), v(3)
            label += f' {CTRL_NAMES.get(out["value"], out["value"])}'
        elif cmd == CMD_FILE_SELECT:
            out['file_id'] = eh.read_bytes_field(sub, 1) or b''
            label += f' file={out["file_id"].decode("utf-8", "replace")}'
        elif cmd == CMD_MODE_SWITCH_REQ:
            out['value'] = v(1)
            label += f' {MODE_NAMES.get(out["value"], out["value"])}'
        elif cmd in (CMD_COMM_RESP, CMD_MODE_SWITCH_RSP):
            out['error'] = v(1)
            if cmd == CMD_COMM_RESP:
                label += f' {ERROR_NAMES.get(out["error"], out["error"])}'
        if out['error'] and cmd != CMD_COMM_RESP:
            label += f' errCode={ERROR_NAMES.get(out["error"], out["error"])}'
    out['label'] = label
    return out


def is_close(msg):
    """True when a decode() result means the glasses ended the session (cmd 170 or notify CLOSE)."""
    return (msg['cmd'] == CMD_END_SESSION_REQUEST or
            (msg['cmd'] == CMD_STATUS_NOTIFY and msg['value'] == CTRL_CLOSE))
