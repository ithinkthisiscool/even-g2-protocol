"""Quicklist / tasks (SID 0x0c): the glasses' to-do list widget, both directions.

Plain English
-------------
The phone owns the task list and pushes it to the glasses; on the glasses the user can only tick
tasks done/undone and scroll. There is no "give me the list" command: the phone is the source of
truth. Each task has a stable `uid` -- the glasses report ticks by uid, so never renumber tasks.

App -> glasses (builders return protobuf bytes; frame with SID_QUICKLIST + FLAG_REQUEST):
    build_quicklist_full_update(items, magic)   replace the whole list        LIVE-CONFIRMED
    build_quicklist_add_page(items, magic)      answer a paging request       untested live
    build_quicklist_keepalive(magic)            empty push the app repeats    (from btsnoop)
  items are tuples (uid, index, is_completed, timestamp_s, title, ts_type).
  Titles are sent as UTF-8 BYTES cut at <=200 bytes plus a trailing NUL (00) byte, not as a
  plain protobuf string (encode_title). At most 20 items per packet. The glasses ack by echoing
  cmd + magic on SID 0x0c.

Glasses -> app (decoders take the protobuf payload of an incoming SID 0x0c frame):
    decode_status_updates(pb)   list of {uid, index, completed, title} ticks   untested live
    decode_event(pb)            (EVENT_UPDATE_DATA_UP/DOWN, pivot_uid) paging  untested live
    page_around(tasks, ev, uid) which <=20 tasks to send back with build_quicklist_add_page

Before the first push, the dashboard base settings (g2.dashboard.build_dashboard_init) must have
been sent and the EvenHub heartbeat must be running; after the ack, stop the heartbeat so the
native dashboard shows the list.

Schema source: same extraction method as g2/dashboard.py and g2/menu.py (blutter decompile),
corrected by btsnoop captures.

SID = 0x0c (UI_QUICKLIST_APP_ID, from service_id_def -- see
esp32_ai/docs/research/unknowns_findings.md section 3).

Schema (CORRECTED 2026-09-28 via btsnoop capture of real EvenHub app):
  QuicklistDataPackage (top-level envelope):
    1  commandId  enum eQuicklistCommandId (0=NONE_COMMAND, 1=ITEM, 2=MULT_ITEMS, 3=EVENT)
    2  magic      int32  ← PRESENT (btsnoop confirmed); device echoes it in ack
    3  item       -> QuicklistItem
    4  multItems  -> QuicklistMultItems  ← field 4, NOT 3 (schema was off by one due to missing magic)
    5  event      -> QuicklistEvent

  Prior incorrect schema had no magic field, item in f2, multItems in f3.  The device DOES
  acknowledge with the same cmdId+magic, so magic-based ack matching works on SID=0x0c.

  Real app keepalive pattern (btsnoop): periodically sends CMD_MULT_ITEMS with empty multItems
  (totalCount=0, no items) to keep the SID=0x0c connection alive.  Ack: device echoes cmdId+magic
  with field 4 = {f1=1} (success indicator).

  QuicklistItem (one task/checklist entry):
    1  uid          int32
    2  index        int32
    3  isCompleted  int32 (0/1)
    4  timestamp    Int64
    5  title        string
    6  errorCode    enum eErrorCode (shared enum, values not extracted here)
    7  tsType       enum eQuicklistTsType (0=DATETIME, 1=DATE, 2=TIME)

  QuicklistMultItems (a full-list push):
    1  dataType    enum eQuicklistDataType (0=UNKNOWN_TYPE, 1=FULL_UPDATE, 2=ADD, 3=MODIFY,
                                             4=DELETE)
    2  totalCount  int32
    3  items       -> QuicklistItem (repeated)
    4  errorCode   enum eErrorCode

  QuicklistEvent (glasses -> app paging request):
    1  event  enum (0=NONE, 1=UPDATE_DATA_UP, 2=UPDATE_DATA_DOWN)
    2  uid    pivot uid
    3  errorCode

  Glasses -> app (blutter: ProtoQuicklistExt.onListenOsPushEvent, OsOperation): ITEM(f3) and
  MULT_ITEMS(f4) are completion-status updates only (dataType is logged, never branched on);
  EVENT is paging, answered with MULT_ITEMS ADD. The glasses cannot add/delete/reorder, and
  there is no app->glasses "pull list" command -- the phone is the source of truth.
  App encoding: title = UTF-8 <=200 bytes + NUL; <=20 items per packet; no reminder ->
  timestamp=0, tsType=TIME.
"""
import sys
import os

from . import evenhub as eh

SID_QUICKLIST = 0x0c

CMD_NONE = 0
CMD_ITEM = 1
CMD_MULT_ITEMS = 2
CMD_EVENT = 3

DATA_TYPE_UNKNOWN = 0
DATA_TYPE_FULL_UPDATE = 1
DATA_TYPE_ADD = 2
DATA_TYPE_MODIFY = 3
DATA_TYPE_DELETE = 4

TS_TYPE_DATETIME = 0
TS_TYPE_DATE = 1
TS_TYPE_TIME = 2


TITLE_MAX_BYTES = 200
MAX_ITEMS_PER_PACKET = 20

EVENT_UPDATE_DATA_UP = 1
EVENT_UPDATE_DATA_DOWN = 2


def encode_title(title):
    """App's _encodeTitleBytes: UTF-8, cut on a codepoint boundary at <=200 bytes, plus NUL.
    encode_title('Buy milk') -> b'Buy milk\x00'."""
    b = title.encode('utf-8')[:TITLE_MAX_BYTES]
    return b.decode('utf-8', 'ignore').encode('utf-8') + b'\x00'


def encode_quicklist_item(uid, index, is_completed, timestamp, title, ts_type=TS_TYPE_TIME):
    """One QuicklistItem. timestamp is seconds; the app sends timestamp=0 + tsType=TIME for tasks
    with no reminder. The title goes in field 5 as NUL-terminated bytes (encode_title)."""
    out = eh.encode_varint_field(1, uid)
    out += eh.encode_varint_field(2, index)
    out += eh.encode_varint_field(3, 1 if is_completed else 0)
    out += eh.encode_varint_field(4, timestamp)
    if title:
        out += eh.encode_bytes_field(5, encode_title(title))
    out += eh.encode_varint_field(7, ts_type)
    return out


def encode_quicklist_mult_items(items, data_type=DATA_TYPE_FULL_UPDATE, total_count=None):
    """QuicklistMultItems {1 dataType, 2 totalCount, 3 item...}.
    items: list of (uid, index, is_completed, timestamp, title, ts_type)."""
    total = total_count if total_count is not None else len(items)
    out = eh.encode_varint_field(1, data_type)
    out += eh.encode_varint_field(2, total)
    for it in items:
        out += eh.encode_message_field(3, encode_quicklist_item(*it))
    return out


def build_quicklist_full_update(items, magic=0):
    """Top-level QuicklistDataPackage payload replacing the whole quicklist. LIVE-CONFIRMED.
    Pass at most MAX_ITEMS_PER_PACKET items (the function does not cut the list).

    CORRECTED 2026-09-28 (btsnoop analysis):
      - field 2 = magic (device echoes it in ack — use session.next_magic())
      - multItems goes in field 4 (NOT field 3; prior schema was missing the magic field)

    items: list of (uid, index, is_completed, timestamp, title, ts_type)
    magic: int32 random value; device echoes in ack for correlation (use session.next_magic())
    """
    mult_items = encode_quicklist_mult_items(items, data_type=DATA_TYPE_FULL_UPDATE)
    out = eh.encode_varint_field(1, CMD_MULT_ITEMS)
    out += eh.encode_varint_field(2, magic)
    out += eh.encode_message_field(4, mult_items)  # field 4, NOT 3
    return out


def build_quicklist_add_page(items, magic=0):
    """Reply to a glasses EVENT UPDATE_DATA_UP/DOWN (paging): MULT_ITEMS dataType=ADD with <=20
    items before/after the pivot uid, or an empty ADD when there is no more data."""
    mult = encode_quicklist_mult_items(items[:MAX_ITEMS_PER_PACKET], data_type=DATA_TYPE_ADD)
    return (eh.encode_varint_field(1, CMD_MULT_ITEMS) + eh.encode_varint_field(2, magic) +
            eh.encode_message_field(4, mult))


def build_quicklist_keepalive(magic=0):
    """Empty CMD_MULT_ITEMS with no items — the real app sends this periodically to keep
    SID=0x0c alive (btsnoop confirmed). Device echoes magic in ack."""
    empty = encode_quicklist_mult_items([], data_type=DATA_TYPE_FULL_UPDATE, total_count=0)
    out = eh.encode_varint_field(1, CMD_MULT_ITEMS)
    out += eh.encode_varint_field(2, magic)
    out += eh.encode_message_field(4, empty)
    return out


# ── Messages from the glasses ────────────────────────────────────────────────────────────────

def decode_item(raw):
    """QuicklistItem bytes → {uid, index, completed, title}. Title may be None (the glasses
    usually send only uid + isCompleted when a task is ticked)."""
    title = eh.read_bytes_field(raw, 5)
    if title is not None:
        title = title.rstrip(b'\x00').decode('utf-8', 'replace')
    return {'uid': eh.read_varint_field(raw, 1, 0), 'index': eh.read_varint_field(raw, 2, 0),
            'completed': bool(eh.read_varint_field(raw, 3, 0)), 'title': title}


def decode_status_updates(pb):
    """Completion changes the user made on-glass, as a list of decode_item() dicts.
    Like the official app, both ITEM (field 3) and MULT_ITEMS (field 4) are treated purely as
    status updates — the glasses cannot add, delete or reorder tasks."""
    cmd = eh.read_varint_field(pb, 1, -1)
    if cmd == CMD_ITEM:
        raw = eh.read_bytes_field(pb, 3)
        return [decode_item(raw)] if raw is not None else []
    if cmd == CMD_MULT_ITEMS:
        mult = eh.read_bytes_field(pb, 4)
        if mult is not None:
            return [decode_item(v) for f, w, v in eh._iter_fields(mult) if f == 3 and w == 2]
    return []


def decode_event(pb):
    """EVENT (field 5) → (event, pivot_uid), e.g. (EVENT_UPDATE_DATA_DOWN, 20) when the user
    scrolls past the 20th task. Returns None if this is not an EVENT."""
    if eh.read_varint_field(pb, 1, -1) != CMD_EVENT:
        return None
    ev = eh.read_bytes_field(pb, 5)
    if ev is None:
        return None
    return eh.read_varint_field(ev, 1, 0), eh.read_varint_field(ev, 2, 0)


def page_around(tasks, event, pivot_uid):
    """Pick the tasks to answer a paging EVENT with: up to MAX_ITEMS_PER_PACKET before the pivot
    (UP) or after it (DOWN). `tasks` is the full ordered list of dicts with a 'uid' key.
    An unknown pivot gives an empty page, which tells the glasses there is nothing more."""
    idx = next((i for i, t in enumerate(tasks) if t['uid'] == pivot_uid), None)
    if idx is None:
        return []
    if event == EVENT_UPDATE_DATA_UP:
        return tasks[max(0, idx - MAX_ITEMS_PER_PACKET):idx]
    return tasks[idx + 1:idx + 1 + MAX_ITEMS_PER_PACKET]
