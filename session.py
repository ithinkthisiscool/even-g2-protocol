"""GlassesSession: one live Bluetooth connection to a pair of G2 glasses.

Plain English
-------------
The glasses are two BLE devices (one per lens). The right lens is the one we talk to; the left
lens only has to be connected and authenticated alongside it (the lenses relay to each other).
GlassesSession does everything needed to get from "glasses on the desk" to "ready for commands":

    1. connect()      find both lenses (BlueZ fast path by address, else a scan that stops as
                      soon as both are seen -- g2/ble.py), connect both with retry/backoff,
                      subscribe to replies, authenticate both (SID 0x80),
                      send the prelude, optionally create an EvenHub page, and start the
                      background heartbeat (SID 0xE0 every 4 s)
    2. send + wait    _send_and_await_ack / _send_and_get_payload frame a protobuf payload,
                      write it to the right lens and wait for the reply that echoes its magic
    3. close()        stop the heartbeat and disconnect

What arrives from the glasses, and where it goes
------------------------------------------------
    notes            asyncio.Queue of every raw frame from the control channel (0x5402).
                     OWNED by the ack-waiting helpers (_send_and_await_ack & co.). Do NOT read
                     it from a second task: two readers race and steal each other's acks.
    on_raw_frame     optional callback(svc_hi, svc_lo, pb) called for EVERY parsed frame as it
                     arrives (svc_hi is the SID, svc_lo the flag byte). This is the safe way to
                     watch incoming messages (Even AI wake, quicklist ticks, news refresh...).
                     It runs inside the Bluetooth callback: do not block or await in it --
                     schedule work with asyncio.get_running_loop().create_task(...).
    on_device_event  optional callback(dict) for decoded EvenHub events (clicks, IMU).
    audio_packets    asyncio.Queue of (arm, packet) mic packets, arm 'R' or 'L', after
                     start_audio_stream() or enable_mic(). Each packet: 200 bytes LC3 audio +
                     5 trailer bytes (see enable_mic).
    file_notes       asyncio.Queue of (sid, flag, payload) file-service replies (SID 0xC4/0xC5).

Magic vs seq: next_magic() gives the request id you put in protobuf field 2 (the glasses echo it
in the ack); next_seq() gives the frame header byte 2. Use a fresh one of each per message.

Status: connect/auth/prelude/heartbeat, page create, text update, settings query, mic, IMU and
Even AI audio streaming are LIVE-CONFIRMED. recreate_page() is unreliable; rebuild_page() and the
file channel (enable_file_channel / send_file_frames) are untested.

History: factored out of the connect/auth/prelude/create-page/heartbeat sequence in
python/archive/legacy/hold_both_evenhub4.py so several scripts could share one proven
connection instead of copy-pasting it."""
import asyncio
import contextlib
import os

from bleak import BleakClient, BleakScanner  # noqa: F401  (kept importable for callers)

from . import ble

from . import transport as tp
from . import evenhub as eh

# Default lens addresses. No pair is built in: pass right_mac/left_mac to GlassesSession, or set
# G2_RIGHT_MAC / G2_LEFT_MAC, or leave them unset and connect() picks the first complete pair that
# discover_pairs() finds (names look like "Even G2_<pair>_R_<last 3 address bytes>").
RIGHT_MAC = os.environ.get('G2_RIGHT_MAC') or None
LEFT_MAC = os.environ.get('G2_LEFT_MAC') or None
# Notify characteristic 0x6402: raw LC3 microphone packets (no frame header, no CRC).
RENDER_NOTIFY_UUID = tp.UUID_BASE.format(0x6402)


def decode_varint(data, i):
    """Read one varint at index i. Returns (value, next_index). Same as evenhub.decode_varint_at."""
    result = 0
    shift = 0
    while True:
        b = data[i]
        result |= (b & 0x7F) << shift
        i += 1
        if not (b & 0x80):
            return result, i
        shift += 7


def read_ack_magic(pb):
    """Return the magic (field 2) of a protobuf that starts with field 1 (varint) then field 2
    (varint) -- the shape of every ack -- or None if `pb` does not start that way. A command id
    of 0 is left out by proto3, so a reply may also start directly with field 2."""
    try:
        if len(pb) >= 2 and pb[0] == 0x10:
            magic, _ = decode_varint(pb, 1)
            return magic
        if len(pb) < 2 or pb[0] != 0x08:
            return None
        _, i = decode_varint(pb, 1)
        if i >= len(pb) or pb[i] != 0x10:
            return None
        magic, _ = decode_varint(pb, i + 1)
        return magic
    except IndexError:          # truncated varint: not an ack (never let it break on_notify)
        return None


_NAME_RE = __import__('re').compile(r'G2_(\w+?)_([LR])_([0-9A-Fa-f]{6})')


async def discover_pairs(timeout=10.0):
    """Find Even G2 pairs: a callback scan of `timeout` seconds merged with the lenses BlueZ
    already knows (so the pair connected right now -- which does not advertise -- still shows).
    Each lens advertises a name like "Even G2_32_L_ABCDEF": pair id (32), side (L/R), last 3
    bytes of its address. Returns a list of dicts {pair_id, right_mac, left_mac, right_name,
    left_name} plus, per side, *_rssi / *_seen / *_known / *_bonded / *_trusted / *_connected,
    and serial / complete (see g2.ble.group_pairs). A side never seen or known is None.
    For a live, streaming variant use g2.ble.stream_pairs()."""
    return await ble.discover_pairs(timeout=timeout)


def parse_frame(frame):
    """Split one frame into (svc_hi, svc_lo, payload): svc_hi = SID (byte 6), svc_lo = flag
    (byte 7), payload = the bytes after the header minus the 2 CRC bytes (len byte 3 - 2).
    Returns None for anything that is not an AA frame. The CRC is not checked. Multi-packet
    messages are reassembled before this sees them (GlassesSession feeds every notification
    through g2.transport.FrameAssembler, whose output this splits the same way)."""
    if len(frame) < 10 or frame[0] != 0xAA:
        return None
    svc_hi = frame[6]; svc_lo = frame[7]
    a, b = tp.payload_span(frame)
    return svc_hi, svc_lo, frame[a:b]


class GlassesSession:
    """One connection to both lenses. Typical use:

        s = GlassesSession()
        await s.connect(launch_app=True, skip_page=True)   # keep the native dashboard visible
        ...                                                  # send things
        await s.close()

    connect() brings up both lenses, authenticates, sends the prelude, creates the display
    page and starts a background 4s heartbeat (the glasses report "connection lost" after ~4s of
    silence on sid=0xe0). update_text() pushes new text to the already-open page."""

    def __init__(self, initial_text='Jarvis ready.', page_payload_builder=None, text_container_name='dashboard',
                 container_ids=None, right_mac=None, left_mac=None):
        """Nothing is sent until connect(). initial_text is the text of the default EvenHub
        page (only used when connect() creates a page).

        page_payload_builder(magic) -> bytes, if given, replaces the default text-only
        create-page payload -- e.g. eh.build_create_prompt_page for a list+text layout. Only
        applies to the initial page create() sends; there's no confirmed "rebuild" command in
        this repo's traced protocol, so a session's page layout is fixed for its lifetime.
        text_container_name must match whatever name page_payload_builder actually gave the text
        container -- update_text() targets this name, and build_create_input_page's default
        page uses "dashboard" (the default here), but build_create_prompt_page callers pick
        their own name and must pass it here too or update_text() will silently target nothing.
        container_ids: {name: id} for every text container on the page, if there's more than
        one -- update_text_container()'s writes need the real id (confirmed live: it's the true
        routing key, not the name -- see build_text_upgrade's docstring), not just a default of
        1, once a page has more than one text container. Omit for single-container pages (id 1
        is used, matching every builder's default)."""
        # Which glasses to connect to. Defaults to G2_RIGHT_MAC / G2_LEFT_MAC; when those are unset
        # too, connect() discovers a pair. The hub passes the pair from its device database.
        self.right_mac = right_mac or RIGHT_MAC
        self.left_mac = left_mac or LEFT_MAC
        self.right_client = None
        self.left_client = None
        self.notes = asyncio.Queue(maxsize=2000)
        # Ack router: (sid, magic) → Future resolved with the reply's protobuf the moment it
        # arrives, so concurrent requests never steal each other's acks from self.notes.
        self._pending = {}
        self._assembler = tp.FrameAssembler()   # joins multi-packet replies (see _on_control_notify)
        # One lock per lens: a multi-packet message is written without other packets in between.
        self._write_lock_right = asyncio.Lock()
        self._write_lock_left = asyncio.Lock()
        self.audio_packets = asyncio.Queue()
        self._magic = 100
        self._seq = 10
        self._hb_task = None
        self._initial_text = initial_text
        self._page_payload_builder = page_payload_builder
        self._text_container_name = text_container_name
        self._container_ids = container_ids or {}
        self._mic_enabled = False
        # Optional callback(event_dict) for every decoded async device event (gestures, IMU,
        # list/text clicks) -- see on_notify() in connect(). None (the default) means no fan-out.
        self.on_device_event = None
        # Optional callback(svc_hi, svc_lo, pb) for EVERY parsed frame on arrival -- read-only
        # tap for passive logging; never consumes from self.notes.
        self.on_raw_frame = None
        # File service (EFS) replies [cid, rsp] from SID 0xC4/0xC5, whichever characteristic they
        # arrive on (0x7402 file notify, or 0x5402).
        self.file_notes = asyncio.Queue()
        self._file_notify_on = False

    def next_magic(self):
        """Next request id (starts at 101) for protobuf field 2 ("magic"); the ack echoes it."""
        self._magic += 1
        return self._magic

    def next_seq(self):
        """Next frame sync byte (0-255, wraps) for frame header byte 2."""
        self._seq = (self._seq + 1) % 256
        return self._seq

    # ── Connection settings (class-level so the hub/tests can tune them without new args) ──
    connect_attempts = 3        # connect_with_retry attempts inside one connect() call
    connect_timeout = 12.0      # per-lens BLE connect timeout (s)
    scan_timeout = 8.0          # per-attempt scan window when the BlueZ fast path is not used
    concurrent_connect = True   # 1st attempt connects both lenses at once; retries go R then L
    clear_stale = 'on_failure'  # see g2.ble.connect_with_retry
    # Offline-test hooks (None = real bleak / BlueZ). See tests/test_g2_ble.py.
    _scanner_factory = None
    _client_factory = None
    _bluez = None
    # Optional callback(side) called the moment a lens link drops (bleak disconnected_callback).
    on_disconnect = None

    async def scan_both(self, attempts=4, timeout=10.0):
        """Scan for self.right_mac and self.left_mac. Returns (right_device, left_device); raises
        RuntimeError if either is still missing after `attempts` scans of up to `timeout` s.

        Each scan is a callback scan that stops the moment BOTH lenses have been seen (usually
        well under a second when both advertise). Both lenses stop advertising once anything
        else (even a stray bluetoothctl connection) holds them -- connect() therefore prefers
        the BlueZ fast path and only scans as a fallback."""
        right = left = None
        for attempt in range(1, attempts + 1):
            print(f'Scanning (attempt {attempt}/{attempts})...', flush=True)
            found = await ble.find_lenses([self.right_mac, self.left_mac], timeout,
                                          self._scanner_factory)
            right = found.get(ble.norm(self.right_mac))
            left = found.get(ble.norm(self.left_mac))
            if right and left:
                return right, left
            print(f"  missing: right={'ok' if right else 'MISSING'} left={'ok' if left else 'MISSING'}", flush=True)
        raise RuntimeError(f"missing lens(es) after {attempts} scans: right={'ok' if right else 'MISSING'} left={'ok' if left else 'MISSING'}")

    async def resolve_pair(self, timeout=8.0):
        """Fill in self.right_mac / self.left_mac from discovery when they were not given: the
        first complete pair (bonded / strongest first, see g2.ble.discover_pairs). Raises
        RuntimeError when no complete pair is found."""
        pairs = await ble.discover_pairs(
            timeout=timeout, scanner_factory=self._scanner_factory, bluez=self._bluez,
            stop_when=lambda ps: any(p.get('complete') for p in ps.values()))
        for p in pairs:
            if p.get('complete'):
                self.right_mac, self.left_mac = p['right_mac'], p['left_mac']
                print(f"Using discovered pair {p['pair_id']}: R {self.right_mac} / L {self.left_mac}", flush=True)
                return p
        raise RuntimeError('no complete G2 pair found: set G2_RIGHT_MAC / G2_LEFT_MAC or pass right_mac/left_mac')

    def _on_link_lost(self, side, _client=None):
        """bleak disconnected_callback for either lens: record it and wake wait_disconnected()."""
        if getattr(self, '_closing', False):
            return
        self.disconnected_side = side
        print(f'Link lost: {side} lens disconnected', flush=True)
        ev = getattr(self, '_link_lost', None)
        if ev is not None:
            ev.set()
        if self.on_disconnect is not None:
            try:
                self.on_disconnect(side)
            except Exception as e:
                print(f'  [on_disconnect error: {e}]', flush=True)

    @property
    def is_connected(self):
        """True while both lens links are up."""
        return bool(self.right_client and self.left_client and self.right_client.is_connected
                    and self.left_client.is_connected)

    async def wait_disconnected(self):
        """Return (the side name) as soon as either lens link drops -- no polling. Returns at
        once if not connected."""
        ev = getattr(self, '_link_lost', None)
        if ev is None or not self.is_connected:
            return getattr(self, 'disconnected_side', None)
        await ev.wait()
        return self.disconnected_side

    async def connect(self, launch_app=True, skip_page=False):
        """Scan, connect both lenses, subscribe to notifications, and authenticate both. Then,
        depending on the flags:

            launch_app=True,  skip_page=False  prelude + create EvenHub page + heartbeat (default;
                                               our own page covers the screen)
            launch_app=True,  skip_page=True   prelude + heartbeat, no page (what the hub uses:
                                               native dashboard stays visible and SID 0x01
                                               pushes are accepted)
            launch_app=False                   stop after auth (no prelude, no heartbeat)

        Raises on scan/connect failure; auth failure is only printed.

        launch_app=False stops right after auth -- no prelude, no CreateStartUpPage, no
        heartbeat. Skipping the EvenHub app launch means the glasses never leave their native
        dashboard, which is otherwise impossible to observe from this side (our own app session
        always immediately takes over the screen). Added for dashboard_sniff2.py -- passively
        watching what the native dashboard does on its own, if anything, without a phone
        present. Every other script keeps launch_app=True (the original, only) behavior.

        skip_page=True skips the CreateStartUpPage step (create-page) but still sends the
        prelude and starts the heartbeat. This keeps SID=0x01 (native dashboard protocol)
        active without drawing a custom EvenHub display page over the native dashboard. Use
        this for native dashboard pushes (news, calendar, quicklist) where we must NOT
        override the display -- we just need the heartbeat to keep SID=0x01 alive."""
        # Fast path by address via BlueZ, scan fallback, backoff + jitter, one-sided-failure
        # cleanup and stale-link clearing for these two addresses only (g2/ble.py).
        self._closing = False
        self._link_lost = asyncio.Event()
        self.disconnected_side = None
        if not (self.right_mac and self.left_mac):
            await self.resolve_pair()
        res = await ble.connect_with_retry(
            self.right_mac, self.left_mac, attempts=self.connect_attempts,
            connect_timeout=self.connect_timeout, scan_timeout=self.scan_timeout,
            concurrent=self.concurrent_connect, clear_stale=self.clear_stale,
            bluez=self._bluez, scanner_factory=self._scanner_factory,
            client_factory=self._client_factory, on_disconnect=self._on_link_lost,
            log=lambda m: print(m, flush=True))
        self.right_client, self.left_client = res.right, res.left
        print('Right connected?', self.right_client.is_connected, f'({res.sources.get(ble.norm(self.right_mac))})', flush=True)
        print('Left connected?', self.left_client.is_connected, f'({res.sources.get(ble.norm(self.left_mac))})', flush=True)

        self._assembler = tp.FrameAssembler()

        def on_notify(_c, data):
            self._on_control_notify(data)

        await self.right_client.start_notify(tp.CHAR_NOTIFY, on_notify)
        await asyncio.sleep(0.3)

        async def auth(client, label):
            magic, frame = tp.auth_frame()
            await client.write_gatt_char(tp.CHAR_WRITE, frame, response=False)
            if client is self.right_client:
                rx = await asyncio.wait_for(self.notes.get(), timeout=5.0)
                _, pb = tp.parse_rx(rx)
                print(f'auth {label}:', 'OK' if pb[4:] == b'\x1a\x00' else f'? {pb.hex()}', flush=True)
            else:
                print(f'auth {label}: sent', flush=True)

        await auth(self.right_client, 'right')
        await auth(self.left_client, 'left')
        await asyncio.sleep(0.3)

        if not launch_app:
            print('Skipping EvenHub app launch (launch_app=False) -- native dashboard stays active.', flush=True)
            return

        print('Sending prelude...', flush=True)
        await self.right_client.write_gatt_char(tp.CHAR_WRITE, eh.PRELUDE_F5872, response=False)
        await self._drain(1.5)

        if not skip_page:
            print('Creating display page...', flush=True)
            magic = self.next_magic()
            if self._page_payload_builder:
                payload = self._page_payload_builder(magic)
            else:
                payload = eh.build_create_input_page(magic, self._initial_text)
            await self._send_and_await_ack(eh.SID_EVENHUB, eh.FLAG_REQUEST, magic, payload, self.next_seq(), 'create-page')
        else:
            print('Skipping create-page (skip_page=True) -- native dashboard stays visible.', flush=True)

        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        print('Session up. Heartbeat running.', flush=True)

    async def _send_right(self, frames):
        """Write already-framed packets to the right lens control characteristic (0x5401). The
        write lock keeps a multi-packet message contiguous when several tasks send at once; the
        short gap between packets is only needed between packets, not after the last."""
        async with self._write_lock_right:
            for n, pkt in enumerate(frames):
                if n:
                    await asyncio.sleep(0.05)   # spacing confirmed on the glasses
                await self.right_client.write_gatt_char(tp.CHAR_WRITE, pkt, response=False)

    async def _send_left(self, frames):
        """Same as _send_right, for the left lens (used for messages the official app sends to
        both lenses, e.g. the 60 s keep-alive)."""
        async with self._write_lock_left:
            for n, pkt in enumerate(frames):
                if n:
                    await asyncio.sleep(0.05)   # spacing confirmed on the glasses
                await self.left_client.write_gatt_char(tp.CHAR_WRITE, pkt, response=False)

    # ── Public send API ──────────────────────────────────────────────────────────────────
    async def send(self, sid, payload, lens='right', flag=eh.FLAG_REQUEST):
        """Frame a protobuf `payload` for service `sid` and send it without waiting for a reply.
        lens: 'right' (the command lens, default), 'left', or 'both'."""
        if lens in ('right', 'both'):
            await self._send_right(eh.frame_pb(payload, sid, flag, self.next_seq()))
        if lens in ('left', 'both'):
            await self._send_left(eh.frame_pb(payload, sid, flag, self.next_seq()))

    async def request(self, sid, payload, magic, label='request', timeout_s=3.0):
        """Send `payload` to the right lens and return the reply's protobuf bytes (matched by
        `magic`, the value you put in the payload's field 2), or None on timeout."""
        return await self._send_and_get_payload(sid, eh.FLAG_REQUEST, magic, payload,
                                                self.next_seq(), label, timeout_s)

    async def _drain(self, timeout_s):
        """Discard everything arriving on self.notes for up to timeout_s seconds."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            try:
                await asyncio.wait_for(self.notes.get(), timeout=max(0.02, remaining))
            except asyncio.TimeoutError:
                break

    def _on_control_notify(self, data):
        """Every notification from the control characteristic (0x5402) of the right lens.
        Multi-packet messages are joined first (g2.transport.FrameAssembler); packets of an
        incomplete message are held back until its last packet arrives."""
        data = self._assembler.feed(data)
        if data is None:
            return
        self._route_ack(data)
        if self.notes.full():            # nobody is reading: drop the oldest, keep memory bounded
            try:
                self.notes.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.notes.put_nowait(data)
        # Fan out to a device-event listener (gestures, IMU, list/text clicks) without
        # stealing frames from the queue above -- _send_and_await_ack's own consumption of
        # self.notes must see every frame too, so this is a callback on arrival, not a
        # second queue.get() consumer (which would race it and randomly steal acks, the
        # same class of bug the heartbeat-loop drain caused earlier).
        if len(data) > 6 and data[0] == 0xAA and data[6] in (0xC4, 0xC5):
            parsed_file = parse_frame(data)
            if parsed_file:
                self.file_notes.put_nowait(parsed_file)
        if self.on_raw_frame is not None:
            try:
                tapped = parse_frame(data)
                if tapped:
                    self.on_raw_frame(*tapped)
            except Exception as e:
                print(f'  [on_raw_frame error: {e}]', flush=True)
        if self.on_device_event is not None:
            parsed = parse_frame(data)
            if parsed and parsed[0] == eh.SID_EVENHUB:
                event = eh.decode_device_event(parsed[2])
                if event is not None:
                    self.on_device_event(event)

    def _route_ack(self, data):
        """Called for every notification: if it is the reply some request is waiting for
        (same SID and magic), hand it to that request's future."""
        if not self._pending:
            return
        parsed = parse_frame(data)
        if not parsed:
            return
        svc_hi, _, pb = parsed
        fut = self._pending.pop((svc_hi, read_ack_magic(pb)), None)
        if fut is not None and not fut.done():
            fut.set_result(pb)

    async def _send_and_get_payload(self, sid, flag, magic, payload, seq, label, timeout_s=3.0):
        """Send `payload` on `sid` to the right lens and return the reply's protobuf (matched by SID
        + magic through the ack router), or None after timeout_s. Safe to call concurrently: each
        request waits on its own future and never consumes self.notes."""
        key = (sid, magic)
        fut = asyncio.get_running_loop().create_future()
        self._pending[key] = fut
        try:
            await self._send_right(eh.frame_pb(payload, sid, flag, seq))
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            print(f'  !! no ack for {label} (sid=0x{sid:02x} magic={magic})', flush=True)
            return None
        finally:
            if self._pending.get(key) is fut:
                del self._pending[key]

    async def _send_and_await_ack(self, sid, flag, magic, payload, seq, label, timeout_s=3.0):
        """Like _send_and_get_payload but returns True/False."""
        return await self._send_and_get_payload(sid, flag, magic, payload, seq, label, timeout_s) is not None

    async def query_settings(self):
        """LIVE-CONFIRMED. Read battery/charging/firmware-version from the glasses (SID_UI_SETTING, separate
        channel from display/mic/IMU). Returns a dict merging parse_settings_battery and
        parse_settings_firmware, or None if the read wasn't acked."""
        magic = self.next_magic()
        payload = eh.build_settings_query(magic)
        pb = await self._send_and_get_payload(eh.SID_UI_SETTING, eh.FLAG_REQUEST, magic, payload, self.next_seq(), 'settings-query')
        if pb is None:
            return None
        result = {}
        battery = eh.parse_settings_battery(pb)
        if battery:
            result.update(battery)
        firmware = eh.parse_settings_firmware(pb)
        if firmware:
            result.update(firmware)
        return result or None

    # ── EvenHub heartbeat control ────────────────────────────────────────────────────────
    @property
    def heartbeat_running(self):
        """True while the EvenHub heartbeat task (started by connect()) is alive."""
        return self._hb_task is not None and not self._hb_task.done()

    def start_heartbeat(self):
        """(Re)start the EvenHub heartbeat (SID 0xE0 every 4 s). connect(launch_app=True) already
        starts it; call this again before native dashboard (SID 0x01) pushes after you stopped
        it. Returns True if it was started now, False if it was already running or the session
        is closing. Must be called from inside the event loop."""
        if getattr(self, '_closing', False) or self.heartbeat_running:
            return False
        self._hb_task = asyncio.ensure_future(self._heartbeat_loop())
        return True

    def stop_heartbeat(self):
        """Stop the EvenHub heartbeat. After a native dashboard push this hands the screen back
        to the glasses' own dashboard, which shows the new content within about 5 s. Returns True
        if a running heartbeat was stopped. Same effect as cancelling `_hb_task` yourself."""
        task, self._hb_task = self._hb_task, None
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def _heartbeat_loop(self):
        """Background task started by connect(): an EvenHub heartbeat every 4 s, fire-and-forget.
        It lives in self._hb_task; cancel that task to stop the heartbeat (the hub does this so
        the native dashboard takes over and shows freshly pushed widgets)."""
        try:
            while True:
                await asyncio.sleep(4.0)
                magic = self.next_magic()
                payload = eh.build_heartbeat(magic)
                frames = eh.frame_pb(payload, eh.SID_EVENHUB, eh.FLAG_REQUEST, self.next_seq())
                # Fire-and-forget, like Faceclaw's own sendHeartbeat() (writeFrame, not
                # writeAndAwaitAck) -- draining a response here would race any other consumer
                # of self.notes (e.g. continuous IMU/list-event listening) for the same items.
                try:
                    await self._send_right(frames)
                except Exception:
                    pass  # BLE send errors are non-fatal; connection_loop detects disconnect
        except asyncio.CancelledError:
            pass

    async def recreate_page(self, page_payload_builder, container_ids=None):
        """Swap the whole page mid-session by reusing cmd=0 (createStartUpPageContainer), which
        the real SDK docs (nickustinov/even-g2-notes, docs/page-lifecycle.md) confirm is
        officially a ONE-SHOT call -- a second call is "invalid" and the documented way to swap
        pages mid-session is a separate rebuildPageContainer operation (see rebuild_page() below
        for a cmd=7 candidate). Live results reusing cmd=0 have been inconsistent: worked once
        (page_swap_test.py, both directions) out of many later attempts in the full jarvis_gui
        flow, where the swap silently didn't render at all despite the code running to
        completion. Always logs acked=False regardless of outcome. Prefer rebuild_page() once
        it's confirmed; this is kept for the one confirmed-working geometry/shape until then."""
        magic = self.next_magic()
        payload = page_payload_builder(magic)
        ok = await self._send_and_await_ack(eh.SID_EVENHUB, eh.FLAG_REQUEST, magic, payload, self.next_seq(), 'recreate-page')
        print(f'  [page recreated: acked={ok}]', flush=True)
        self._page_payload_builder = page_payload_builder
        self._container_ids = container_ids or {}
        return ok

    async def rebuild_page(self, page_payload_builder, container_ids=None):
        """Candidate replacement for recreate_page(), using cmd=7 (UpdateContainer) instead of
        reusing cmd=0. See build_rebuild_page()'s docstring in g2/evenhub.py for the full
        reasoning -- NOT YET LIVE-TESTED (written while the BLE connection was unavailable).
        page_payload_builder(magic) should return eh.build_rebuild_page(...)'s result. Same
        call shape as recreate_page() so a caller can try both without other code changes."""
        magic = self.next_magic()
        payload = page_payload_builder(magic)
        ok = await self._send_and_await_ack(eh.SID_EVENHUB, eh.FLAG_REQUEST, magic, payload, self.next_seq(), 'rebuild-page')
        print(f'  [page rebuilt (cmd=7): acked={ok}]', flush=True)
        self._page_payload_builder = page_payload_builder
        self._container_ids = container_ids or {}
        return ok

    async def update_text(self, text):
        """Replace the text of the page's main text container (text_container_name)."""
        await self.update_text_container(self._text_container_name, text)

    async def update_text_container(self, container_name, text):
        """Like update_text(), but targets any named text container -- update_text() is just
        this pinned to self._text_container_name (the primary reply area). Needed once a page
        has more than one text container (e.g. a dashboard layout with a clock/news/notes area
        alongside the reply area) since each is only addressable by its own real id (see
        container_ids in __init__ and build_text_upgrade's docstring for why id, not just name,
        matters once there's more than one text container)."""
        text = text.strip()
        if len(text) > 900:  # stock-firmware create-page cap is ~1000 chars; upgrade allows ~2000
            text = text[:897] + '...'
        container_id = self._container_ids.get(container_name, 1)
        magic = self.next_magic()
        payload = eh.build_text_upgrade(magic, container_id, container_name, text)
        ok = await self._send_and_await_ack(eh.SID_EVENHUB, eh.FLAG_REQUEST, magic, payload, self.next_seq(), 'text-upgrade')
        print(f'  [display updated ({container_name}): acked={ok}]', flush=True)

    async def enable_mic(self, enable=True):
        """Subscribe to RENDER_NOTIFY_UUID (raw LC3 mic packets, right lens) and send the
        real audio-control command on SID_EVENHUB. Confirmed real spec (Faceclaw's
        Lc3PacketFramer.kt / FaceclawBleCommunicator.java, not guessed): each notification is a
        raw 205-byte packet -- 200 bytes LC3 (5 frames x 40 bytes, 10ms/frame, 16kHz) + int16 LE
        signal-strength ratio + int16 LE direction-of-arrival degrees + 1-byte wrap counter.
        No envelope/CRC on this channel, unlike the control characteristic."""
        if enable and not self._mic_enabled:
            # Faceclaw's real connectArm() enables RENDER_NOTIFY on BOTH lenses (even though the
            # audio-control command itself is sent right-only) -- subscribe both here too, tag
            # each packet with its arm since it's not otherwise recoverable downstream.
            def make_handler(arm):
                def on_audio(_c, data):
                    self.audio_packets.put_nowait((arm, bytes(data)))
                return on_audio
            await self.right_client.start_notify(RENDER_NOTIFY_UUID, make_handler('R'))
            await self.left_client.start_notify(RENDER_NOTIFY_UUID, make_handler('L'))
            self._mic_enabled = True
        magic = self.next_magic()
        payload = eh.build_audio_control(magic, enable)
        ok = await self._send_and_await_ack(eh.SID_EVENHUB, eh.FLAG_REQUEST, magic, payload, self.next_seq(), 'audio-control')
        print(f'  [mic {"enabled" if enable else "disabled"}: acked={ok}]', flush=True)
        if not enable and self._mic_enabled:
            await self.right_client.stop_notify(RENDER_NOTIFY_UUID)
            await self.left_client.stop_notify(RENDER_NOTIFY_UUID)
            self._mic_enabled = False
        return ok

    async def enable_imu(self, enable=True, report_freq=500):
        """IMU streaming: wrapEvenHub(CMD_OPEN_IMU=19, ...) on SID_EVENHUB, right lens only.
        Reports arrive as ordinary async device-event notifications on the main notify channel
        (self.notes), not a separate characteristic like mic audio -- read them with
        eh.decode_device_event() on whatever comes out of self.notes."""
        magic = self.next_magic()
        payload = eh.build_imu_control(magic, enable, report_freq)
        ok = await self._send_and_await_ack(eh.SID_EVENHUB, eh.FLAG_REQUEST, magic, payload, self.next_seq(), 'imu-control')
        print(f'  [imu {"enabled" if enable else "disabled"}: acked={ok}]', flush=True)
        return ok

    async def start_audio_stream(self):
        """Subscribe to mic packets on 0x6402 (both lenses) WITHOUT sending the EvenHub
        audio-control command -- Even AI streams audio on its own after CTRL ENTER."""
        if self._mic_enabled:
            return

        def make_handler(arm):
            def on_audio(_c, data):
                self.audio_packets.put_nowait((arm, bytes(data)))
            return on_audio
        await self.right_client.start_notify(RENDER_NOTIFY_UUID, make_handler('R'))
        await self.left_client.start_notify(RENDER_NOTIFY_UUID, make_handler('L'))
        self._mic_enabled = True

    async def stop_audio_stream(self):
        """Unsubscribe from mic packets on both lenses (undoes start_audio_stream)."""
        if not self._mic_enabled:
            return
        for client in (self.right_client, self.left_client):
            try:
                await client.stop_notify(RENDER_NOTIFY_UUID)
            except Exception:
                pass
        self._mic_enabled = False

    async def enable_file_channel(self):
        """Subscribe to the file-service notify characteristic 0x7402 (right lens). Replies land
        in self.file_notes (and are also passed to on_raw_frame). UNTESTED live. Called for you by
        g2.file_service.send_file()."""
        if self._file_notify_on:
            return

        def on_file_notify(_c, data):
            data = bytes(data)
            parsed = parse_frame(data)
            if parsed:
                self.file_notes.put_nowait(parsed)
                if self.on_raw_frame is not None:
                    try:
                        self.on_raw_frame(*parsed)
                    except Exception as e:
                        print(f'  [on_raw_frame error: {e}]', flush=True)

        await self.right_client.start_notify(tp.UUID_BASE.format(0x7402), on_file_notify)
        self._file_notify_on = True

    async def send_file_frames(self, frames, gap_s=0.01):
        """Write raw EFS frames (from g2.file_service builders) to the file characteristic 0x7401
        on the right lens, gap_s seconds apart. UNTESTED live. Prefer g2.file_service.send_file()."""
        for pkt in frames:
            await self.right_client.write_gatt_char(tp.UUID_BASE.format(0x7401), pkt, response=False)
            await asyncio.sleep(gap_s)

    async def close(self):
        """Stop the heartbeat and disconnect both lenses (in parallel, each bounded to 5 s so
        close() never hangs on a dead link). Safe to call more than once."""
        self._closing = True
        if self._hb_task:
            self._hb_task.cancel()

        async def drop(client):
            if client and client.is_connected:
                try:
                    await asyncio.wait_for(client.disconnect(), 5.0)
                except (Exception, asyncio.TimeoutError) as e:
                    print(f'  (disconnect cleanup: {e})', flush=True)
        await asyncio.gather(drop(self.right_client), drop(self.left_client))


@contextlib.asynccontextmanager
async def open_session(right_mac=None, left_mac=None, *, launch_app=True, skip_page=True,
                       **session_kwargs):
    """Connect to a pair of glasses for the length of an `async with` block, and always close the
    connection afterwards (also when connect() fails half-way or the block raises):

        async with open_session() as s:                 # G2_RIGHT_MAC / G2_LEFT_MAC, or discovery
            print(await s.query_settings())

    The defaults (launch_app=True, skip_page=True) are what the hub uses: auth, prelude and the
    EvenHub heartbeat, with no custom page, so the native dashboard stays visible and native
    dashboard / menu / quicklist / Even AI messages are accepted. Pass skip_page=False to draw the
    default EvenHub text page (session_kwargs go to GlassesSession, e.g. initial_text=...).
    Only one program can hold the glasses at a time: stop the hub before using this."""
    session = GlassesSession(right_mac=right_mac, left_mac=left_mac, **session_kwargs)
    try:
        await session.connect(launch_app=launch_app, skip_page=skip_page)
        yield session
    finally:
        await session.close()
