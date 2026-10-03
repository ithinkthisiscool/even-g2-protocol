"""Wire format for the G2: protobuf field encoding, frame building (framing), and EvenHub pages.

This is the module every other g2 module builds on. It has three jobs.

1. Protobuf field encoding (encode_* / read_* / decode_varint_at)
   Most payloads are protobuf: a list of numbered fields. Each field starts with a "key" =
   (field_number << 3) | wire_type, written as a varint. Wire types used here:
       0 varint             a whole number, 7 bits per byte, low bits first; high bit = "more".
                            Example: 300 -> ac 02.   encode_varint_field(1, 2) -> 08 02
       2 length-delimited   varint length, then that many bytes (a string, raw bytes, or a
                            nested message). encode_string_field(2, "hi") -> 12 02 68 69
       5 fixed32            4 bytes little-endian (protobuf float), see dashboard.encode_float_field
       1 fixed64            8 bytes little-endian (protobuf double)
   Getting a wire type wrong (e.g. double instead of float) makes the glasses drop the WHOLE
   message silently.

2. Framing (frame_pb, crc16_ccitt)
   A protobuf payload is sent inside one or more frames:
       AA 21 <seq> <len> <total> <num> <SID> <flag> <payload bytes...> [CRC16 LE]
       AA     start marker                   21   direction: to glasses (replies use 12)
       seq    per-frame sync byte ("magic" in the app's code); any 0-255 value, we count up
       len    bytes of payload in THIS packet, including CRC bytes if it carries them
       total/num  packet count and 1-based packet number (long payloads are split)
       SID    which feature (see g2.sids)    flag 0x20 = FLAG_REQUEST for app->glasses commands
       CRC16  CRC-16/CCITT over the whole payload, little-endian, at the end of the last packet
   Example (Even AI ENTER, seq 5): frame_pb(08 01 10 42 1a 02 08 02, 0x07, 0x20, 5) ->
       aa 21 05 0a 01 01 07 20 | 08 01 10 42 1a 02 08 02 | 6d 6c
   Do not confuse the frame's seq byte with the protobuf "magic" (field 2 of most payloads):
   that is a request id the glasses echo in their ack. g2.session.read_ack_magic reads it.

3. EvenHub (SID 0xE0) messages
   EvenHub is Even's plugin system: third-party apps draw their own pages (text, list and image
   "containers") on the glasses. Every EvenHub message is wrap_even_hub(cmd, magic, field, inner).
   The same channel carries the session heartbeat (build_heartbeat), which must run every few
   seconds or the glasses report "connection lost" -- and which must also be running for native
   dashboard (SID 0x01) pushes to be accepted. It also carries mic and IMU on/off
   (build_audio_control / build_imu_control) and click/IMU events from the glasses
   (decode_device_event). build_settings_query/parse_settings_* use SID 0x09 instead.

Status: framing, field encoders, prelude, heartbeat, text/list pages, text upgrade, settings
query, mic and IMU control are LIVE-CONFIRMED. Things marked EXPERIMENTAL/UNTESTED in their
docstrings (borders/padding, multi-text pages, cmd 7 rebuild, image containers) are not.
The cmd 7 rebuild, image-page and image-data layouts were corrected against the app's generated
proto on 2026-09-29 (docs/research/remaining_features.md section 2); still untested live.

History -- where the EvenHub builders came from. Python port of Faceclaw's real, working
BleProtocol.kt EvenHub-container message builders.

Confirmed 2026-09-25 against the real Faceclaw source (MessageBuilder.kt, FlashPromptFlow.kt):
the session prelude goes out on SID_APP_LAUNCH (0x01, fixed magic 156), but every EvenHub
container command that follows it -- create-page, text-upgrade, heartbeat, shutdown -- goes out
on SID_EVENHUB (0xe0), not SID_APP_LAUNCH. Earlier test scripts in this repo sent all of these on
SID_APP_LAUNCH, which is why the glasses never rendered anything: the firmware was never being
asked on the channel it listens for EvenHub commands on. All of these commands go to the RIGHT
lens only (the two lenses relay to each other internally); the left lens still needs to be
connected and sid-0x80-authenticated alongside it for bonding parity, just not sent commands."""

SID_APP_LAUNCH = 0x01      # prelude channel (same number as the native dashboard, sids.DASHBOARD)
SID_EVENHUB = 0xE0         # EvenHub pages, heartbeat, mic/IMU control, device events
SID_UI_SETTING = 0x09      # device settings (battery/firmware read-back)
FLAG_REQUEST = 0x20        # frame byte 7 for app -> glasses commands on the control channel
PRELUDE_ACK_MAGIC = 156    # fixed magic the prelude uses
DEFAULT_WIDGET_ID = 10000  # widgetId our cmd 0 pages have always used (LIVE)

# EvenHub_Cmd_List values we build or read, and the envelope field each one uses (CONFIRMED,
# fields_EvenHub.txt / enums.txt). Only the ones this module needs are named.
CMD_UPDATE_IMAGE, FIELD_UPDATE_IMAGE = 3, 5
CMD_IMAGE_ACK, FIELD_IMAGE_ACK = 4, 6
CMD_REBUILD_PAGE, FIELD_REBUILD_PAGE = 7, 7
CMD_REBUILD_ACK, FIELD_REBUILD_ACK = 8, 8
CMD_MENU_STARTUP, FIELD_MENU_STARTUP = 17, 20
CMD_MENU_ITEM_CLICK, FIELD_MENU_ITEM_CLICK = 21, 24
CMD_MENU_SUBITEM_SWITCH, FIELD_MENU_SUBITEM_SWITCH = 22, 25

# OsEventTypeList: the event_type in list/text/sys events (CONFIRMED enum)
EVENT_CLICK = 0
EVENT_SCROLL_TOP = 1
EVENT_SCROLL_BOTTOM = 2
EVENT_DOUBLE_CLICK = 3
EVENT_FOREGROUND_ENTER = 4
EVENT_FOREGROUND_EXIT = 5
EVENT_ABNORMAL_EXIT = 6
EVENT_SYSTEM_EXIT = 7
EVENT_IMU_DATA = 8
EVENT_LONG_PRESS = 9
EVENT_LONG_PRESS_RELEASE = 10
EVENT_NAMES = {0: 'CLICK', 1: 'SCROLL_TOP', 2: 'SCROLL_BOTTOM', 3: 'DOUBLE_CLICK',
               4: 'FOREGROUND_ENTER', 5: 'FOREGROUND_EXIT', 6: 'ABNORMAL_EXIT', 7: 'SYSTEM_EXIT',
               8: 'IMU_DATA_REPORT', 9: 'LONG_PRESS', 10: 'LONG_PRESS_RELEASE'}


def encode_varint(value):
    """Protobuf varint: 7 bits per byte, least-significant group first, high bit set on every
    byte except the last. encode_varint(1) -> 01, encode_varint(300) -> ac 02."""
    out = bytearray()
    v = value
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def encode_key(field_number, wire_type):
    """Field key = varint((field_number << 3) | wire_type). encode_key(2, 2) -> 12."""
    return encode_varint((field_number << 3) | wire_type)


def encode_varint_field(field_number, value):
    """Whole-number field (wire type 0). encode_varint_field(1, 2) -> 08 02. Also used for enums
    and bools (0/1). Note: proto3 senders usually omit zero values; we sometimes write them."""
    return encode_key(field_number, 0) + encode_varint(value)


def encode_string_field(field_number, value):
    """Text field (wire type 2): key, UTF-8 byte length, UTF-8 bytes.
    encode_string_field(2, "hi") -> 12 02 68 69."""
    b = value.encode("utf-8")
    return encode_key(field_number, 2) + encode_varint(len(b)) + b


def encode_bytes_field(field_number, value):
    """Raw-bytes field (wire type 2). Like encode_string_field but `value` is already bytes."""
    return encode_key(field_number, 2) + encode_varint(len(value)) + value


def encode_message_field(field_number, inner):
    """Nested-message field (wire type 2): key, length, then `inner` (an already-encoded message).
    This is how every "A contains B" in the schemas is built. Empty inner -> key + 00."""
    return encode_key(field_number, 2) + encode_varint(len(inner)) + inner


def crc16_ccitt(data):
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) of `data`, returned as 2 bytes little-endian,
    ready to append to a frame. Same algorithm as g2.transport.crc16_ccitt (which returns an int)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def frame_pb(pb, sid, flag, seq, max_write=240):
    """Wrap payload `pb` in one or more frames for service `sid`. Returns a list of bytes objects;
    write each one, in order, to transport.CHAR_WRITE on the right lens.

    Header: AA 21 seq len total num sid flag. The CRC16 of the whole payload is appended to the
    payload and the result is cut into chunks of up to 232 bytes (so for long payloads the CRC
    may be split across the last two packets). `flag` is FLAG_REQUEST (0x20) for commands;
    `seq` is the frame sync byte (use GlassesSession.next_seq()). The file service uses its own
    framing instead (g2.file_service), because its rules differ."""
    chunk_size = min(232, max_write - 8)
    crc = crc16_ccitt(pb)
    payload = pb + crc
    total_frags = max(1, -(-len(payload) // chunk_size))
    frames = []
    for i in range(total_frags):
        chunk = payload[i * chunk_size: (i + 1) * chunk_size]
        frame = bytes([0xAA, 0x21, seq & 0xFF, len(chunk) & 0xFF, total_frags & 0xFF, (i + 1) & 0xFF, sid & 0xFF, flag & 0xFF]) + chunk
        frames.append(frame)
    return frames


def wrap_even_hub(cmd, magic, inner_field_number, inner):
    """EvenHub (SID 0xE0) envelope: {1: cmd, 2: magic, <inner_field_number>: inner}.
    Returns protobuf bytes; frame them with frame_pb(..., SID_EVENHUB, FLAG_REQUEST, seq)."""
    return encode_varint_field(1, cmd) + encode_varint_field(2, magic) + encode_message_field(inner_field_number, inner)


def encode_text_object(name, container_id, x, y, width, height, text, capture_events,
                        border_width=0, border_color=0, border_radius=0, padding=0):
    """border_width/border_color/border_radius/padding are EXPERIMENTAL: the official SDK's
    TextContainerProperty docs list borderWidth/borderColor/borderRadius/paddingLength as real
    fields, but Faceclaw's own encodeTextObject (the confirmed reference for every other field
    here) never sets them -- fields 5-8 sit unused between height(4) and containerId(9) in their
    code, in exactly the order those four properties appear in the SDK's own example, which is
    the basis for this mapping. Not confirmed live yet."""
    parts = encode_varint_field(1, x) + encode_varint_field(2, y) + encode_varint_field(3, width) + encode_varint_field(4, height)
    if border_width:
        parts += encode_varint_field(5, border_width)
    if border_color:
        parts += encode_varint_field(6, border_color)
    if border_radius:
        parts += encode_varint_field(7, border_radius)
    if padding:
        parts += encode_varint_field(8, padding)
    parts += encode_varint_field(9, container_id) + encode_string_field(10, name)
    if capture_events:
        parts += encode_varint_field(11, 1)
    parts += encode_string_field(12, text)
    return parts


def build_create_input_page(magic, text="dashboard", border_width=0, border_color=0, border_radius=0, padding=0):
    """EvenHub cmd 0 (CreateStartUpPage): a full-screen (576x288) page with ONE text container named
    "dashboard" (id 1) showing `text`, with click events on. LIVE-CONFIRMED (the default page of
    GlassesSession.connect()). Only valid once per session -- see build_rebuild_page."""
    inner = encode_varint_field(1, 1)
    text_obj = encode_text_object("dashboard", 1, 0, 0, 576, 288, text, True,
                                   border_width=border_width, border_color=border_color,
                                   border_radius=border_radius, padding=padding)
    inner += encode_message_field(3, text_obj)
    inner += encode_varint_field(5, 10000)
    return wrap_even_hub(0, magic, 3, inner)


def build_text_upgrade(magic, container_id, container_name, text):
    """EvenHub cmd 5 (text upgrade): replace the text of an existing text container. Returns
    protobuf bytes for SID 0xE0. LIVE-CONFIRMED. GlassesSession.update_text_container() wraps it.

    General text-container update -- container_id AND container_name must both match whatever
    the page's text container was actually created with. Field1 was hardcoded to 1 in every
    version of this function until a live multi-container test proved that wrong: with 4 text
    containers, every update -- regardless of which name was sent in field2 -- landed on
    whichever container had id=1, with only the LAST update's content surviving there (each
    later update silently overwrote the previous one in that same slot). Every single-container
    page this session happened to use container id=1, which is exactly why the bug stayed
    hidden until now. Field1 is the real routing key; field2 (the name) may just be metadata."""
    inner = encode_varint_field(1, container_id) + encode_string_field(2, container_name)
    inner += encode_varint_field(3, 0) + encode_varint_field(4, len(text.encode("utf-8"))) + encode_string_field(5, text)
    return wrap_even_hub(5, magic, 9, inner)


def build_dashboard_text_upgrade(magic, text):
    """Shortcut: build_text_upgrade for the default page from build_create_input_page (id 1, "dashboard")."""
    return build_text_upgrade(magic, 1, "dashboard", text)


def build_settings_query(magic):
    """Real Faceclaw buildSettingsQuery: field1=2 (cmd), field2=magic, field4=message{field1=1}
    (a bare read request). Goes on SID_UI_SETTING (0x09), not SID_EVENHUB -- a different
    channel from every display/mic/IMU command above. The ack carries battery/charging/firmware
    -- see parse_settings_battery/parse_settings_firmware below."""
    request = encode_varint_field(1, 1)
    return encode_varint_field(1, 2) + encode_varint_field(2, magic) + encode_message_field(4, request)


def parse_settings_battery(pb):
    """Real Faceclaw parseSettingsBattery: ack's field4 (deviceReceiveRequestFromApp) carries
    field12=battery(0-100), field13=charging(0/1), field14=silentMode(0/1). Returns None if the
    ack doesn't carry a battery field (e.g. a push notification, not a read ack)."""
    request = read_bytes_field(pb, 4)
    if request is None:
        return None
    battery = read_varint_field(request, 12, -1)
    if battery < 0:
        return None
    return {
        "battery": battery,
        "charging": read_varint_field(request, 13, -1),
        "silent_mode": read_varint_field(request, 14, -1),
    }


def parse_settings_firmware(pb):
    """Real Faceclaw parseSettingsFirmwareInfo: field4's field5/field6 are the left/right lens
    firmware version strings. Returns None if neither is present."""
    request = read_bytes_field(pb, 4)
    if request is None:
        return None
    left = read_string_field(request, 5)
    right = read_string_field(request, 6)
    if not left and not right:
        return None
    return {"left_version": left, "right_version": right}


def encode_list_item_container(items):
    """List items sub-message: {1: count, 3: 1, 4: item text (repeated)}. Used by encode_list_object."""
    parts = encode_varint_field(1, len(items)) + encode_varint_field(3, 1)
    for item in items:
        parts += encode_string_field(4, item)
    return parts


def encode_list_object(name, container_id, x, y, width, height, items, capture_events,
                        border_width=0, border_color=0, border_radius=0, padding=0):
    """Fields 5-8 (border_width/border_color/border_radius/padding) confirmed against the real
    SDK docs (nickustinov/even-g2-notes, docs/display.md's ListContainerProperty example) --
    same field layout as encode_text_object's fields 1-4/5-8, just with list-specific fields
    (item container, capture_events) after container_id/name instead of a plain text string."""
    parts = encode_varint_field(1, x) + encode_varint_field(2, y) + encode_varint_field(3, width) + encode_varint_field(4, height)
    if border_width:
        parts += encode_varint_field(5, border_width)
    if border_color:
        parts += encode_varint_field(6, border_color)
    if border_radius:
        parts += encode_varint_field(7, border_radius)
    if padding:
        parts += encode_varint_field(8, padding)
    parts += encode_varint_field(9, container_id) + encode_string_field(10, name)
    parts += encode_message_field(11, encode_list_item_container(items))
    if capture_events:
        parts += encode_varint_field(12, 1)
    return parts


def build_create_prompt_page(magic, text_name, text_container_id, warning_text,
                              list_name, list_container_id, items):
    """Real Faceclaw buildCreatePromptPage: cmd=0 CreateStartUpPage with a text container (the
    prompt) plus a list container (the selectable rows) in one page.

    Geometry reverted to stock-firmware-safe (2026-09-26): list 280x120 at (0,150), text 280x130
    at (0,0) -- the ONLY geometry actually confirmed live via recreate_page() (page_swap_test.py).
    A full-canvas (576x288) version was tried for the "widgets on the right" redesign, and the
    on-glasses menu then failed to render at all on a live double-tap -- acked=False either way
    (recreate-page never acks, confirmed harmless elsewhere), but this time nothing visually
    changed either, unlike every prior successful swap. Full-canvas geometry combined with
    recreate_page (as opposed to a fresh connect(), which list_text_test.py proved works at full
    canvas) had never actually been live-tested -- reverting until proven otherwise.

    NO border on the list either -- tried once (2026-09-26) and also failed to render; unconfirmed
    and untested independent of the geometry change above."""
    inner = encode_varint_field(1, 2)
    inner += encode_message_field(2, encode_list_object(list_name, list_container_id, 0, 150, 280, 120, items, True))
    inner += encode_message_field(3, encode_text_object(text_name, text_container_id, 0, 0, 280, 130, warning_text, False))
    inner += encode_varint_field(5, 10000)
    return wrap_even_hub(0, magic, 3, inner)


def build_create_dashboard_page(magic, list_object_bytes, text_objects_bytes):
    """EXPERIMENTAL, extending the confirmed field2=list/field3=text pattern to multiple text
    containers -- never tried before this repo (every prior test used exactly one of each).
    Grounded in real protobuf semantics (repeated fields share a tag, in order) and the SDK
    docs' stated caps (textObject max 8, containerTotalNum 1-12), but the *specific* multi-text
    case hasn't been confirmed live before. list_object_bytes: one encode_list_object() result,
    or None for a text-only multi-container page. text_objects_bytes: list of
    encode_text_object() results, one per container -- each needs its own unique containerName
    to be addressable later via build_text_upgrade()."""
    total = len(text_objects_bytes) + (1 if list_object_bytes else 0)
    inner = encode_varint_field(1, total)
    if list_object_bytes:
        inner += encode_message_field(2, list_object_bytes)
    for text_bytes in text_objects_bytes:
        inner += encode_message_field(3, text_bytes)
    inner += encode_varint_field(5, 10000)
    return wrap_even_hub(0, magic, 3, inner)


def build_rebuild_page(magic, list_object_bytes, text_objects_bytes, image_objects_bytes=None):
    """EvenHub cmd 7 (REBUILD_PAGE): replace the current page's containers without the one-shot
    cmd 0. UNTESTED LIVE; layout CONFIRMED from the app's generated proto (fields_EvenHub.txt).

    RebuildPageContainer (envelope field 7, NOT 3):
        1 ContainerTotalNum  2 ListObject[]  3 TextObject[]  4 ImageObject[]  5 MenuObject (message)
    It has no widgetId and no sessionId. The previous version of this function (a) wrapped the
    body in envelope field 3 (CreateMessage) and (b) appended varint 5: 10000 -- a wire-type
    mismatch, since field 5 here is the MenuObject message. Both fixed 2026-09-29
    (docs/research/remaining_features.md section 2.3). Ack: cmd 8, envelope field 8
    {1 ResCmdMsg}: 6 = REBUILD_PAGE_SUCCESS, 7 = failed.

    list_object_bytes: one encode_list_object() result or None; text_objects_bytes: list of
    encode_text_object() results; image_objects_bytes: list of encode_image_object() results.
    Called through GlassesSession.rebuild_page()."""
    images = image_objects_bytes or []
    total = len(text_objects_bytes) + len(images) + (1 if list_object_bytes else 0)
    inner = encode_varint_field(1, total)
    if list_object_bytes:
        inner += encode_message_field(2, list_object_bytes)
    for text_bytes in text_objects_bytes:
        inner += encode_message_field(3, text_bytes)
    for image_bytes in images:
        inner += encode_message_field(4, image_bytes)
    return wrap_even_hub(CMD_REBUILD_PAGE, magic, FIELD_REBUILD_PAGE, inner)


# ---- image containers -- UNTESTED LIVE. Field layout CONFIRMED from the app's generated proto
# (fields_EvenHub.txt): CreateStartUpPageContainer.4 = ImageObject[], ImageContainerProperty =
# {1 x, 2 y, 3 w, 4 h, 5 ContainerID, 6 ContainerName, 7 zOrderIndex}. Still unknown: the
# CompressMode value the firmware expects (the SDK says LZ4) and the exact 4-bit grey packing.

def encode_image_object(name, container_id, x, y, width, height, z_order=None):
    """ImageContainerProperty {1 x, 2 y, 3 w, 4 h, 5 ContainerID, 6 ContainerName, 7 zOrderIndex}.
    CONFIRMED layout (schema); zOrderIndex is written only when given."""
    parts = encode_varint_field(1, x) + encode_varint_field(2, y) + encode_varint_field(3, width) + encode_varint_field(4, height)
    parts += encode_varint_field(5, container_id) + encode_string_field(6, name)
    if z_order is not None:
        parts += encode_varint_field(7, z_order)
    return parts


def build_create_image_page(magic, name, container_id, x, y, width, height,
                            widget_id=DEFAULT_WIDGET_ID, session_id=None):
    """EvenHub cmd 0 page with a single image container. UNTESTED LIVE.
    CreateStartUpPageContainer: 1 total, 4 ImageObject (was wrongly field 6 = MenuObject until
    2026-09-29), 5 widgetId, 7 sessionId (optional; INFERRED to be a fresh per-run number)."""
    inner = encode_varint_field(1, 1)
    inner += encode_message_field(4, encode_image_object(name, container_id, x, y, width, height))
    inner += encode_varint_field(5, widget_id)
    if session_id is not None:
        inner += encode_varint_field(7, session_id)
    return wrap_even_hub(0, magic, 3, inner)


def pack_4bpp(pixels, width, height):
    """Pack a width*height list of 0-15 grayscale values into 4bpp bytes, 2 pixels/byte, high
    nibble = left pixel -- matches the hardware's 16-gray-level spec and Faceclaw's own packing
    (BleImageOptimizer.kt), the closest real precedent found even though it's for a different
    (CFW) wire format. Pads an odd width with a zero nibble per row."""
    stride = (width + 1) // 2
    out = bytearray(stride * height)
    for row in range(height):
        for col in range(width):
            v = pixels[row * width + col] & 0x0F
            byte_index = row * stride + col // 2
            if col % 2 == 0:
                out[byte_index] |= v << 4
            else:
                out[byte_index] |= v
    return bytes(out)


def build_image_raw_data(magic, container_id, name, total_size, fragment_index, fragment_data,
                         map_session_id=1, compress_mode=0):
    """EvenHub cmd 3 (UPDATE_IMAGE_RAW_DATA), envelope field 5. UNTESTED LIVE.
    ImageRawDataUpdate: 1 ContainerID, 2 ContainerName, 3 MapSessionId, 4 MapTotalSize,
    5 CompressMode, 6 MapFragmentIndex, 7 MapFragmentPacketSize, 8 MapRawData.
    Field 3 is a per-IMAGE transfer id, separate from the container id (the old code wrote
    container_id there): INFERRED to be a counter, fresh for each image (1, 2, ...) and the same
    for every fragment of that image. total_size = whole (compressed) image; field 7 = this
    fragment's length; fragment_index starts at 0. compress_mode's LZ4 value is unknown (0 is
    assumed to mean raw). Wait for the cmd 4 ack (envelope field 6, ErrorCode 4 = OK) before the
    next fragment."""
    inner = encode_varint_field(1, container_id) + encode_string_field(2, name)
    inner += encode_varint_field(3, map_session_id) + encode_varint_field(4, total_size)
    inner += encode_varint_field(5, compress_mode) + encode_varint_field(6, fragment_index)
    inner += encode_varint_field(7, len(fragment_data)) + encode_bytes_field(8, fragment_data)
    return wrap_even_hub(CMD_UPDATE_IMAGE, magic, FIELD_UPDATE_IMAGE, inner)


def build_prelude_f5872_payload():
    """Session prelude protobuf, copied from Faceclaw: {1: 2, 2: 156, 4: {3: {2: {2: {1: 0, 2: 0}}}}}.
    Sent once on SID 0x01 right after auth; GlassesSession.connect() sends the framed version
    PRELUDE_F5872. LIVE-CONFIRMED as part of the working connect sequence."""
    field4 = encode_message_field(3, encode_message_field(2, encode_message_field(2,
        encode_varint_field(1, 0) + encode_varint_field(2, 0))))
    return encode_varint_field(1, 2) + encode_varint_field(2, PRELUDE_ACK_MAGIC) + encode_message_field(4, field4)


# The prelude payload and its ready-made frame (SID 0x01, flag 0x20, fixed seq 0x92). Built once at import.
PRELUDE_F5872_PAYLOAD = build_prelude_f5872_payload()
PRELUDE_F5872 = frame_pb(PRELUDE_F5872_PAYLOAD, SID_APP_LAUNCH, FLAG_REQUEST, 0x92)[0]

# `python -m g2.evenhub` prints the prelude bytes (a quick sanity check; not a test).
if __name__ == "__main__":
    print("PRELUDE_F5872_PAYLOAD:", PRELUDE_F5872_PAYLOAD.hex())
    print("PRELUDE_F5872 frame:  ", PRELUDE_F5872.hex())


def build_heartbeat(magic):
    """EvenHub session heartbeat: wrapEvenHub(12, magic, 14, {field1=0}) -- the real Faceclaw
    heartbeat. LIVE-CONFIRMED. Must go out on SID 0xE0 every ~4 s (GlassesSession does this in
    the background); without it the glasses drop the EvenHub session, and native dashboard
    (SID 0x01) pushes are ignored. Fire-and-forget: do not wait for its echo."""
    return wrap_even_hub(12, magic, 14, encode_varint_field(1, 0))


def build_audio_control(magic, enable):
    """Real Faceclaw mic control: wrapEvenHub(CMD_AUDIO_CONTROL=15, magic, 18, {field1=enable}).
    Mic audio itself doesn't arrive on this channel -- it arrives as raw LC3 packets on a
    separate notify characteristic, RENDER_NOTIFY_UUID (...e6402), enabled independently.
    See tools/hw_tests/dev/mic_capture_test.py and GlassesSession.enable_mic()."""
    return wrap_even_hub(15, magic, 18, encode_varint_field(1, 1 if enable else 0))


def build_imu_control(magic, enable, report_freq=500):
    """Real Faceclaw IMU control: wrapEvenHub(CMD_OPEN_IMU=19, magic, 22, {field1=enable,
    field2=reportFreq}). reportFreq is a pacing code (100..1000, step 100, per the SDK docs'
    ImuReportPace -- not literal Hz), only sent when enabling. Streamed IMU reports arrive as
    async device events on the same notify channel -- see decode_device_event()."""
    inner = encode_varint_field(1, 1 if enable else 0)
    if enable and report_freq > 0:
        inner += encode_varint_field(2, report_freq)
    return wrap_even_hub(19, magic, 22, inner)


# ---- generic protobuf field readers + async-event decoder ----
# Mirrors BleProtocol.kt's readVarintFieldValue/readFieldBytes/readStringFieldValue/
# readFloatFieldValue and G2Event.decodePayload's exact field layout for list/text/sys events
# (list clicks, text clicks, and system events including IMU reports) on SID_EVENHUB notifications.

def _iter_fields(pb):
    """Yield (field_number, wire_type, value) for each top-level field of protobuf bytes `pb`.
    value is an int for varints and raw bytes otherwise. Stops at an unknown wire type.
    Nested messages are NOT expanded -- call again on the returned bytes."""
    i = 0
    while i < len(pb):
        key, i = decode_varint_at(pb, i)
        field, wire = key >> 3, key & 0x7
        if wire == 0:
            value, i = decode_varint_at(pb, i)
            yield field, wire, value
        elif wire == 1:
            yield field, wire, pb[i:i + 8]
            i += 8
        elif wire == 2:
            length, i = decode_varint_at(pb, i)
            yield field, wire, pb[i:i + length]
            i += length
        elif wire == 5:
            yield field, wire, pb[i:i + 4]
            i += 4
        else:
            return


def decode_varint_at(data, i):
    """Read one varint from `data` starting at index i. Returns (value, index_after_it)."""
    result = 0
    shift = 0
    while True:
        b = data[i]
        result |= (b & 0x7F) << shift
        i += 1
        if not (b & 0x80):
            return result, i
        shift += 7


def read_varint_field(pb, field_number, default=-1):
    """Value of the first varint field `field_number` in `pb`, or `default` if absent.
    Example: read_varint_field(bytes.fromhex("0801 1042"), 2) -> 0x42."""
    for f, wire, v in _iter_fields(pb):
        if f == field_number and wire == 0:
            return v
    return default


def read_string_field(pb, field_number, default=""):
    """First length-delimited field `field_number` decoded as UTF-8 text, or `default`."""
    for f, wire, v in _iter_fields(pb):
        if f == field_number and wire == 2:
            return v.decode("utf-8", errors="replace")
    return default


def read_bytes_field(pb, field_number):
    """First length-delimited field `field_number` as raw bytes (e.g. a nested message), or None."""
    for f, wire, v in _iter_fields(pb):
        if f == field_number and wire == 2:
            return v
    return None


def read_float_field(pb, field_number, default=0.0):
    """First fixed32 (protobuf float) field `field_number` as a Python float, or `default`."""
    import struct
    for f, wire, v in _iter_fields(pb):
        if f == field_number and wire == 5:
            return struct.unpack("<f", v)[0]
    return default


def _event_fields(event_type):
    """Common keys for an OsEventTypeList value."""
    return {"event_type": event_type,
            "event_name": EVENT_NAMES.get(event_type, f"event={event_type}"),
            "long_press": event_type in (EVENT_LONG_PRESS, EVENT_LONG_PRESS_RELEASE)}


def decode_device_event(pb):
    """Decode a SID_EVENHUB async notification into a dict, or None if it is not an event (e.g.
    an ACK, or a heartbeat echo). Layout CONFIRMED from the app's generated proto:

      cmd 2, field 13 SendDeviceEvent, one of:
        1 List_ItemEvent{1 ContainerID, 2 name, 3 item name, 4 item index, 5 EventType}
             -> type "list-click"   (container_id, container_name, item_name, item_index)
        2 Text_ItemEvent{1 ContainerID, 2 name, 3 EventType}
             -> type "text-click"   (container_id, container_name)
        3 Sys_ItemEvent{1 EventType, 2 EventSource, 3 IMU{x,y,z float}, 4 exitReason,
                        5 widgetId, 6 sessionId}
             -> type "sys-event"    (event_source, exit_reason, widget_id, session_id, imu)
      cmd 17, field 20 MenuStartUpEvent{1 widgetId}     -> type "menu-startup"   (widget_id)
      cmd 21, field 24 MenuItemClickEvent{1 ItemID}     -> type "menu-item-click" (item_id)
      cmd 22, field 25 {1 ParentItemID, 2 SubItemID, 3 SubItemIndex} -> type "menu-subitem-switch"

    Every list/text/sys event also carries event_type (OsEventTypeList: 0 CLICK, 3 DOUBLE_CLICK,
    9 LONG_PRESS, 10 LONG_PRESS_RELEASE, ...), event_name and long_press (True for 9/10). The
    "type" key stays "text-click"/"list-click" for every event_type, as before, so check
    event_type to tell a click from a long press. Absent numeric fields decode as 0 (proto3 /
    nanopb leave zeros out; INFERRED for this firmware), so item_index 0 is a real first-row
    selection (it used to decode as -1)."""
    device_event = read_bytes_field(pb, 13)
    if device_event is None:
        startup = read_bytes_field(pb, FIELD_MENU_STARTUP)
        if startup is not None:
            return {"type": "menu-startup", "widget_id": read_varint_field(startup, 1, 0)}
        click = read_bytes_field(pb, FIELD_MENU_ITEM_CLICK)
        if click is not None:
            return {"type": "menu-item-click", "item_id": read_varint_field(click, 1, 0)}
        switch = read_bytes_field(pb, FIELD_MENU_SUBITEM_SWITCH)
        if switch is not None:
            return {"type": "menu-subitem-switch", "parent_item_id": read_varint_field(switch, 1, 0),
                    "sub_item_id": read_varint_field(switch, 2, 0),
                    "sub_item_index": read_varint_field(switch, 3, 0)}
        return None
    list_event = read_bytes_field(device_event, 1)
    if list_event is not None:
        result = {
            "type": "list-click",
            "container_id": read_varint_field(list_event, 1, 0),
            "container_name": read_string_field(list_event, 2),
            "item_name": read_string_field(list_event, 3),
            "item_index": read_varint_field(list_event, 4, 0),
        }
        result.update(_event_fields(read_varint_field(list_event, 5, 0)))
        return result
    text_event = read_bytes_field(device_event, 2)
    if text_event is not None:
        result = {
            "type": "text-click",
            "container_id": read_varint_field(text_event, 1, 0),
            "container_name": read_string_field(text_event, 2),
        }
        result.update(_event_fields(read_varint_field(text_event, 3, 0)))
        return result
    sys_event = read_bytes_field(device_event, 3)
    if sys_event is not None:
        result = {"type": "sys-event"}
        result.update(_event_fields(read_varint_field(sys_event, 1, 0)))
        result.update({
            "event_source": read_varint_field(sys_event, 2, 0),
            "exit_reason": read_varint_field(sys_event, 4, 0),
            "widget_id": read_varint_field(sys_event, 5, 0),
            "session_id": read_varint_field(sys_event, 6, 0),
        })
        imu = read_bytes_field(sys_event, 3)
        if imu is not None:
            result["imu"] = {
                "x": read_float_field(imu, 1),
                "y": read_float_field(imu, 2),
                "z": read_float_field(imu, 3),
            }
        return result
    return None


def decode_page_ack(pb):
    """Result code of a page-level ack, or None if `pb` is not one: cmd 8 rebuild ack (envelope
    field 8, 6 = OK, 7 = failed) or cmd 4 image ack (field 6; ErrorCode in its field 8, 4 = OK,
    5 = failed). Returns {"cmd", "result", "ok"}; zero-omission means an absent code reads 0."""
    cmd = read_varint_field(pb, 1, 0)
    if cmd == CMD_REBUILD_ACK:
        sub = read_bytes_field(pb, FIELD_REBUILD_ACK)
        if sub is not None:
            code = read_varint_field(sub, 1, 0)
            return {"cmd": cmd, "result": code, "ok": code == 6}
    if cmd == CMD_IMAGE_ACK:
        sub = read_bytes_field(pb, FIELD_IMAGE_ACK)
        if sub is not None:
            code = read_varint_field(sub, 8, 0)
            return {"cmd": cmd, "result": code, "ok": code == 4,
                    "map_session_id": read_varint_field(sub, 3, 0),
                    "fragment_index": read_varint_field(sub, 6, 0)}
    return None
