"""g2.ble -- fast, robust Bluetooth LE discovery and connection for G2 lenses (bleak + BlueZ).

Plain English
-------------
Each lens is its own BLE peripheral with a name like "Even G2_32_L_ABCDEF" (pair id 32, side L/R,
last 3 bytes of its address) and manufacturer data (company 0x5245 "RE") that carries the PAIR
SERIAL (same on both lenses, e.g. "S211GABE000001"), the lens MAC (little-endian) and one flag
byte. A lens stops advertising while any host holds a connection to it.

This module is the connection layer under g2.session.GlassesSession:

    iter_lenses()          async generator: every G2 advertisement heard (callback scan)
    find_lenses()          scan until ALL wanted addresses are seen, then stop at once
    stream_pairs()         async generator of pair updates (for a "scanning..." UI)
    discover_pairs()       scan + BlueZ cache -> list of pairs with RSSI / bonded / connected
    connect_pair()         connect both lenses (concurrently or in order); on a one-sided
                           failure the connected lens is disconnected again
    connect_with_retry()   BlueZ fast path by address, scan fallback, exponential backoff with
                           jitter, stale-connection clearing for OUR two addresses only
    BlueZ                  small org.bluez D-Bus helper: known devices (read-only), disconnect,
                           trust, pair/bond (with a NoInputNoOutput agent), remove ("forget")

Everything takes injectable factories (scanner_factory, client_factory, bluez, sleep) so it is
fully testable offline -- see tests/test_g2_ble.py. Nothing here runs at import time.

Status
------
    known_devices() (read-only D-Bus)                  checked against the local BlueZ 5.85
    scan / find / group / backoff / cleanup logic      OFFLINE-TESTED with fakes
    fast path by address, concurrent connect,
    stale clearing, disconnect callbacks                UNTESTED LIVE
    BlueZ.pair / bond / set_trusted / remove / agent    UNTESTED LIVE -- never called by tests
                                                        except with fakes
See docs/research/ble_connection.md for the research behind this.
"""
import asyncio
import contextlib
import random
import re

NAME_RE = re.compile(r'G2_(\w+?)_([LR])_([0-9A-Fa-f]{6})')
MANUFACTURER_ID = 0x5245          # "RE" -- seen on both lenses of the development pair

BLUEZ_SERVICE = 'org.bluez'
DEVICE_IFACE = 'org.bluez.Device1'
ADAPTER_IFACE = 'org.bluez.Adapter1'
AGENT_MANAGER_IFACE = 'org.bluez.AgentManager1'
PROPS_IFACE = 'org.freedesktop.DBus.Properties'
OBJECT_MANAGER_IFACE = 'org.freedesktop.DBus.ObjectManager'

# Substrings of BlueZ / bleak errors that usually mean "BlueZ still thinks a link exists or a
# connect is half-done" -- worth a Device1.Disconnect on OUR address before the next attempt.
STALE_HINTS = ('inprogress', 'in progress', 'alreadyconnected', 'already connected',
               'le-connection-abort', 'br-connection-canceled', 'connection-abort',
               'not connected', 'software caused connection abort', 'org.bluez.error.failed')

NOT_ADVERTISING_HINT = ('not advertising: switched off, in the case, out of range, or held by '
                        'another host (phone app / another program) -- a connected lens stops '
                        'advertising')


# ── Names, manufacturer data, backoff ────────────────────────────────────────────────────────

def parse_name(name):
    """"Even G2_32_L_ABCDEF" -> {'pair_id': '32', 'side': 'left', 'mac_tail': 'ABCDEF'}.
    Returns None for anything that is not a G2 lens name."""
    m = NAME_RE.search(name or '')
    if not m:
        return None
    return {'pair_id': m.group(1), 'side': 'right' if m.group(2) == 'R' else 'left',
            'mac_tail': m.group(3).upper()}


def parse_manufacturer(data):
    """Decode the 0x5245 manufacturer data: <serial ascii> <6-byte MAC, little-endian> <flag>.
    Observed (bluetoothctl info, both lenses): a serial shaped like 'S211GABE000001' (example value), flag 0x02. The meaning of
    the flag byte is unknown. Returns {'serial', 'mac', 'flag'} or None if too short."""
    data = bytes(data or b'')
    if len(data) < 8:
        return None
    serial = data[:-7].decode('ascii', 'replace')
    mac = ':'.join(f'{b:02X}' for b in reversed(data[-7:-1]))
    return {'serial': serial, 'mac': mac, 'flag': data[-1]}


def backoff_delay(attempt, base=1.0, factor=2.0, cap=30.0, jitter=0.25, rng=random.random):
    """Delay before retry number `attempt` (0 = after the first failure): base*factor**attempt,
    capped at `cap`, then scaled by a random factor in [1-jitter, 1+jitter] so several hosts /
    lenses do not retry in lock-step."""
    d = min(cap, base * (factor ** attempt))
    return max(0.0, d * (1.0 + jitter * (2.0 * rng() - 1.0)))


def backoff_schedule(n, **kw):
    """The first n delays of backoff_delay (handy for logging and tests)."""
    return [backoff_delay(i, **kw) for i in range(n)]


def looks_stale(exc):
    """True if an exception text suggests BlueZ holds a stale / half-open link."""
    text = f'{type(exc).__name__} {exc}'.lower()
    return any(h in text for h in STALE_HINTS)


def norm(address):
    return (address or '').upper()


# ── BlueZ D-Bus helper ───────────────────────────────────────────────────────────────────────

def _unwrap(v):
    """dbus-fast Variant -> plain Python value (recursively for dicts/lists)."""
    if type(v).__name__ == 'Variant':
        v = v.value
    if isinstance(v, dict):
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


def parse_managed_objects(objects):
    """GetManagedObjects() result -> {ADDRESS: info} for every org.bluez.Device1.
    info keys: address, path, adapter, name, paired, bonded, trusted, connected, blocked, rssi,
    address_type, manufacturer ({company_id: bytes}), props (all Device1 properties, unwrapped;
    this is the dict bleak expects in BLEDevice.details['props'])."""
    out = {}
    for path, ifaces in (objects or {}).items():
        dev = ifaces.get(DEVICE_IFACE)
        if dev is None:
            continue
        props = _unwrap(dev)
        addr = norm(props.get('Address'))
        if not addr:
            continue
        paired = bool(props.get('Paired', False))
        out[addr] = {
            'address': addr, 'path': path, 'adapter': props.get('Adapter') or path.rsplit('/', 1)[0],
            'name': props.get('Name') or props.get('Alias'),
            'paired': paired, 'bonded': bool(props.get('Bonded', paired)),
            'trusted': bool(props.get('Trusted', False)),
            'connected': bool(props.get('Connected', False)),
            'blocked': bool(props.get('Blocked', False)),
            'rssi': props.get('RSSI'), 'address_type': props.get('AddressType'),
            'manufacturer': {int(k): bytes(v) for k, v in (props.get('ManufacturerData') or {}).items()},
            'props': props,
        }
    return out


class BlueZ:
    """Minimal org.bluez client over dbus-fast (bundled with bleak). One system-bus connection,
    reopened if the event loop changes. Only known_devices() is read-only; everything else
    changes host Bluetooth state and is UNTESTED LIVE.

    Every method takes an address (any case). Nothing here is ever called on an address the
    caller did not pass in -- connect_with_retry only passes the session's own two lenses."""

    def __init__(self, bus_factory=None):
        self._bus_factory = bus_factory
        self._bus = None
        self._loop = None
        self._agent = None

    async def _get_bus(self):
        loop = asyncio.get_running_loop()
        if self._bus is None or self._loop is not loop or not getattr(self._bus, 'connected', True):
            if self._bus_factory is not None:
                self._bus = await self._bus_factory()
            else:
                from dbus_fast import BusType
                from dbus_fast.aio import MessageBus
                self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self._loop = loop
        return self._bus

    async def _call(self, path, interface, member, signature='', body=None, timeout=None):
        from dbus_fast import Message, MessageType
        bus = await self._get_bus()
        call = bus.call(Message(destination=BLUEZ_SERVICE, path=path, interface=interface,
                                member=member, signature=signature, body=body or []))
        reply = await (asyncio.wait_for(call, timeout) if timeout else call)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f'{reply.error_name}: {reply.body[0] if reply.body else ""}')
        return reply.body

    async def known_devices(self):
        """READ-ONLY. {ADDRESS: info} for every device BlueZ knows (see parse_managed_objects).
        Returns {} when BlueZ / D-Bus is unavailable (non-Linux, no bluetoothd)."""
        try:
            body = await self._call('/', OBJECT_MANAGER_IFACE, 'GetManagedObjects', timeout=5.0)
        except Exception:
            return {}
        return parse_managed_objects(body[0])

    async def device(self, address):
        return (await self.known_devices()).get(norm(address))

    async def _path(self, address):
        info = await self.device(address)
        if info is None:
            raise LookupError(f'BlueZ does not know {address} (scan first so it has a device object)')
        return info

    async def disconnect(self, address):
        """UNTESTED LIVE. Device1.Disconnect -- drops a (possibly stale) link held by THIS host.
        Equivalent to `bluetoothctl disconnect <mac>`. Cannot free a lens held by another host."""
        info = await self._path(address)
        await self._call(info['path'], DEVICE_IFACE, 'Disconnect', timeout=10.0)

    async def set_trusted(self, address, trusted=True):
        """UNTESTED LIVE. Device1.Trusted = trusted (`bluetoothctl trust/untrust <mac>`)."""
        from dbus_fast import Variant
        info = await self._path(address)
        await self._call(info['path'], PROPS_IFACE, 'Set', 'ssv',
                         [DEVICE_IFACE, 'Trusted', Variant('b', bool(trusted))], timeout=5.0)

    async def pair(self, address, timeout=40.0):
        """UNTESTED LIVE. Device1.Pair (`bluetoothctl pair <mac>`). Needs an agent for anything but
        Just Works -- call register_agent() first when no desktop agent is running. Pair also
        connects the lens; it then stops advertising until disconnected."""
        info = await self._path(address)
        if info['paired']:
            return 'already-paired'
        await self._call(info['path'], DEVICE_IFACE, 'Pair', timeout=timeout)
        return 'paired'

    async def bond(self, address, timeout=40.0, agent=True):
        """UNTESTED LIVE. The one-time host setup for one lens: (agent) + trust + pair.
        Run it for BOTH lenses of a pair. Returns 'paired' or 'already-paired'."""
        if agent:
            await self.register_agent()
        await self.set_trusted(address, True)
        return await self.pair(address, timeout=timeout)

    async def remove(self, address):
        """UNTESTED LIVE. "Forget": Adapter1.RemoveDevice -- deletes the host's bond keys and the
        device object (`bluetoothctl remove <mac>`). The lens keeps ITS copy of the bond; see
        docs/research/ble_connection.md for what that means for re-pairing."""
        info = await self._path(address)
        await self._call(info['adapter'], ADAPTER_IFACE, 'RemoveDevice', 'o', [info['path']],
                         timeout=10.0)

    async def register_agent(self, capability='NoInputNoOutput', path='/g2/agent', passkey_cb=None):
        """UNTESTED LIVE. Export a pairing agent and make it BlueZ's default. NoInputNoOutput makes
        LE Secure Connections fall back to Just Works (what the G2 used with bleak's pair() on
        2026-09-25). Confirmation / authorization requests are auto-accepted; PIN / passkey
        requests are answered by passkey_cb(device_path) if given, else rejected."""
        if self._agent is not None:
            return self._agent
        bus = await self._get_bus()
        agent = make_agent(passkey_cb)
        bus.export(path, agent)
        await self._call('/org/bluez', AGENT_MANAGER_IFACE, 'RegisterAgent', 'os', [path, capability])
        with contextlib.suppress(Exception):
            await self._call('/org/bluez', AGENT_MANAGER_IFACE, 'RequestDefaultAgent', 'o', [path])
        self._agent = agent
        return agent

    async def close(self):
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.disconnect()
        self._bus = None


def make_agent(passkey_cb=None):
    """Build an org.bluez.Agent1 service object (dbus-fast). Separate so it can be constructed
    offline (tests check the D-Bus signatures parse) without touching the bus."""
    from dbus_fast import DBusError
    from dbus_fast.service import ServiceInterface, method

    class _Agent(ServiceInterface):
        def __init__(self):
            super().__init__('org.bluez.Agent1')
            self.log = []

        @method()
        def Release(self):
            self.log.append('Release')

        @method()
        def RequestPinCode(self, device: 'o') -> 's':
            self.log.append(('RequestPinCode', device))
            if passkey_cb is None:
                raise DBusError('org.bluez.Error.Rejected', 'no PIN available')
            return str(passkey_cb(device))

        @method()
        def DisplayPinCode(self, device: 'o', pincode: 's'):
            self.log.append(('DisplayPinCode', device, pincode))

        @method()
        def RequestPasskey(self, device: 'o') -> 'u':
            self.log.append(('RequestPasskey', device))
            if passkey_cb is None:
                raise DBusError('org.bluez.Error.Rejected', 'no passkey available')
            return int(passkey_cb(device))

        @method()
        def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'):
            self.log.append(('DisplayPasskey', device, passkey))

        @method()
        def RequestConfirmation(self, device: 'o', passkey: 'u'):
            self.log.append(('RequestConfirmation', device, passkey))   # accept

        @method()
        def RequestAuthorization(self, device: 'o'):
            self.log.append(('RequestAuthorization', device))           # accept

        @method()
        def AuthorizeService(self, device: 'o', uuid: 's'):
            self.log.append(('AuthorizeService', device, uuid))         # accept

        @method()
        def Cancel(self):
            self.log.append('Cancel')

    return _Agent()


_default_bluez = None


def default_bluez():
    """Shared BlueZ helper (lazily created, no bus opened until first use)."""
    global _default_bluez
    if _default_bluez is None:
        _default_bluez = BlueZ()
    return _default_bluez


# ── Scanning ─────────────────────────────────────────────────────────────────────────────────

def _default_scanner_factory(detection_callback):
    from bleak import BleakScanner
    return BleakScanner(detection_callback=detection_callback)


def lens_from_adv(device, adv=None):
    """(BLEDevice, AdvertisementData) -> lens dict, or None if not a G2 lens."""
    name = (getattr(adv, 'local_name', None) if adv is not None else None) or getattr(device, 'name', None)
    parsed = parse_name(name)
    if not parsed:
        return None
    mfr = (getattr(adv, 'manufacturer_data', None) or {}) if adv is not None else {}
    info = parse_manufacturer(mfr.get(MANUFACTURER_ID)) if MANUFACTURER_ID in mfr else None
    return {'address': norm(device.address), 'name': name, 'rssi': getattr(adv, 'rssi', None),
            'serial': info['serial'] if info else None, 'device': device, **parsed}


async def iter_lenses(timeout=10.0, scanner_factory=None):
    """Async generator: one lens dict per G2 advertisement heard (repeats included), until
    `timeout` seconds pass. Breaking out stops the scanner -- wrap in contextlib.aclosing() (or
    use the helpers below) so that happens immediately."""
    queue = asyncio.Queue()

    def on_adv(device, adv):
        lens = lens_from_adv(device, adv)
        if lens:
            queue.put_nowait(lens)

    scanner = (scanner_factory or _default_scanner_factory)(on_adv)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    await scanner.start()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                lens = await asyncio.wait_for(queue.get(), remaining)
            except asyncio.TimeoutError:
                return
            yield lens
    finally:
        with contextlib.suppress(Exception):
            await scanner.stop()


async def find_lenses(addresses, timeout=10.0, scanner_factory=None):
    """Scan until every address in `addresses` has advertised (then stop immediately) or
    `timeout` passes. Returns {ADDRESS: BLEDevice} for the ones seen."""
    wanted = {norm(a) for a in addresses}
    found = {}
    if not wanted:
        return found
    async with contextlib.aclosing(iter_lenses(timeout, scanner_factory)) as it:
        async for lens in it:
            if lens['address'] in wanted:
                found[lens['address']] = lens['device']
                if wanted <= found.keys():
                    break
    return found


def _empty_pair(pair_id):
    p = {'pair_id': pair_id, 'serial': None}
    for side in ('right', 'left'):
        p.update({f'{side}_mac': None, f'{side}_name': None, f'{side}_rssi': None,
                  f'{side}_seen': False, f'{side}_known': False, f'{side}_bonded': False,
                  f'{side}_trusted': False, f'{side}_connected': False})
    return p


def _complete(p):
    return bool(p['right_mac'] and p['left_mac'])


def group_pairs(lenses, known=None):
    """Group lens dicts (from scanning) and BlueZ-known devices into pairs by pair id.
    Returns {pair_id: pair}. Pair keys (the first five are the historic discover_pairs keys):
        pair_id, right_mac, left_mac, right_name, left_name,
        serial, complete, and per side: *_rssi, *_seen (advertised in this scan),
        *_known (BlueZ has a device object), *_bonded, *_trusted, *_connected (to THIS host)."""
    pairs = {}
    for info in (known or {}).values():
        parsed = parse_name(info.get('name'))
        if not parsed:
            continue
        p = pairs.setdefault(parsed['pair_id'], _empty_pair(parsed['pair_id']))
        s = parsed['side']
        p[f'{s}_mac'] = p[f'{s}_mac'] or info['address']
        p[f'{s}_name'] = p[f'{s}_name'] or info['name']
        p[f'{s}_known'] = True
        p[f'{s}_bonded'] = bool(info.get('bonded') or info.get('paired'))
        p[f'{s}_trusted'] = bool(info.get('trusted'))
        p[f'{s}_connected'] = bool(info.get('connected'))
        mfr = parse_manufacturer((info.get('manufacturer') or {}).get(MANUFACTURER_ID))
        if mfr and not p['serial']:
            p['serial'] = mfr['serial']
    for lens in lenses:
        _apply_lens(pairs, lens, known)
    for p in pairs.values():
        p['complete'] = _complete(p)
    return pairs


def _apply_lens(pairs, lens, known=None):
    """Merge one scanned lens into `pairs`. Returns (pair, changed)."""
    p = pairs.setdefault(lens['pair_id'], _empty_pair(lens['pair_id']))
    s = lens['side']
    before = (p[f'{s}_mac'], p[f'{s}_seen'], p[f'{s}_rssi'])
    p[f'{s}_mac'], p[f'{s}_name'], p[f'{s}_seen'] = lens['address'], lens['name'], True
    if lens.get('rssi') is not None:
        p[f'{s}_rssi'] = lens['rssi']
    if lens.get('serial') and not p['serial']:
        p['serial'] = lens['serial']
    info = (known or {}).get(lens['address'])
    if info:
        p[f'{s}_known'] = True
        p[f'{s}_bonded'] = bool(info.get('bonded') or info.get('paired'))
        p[f'{s}_trusted'] = bool(info.get('trusted'))
        p[f'{s}_connected'] = bool(info.get('connected'))
    p['complete'] = _complete(p)
    return p, before != (p[f'{s}_mac'], p[f'{s}_seen'], p[f'{s}_rssi'])


async def stream_pairs(timeout=10.0, scanner_factory=None, bluez=None, include_known=True,
                       rssi_delta=3, stop_when=None):
    """Live discovery for a UI. Async generator yielding a COPY of a pair dict (group_pairs
    format) every time a lens is first seen or its RSSI moves by >= rssi_delta dB. When
    include_known, pairs BlueZ already knows (e.g. the one connected right now, which does not
    advertise) are yielded first. stop_when(pairs) -> True ends the scan early."""
    known = {}
    if include_known:
        known = await (bluez or default_bluez()).known_devices()
    pairs = group_pairs([], known)
    for p in list(pairs.values()):
        yield dict(p)
    last_rssi = {}
    async with contextlib.aclosing(iter_lenses(timeout, scanner_factory)) as it:
        async for lens in it:
            first = lens['address'] not in last_rssi
            p, _ = _apply_lens(pairs, lens, known)
            prev = last_rssi.get(lens['address'])
            rssi = lens.get('rssi')
            if first or (rssi is not None and (prev is None or abs(rssi - prev) >= rssi_delta)):
                last_rssi[lens['address']] = rssi
                yield dict(p)
            if stop_when is not None and stop_when(pairs):
                break


async def discover_pairs(timeout=10.0, scanner_factory=None, bluez=None, include_known=True,
                         on_update=None, stop_when=None):
    """Scan for `timeout` seconds and return a list of pairs (group_pairs format), sorted with
    complete pairs first, then by strongest RSSI. on_update(pair) is called for each streamed
    update (same events as stream_pairs). The first five keys match the old
    session.discover_pairs output, so existing callers keep working."""
    latest = {}
    async with contextlib.aclosing(stream_pairs(timeout, scanner_factory, bluez, include_known,
                                                stop_when=stop_when)) as it:
        async for p in it:
            latest[p['pair_id']] = p
            if on_update is not None:
                on_update(dict(p))

    def key(p):
        rssis = [r for r in (p['right_rssi'], p['left_rssi']) if r is not None]
        return (not p['complete'], -(max(rssis) if rssis else -999))
    return sorted(latest.values(), key=key)


# ── Connecting ───────────────────────────────────────────────────────────────────────────────

class PairConnectError(RuntimeError):
    """Connecting a pair failed. .errors = {'right': exc|None, 'left': exc|None};
    .stale = True if an error looked like a stale BlueZ link."""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or {}
        self.stale = any(e is not None and looks_stale(e) for e in self.errors.values())


def _default_client_factory(device, disconnected_callback=None, timeout=20.0):
    from bleak import BleakClient
    return BleakClient(device, disconnected_callback=disconnected_callback, timeout=timeout)


def ble_device_from_bluez(info):
    """BlueZ known-device info -> bleak BLEDevice with details {'path', 'props'}: BleakClient then
    calls Device1.Connect directly instead of scanning first (bleak's BlueZ backend only scans
    when it has no D-Bus path)."""
    from bleak.backends.device import BLEDevice
    return BLEDevice(info['address'], info.get('name'), {'path': info['path'], 'props': info['props']})


async def safe_disconnect(client, timeout=5.0):
    if client is None:
        return
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(client.disconnect(), timeout)


async def connect_pair(right_device, left_device, *, client_factory=None, timeout=12.0,
                       concurrent=True, on_disconnect=None):
    """Connect both lenses. Returns (right_client, left_client). If either fails (or the caller
    is cancelled) every client this call created is disconnected again, then PairConnectError is
    raised (CancelledError is re-raised as is). on_disconnect(side, client) is wired as bleak's
    disconnected_callback so link loss is noticed immediately."""
    factory = client_factory or _default_client_factory
    created = []

    async def one(side, device):
        cb = None
        if on_disconnect is not None:
            def cb(client, _side=side):
                on_disconnect(_side, client)
        client = factory(device, disconnected_callback=cb, timeout=timeout)
        created.append(client)
        await asyncio.wait_for(client.connect(), timeout + 2.0)
        return client

    errors = {'right': None, 'left': None}
    clients = {'right': None, 'left': None}
    try:
        if concurrent:
            results = await asyncio.gather(one('right', right_device), one('left', left_device),
                                           return_exceptions=True)
            for side, r in zip(('right', 'left'), results):
                if isinstance(r, BaseException):
                    errors[side] = r
                else:
                    clients[side] = r
        else:
            for side, dev in (('right', right_device), ('left', left_device)):
                try:
                    clients[side] = await one(side, dev)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    errors[side] = e
                    break
    except BaseException:
        for c in created:
            await safe_disconnect(c)
        raise
    if any(isinstance(e, asyncio.CancelledError) for e in errors.values()):
        for c in created:
            await safe_disconnect(c)
        raise asyncio.CancelledError()
    if errors['right'] or errors['left'] or not (clients['right'] and clients['left']):
        for c in created:
            await safe_disconnect(c)
        parts = [f'{s}: {type(e).__name__}: {e}' for s, e in errors.items() if e is not None]
        raise PairConnectError('connect failed (' + '; '.join(parts or ['not attempted']) + ')', errors)
    return clients['right'], clients['left']


class ConnectResult:
    def __init__(self, right, left, attempts, sources):
        self.right, self.left, self.attempts, self.sources = right, left, attempts, sources

    def __iter__(self):
        return iter((self.right, self.left))


async def resolve_devices(addresses, *, known, prefer_known=True, scan_timeout=8.0,
                          scanner_factory=None):
    """{ADDRESS: device} plus {ADDRESS: 'bluez'|'scan'}. Known-to-BlueZ addresses are used
    directly (no scan) when prefer_known; the rest are found with an early-stopping scan."""
    devices, sources = {}, {}
    missing = []
    for a in map(norm, addresses):
        if prefer_known and a in known and known[a].get('path'):
            devices[a], sources[a] = ble_device_from_bluez(known[a]), 'bluez'
        else:
            missing.append(a)
    if missing:
        found = await find_lenses(missing, scan_timeout, scanner_factory)
        for a, d in found.items():
            devices[a], sources[a] = d, 'scan'
    return devices, sources


async def connect_with_retry(right_mac, left_mac, *, attempts=3, connect_timeout=12.0,
                             scan_timeout=8.0, concurrent=True, clear_stale='on_failure',
                             bluez=None, scanner_factory=None, client_factory=None,
                             on_disconnect=None, sleep=asyncio.sleep, backoff=None, log=print):
    """Connect a pair, retrying with exponential backoff + jitter.

    Attempt 1 uses the BlueZ fast path (connect by D-Bus path, no scan) for lenses BlueZ already
    knows, and connects both lenses concurrently if `concurrent`. Later attempts scan first
    (early stop as soon as both are seen -- this also tells "not advertising" apart from a
    connect error) and connect right-then-left, the historically proven order.

    clear_stale: 'on_failure' (default) -- after a failed attempt, Device1.Disconnect OUR two
    addresses if BlueZ still reports them connected or the error looks stale (what the hub did
    with `bluetoothctl disconnect`); 'always' -- also before the first attempt (kicks any other
    local program sharing the link!); 'never'.
    backoff: dict of backoff_delay kwargs. Returns ConnectResult (iterable as (right, left))."""
    bluez = bluez or default_bluez()
    ours = [norm(right_mac), norm(left_mac)]
    backoff = backoff or {}
    last = None

    async def clear(reason, known):
        for a in ours:
            info = known.get(a)
            if info and (info.get('connected') or reason == 'stale-error'):
                try:
                    await bluez.disconnect(a)
                    log(f'  cleared BlueZ link to {a} ({reason})')
                except Exception as e:
                    log(f'  (could not clear {a}: {e})')

    for attempt in range(attempts):
        known = await bluez.known_devices()
        if attempt == 0 and clear_stale == 'always':
            await clear('pre-connect', known)
            known = await bluez.known_devices()
        first = attempt == 0
        log(f'Connecting (attempt {attempt + 1}/{attempts}, '
            f'{"BlueZ fast path" if first else "scan"}, {"concurrent" if first and concurrent else "sequential"})...')
        try:
            devices, sources = await resolve_devices(ours, known=known, prefer_known=first,
                                                     scan_timeout=scan_timeout,
                                                     scanner_factory=scanner_factory)
            missing = [a for a in ours if a not in devices]
            if missing:
                errs = {side: (LookupError(f'{a} {NOT_ADVERTISING_HINT}') if a in missing else None)
                        for side, a in zip(('right', 'left'), ours)}
                raise PairConnectError('lens(es) not found: ' + ', '.join(missing), errs)
            r, l = await connect_pair(devices[ours[0]], devices[ours[1]],
                                      client_factory=client_factory, timeout=connect_timeout,
                                      concurrent=concurrent and first, on_disconnect=on_disconnect)
            return ConnectResult(r, l, attempt + 1, sources)
        except PairConnectError as e:
            last = e
            log(f'  attempt {attempt + 1} failed: {e}')
            if clear_stale != 'never':
                await clear('stale-error' if e.stale else 'still-connected', await bluez.known_devices())
        if attempt + 1 < attempts:
            delay = backoff_delay(attempt, **backoff)
            log(f'  retrying in {delay:.1f}s')
            await sleep(delay)
    raise last
