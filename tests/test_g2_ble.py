"""Offline tests for g2/ble.py and GlassesSession's connect path -- fake scanner / client / BlueZ
objects only. Nothing here opens D-Bus or touches a Bluetooth adapter.

Run from the repo root:  .venv/bin/python tests/test_g2_ble.py      (or pytest)
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from g2 import ble, session as sess  # noqa: E402

R_MAC, L_MAC = 'CC:00:00:12:34:56', 'C4:00:00:AB:CD:EF'
R_NAME, L_NAME = 'Even G2_32_R_123456', 'Even G2_32_L_ABCDEF'
# Real manufacturer data of the right lens (bluetoothctl info, 2026-09-29).
R_MFR = bytes.fromhex('5332313147414245303030303031' '5634120000cc' '02')


# ── Fakes ────────────────────────────────────────────────────────────────────────────────────

class Dev:
    def __init__(self, address, name=None, details=None):
        self.address, self.name, self.details = address, name, details or {}


class Adv:
    def __init__(self, local_name, rssi=-50, manufacturer_data=None):
        self.local_name, self.rssi, self.manufacturer_data = local_name, rssi, manufacturer_data or {}


class FakeScanner:
    """Replays (delay_s, device, adv) adverts after start(); records start/stop."""
    instances = []

    def __init__(self, callback, adverts):
        self.callback, self.adverts = callback, adverts
        self.started = self.stopped = None
        self._tasks = []
        FakeScanner.instances.append(self)

    async def start(self):
        self.started = time.monotonic()
        loop = asyncio.get_running_loop()
        for delay, dev, adv in self.adverts:
            self._tasks.append(loop.call_later(delay, self._fire, dev, adv))

    def _fire(self, dev, adv):
        if self.stopped is None:
            self.callback(dev, adv)

    async def stop(self):
        self.stopped = time.monotonic()
        for t in self._tasks:
            t.cancel()


def scanner_factory(adverts):
    FakeScanner.instances = []
    return lambda cb: FakeScanner(cb, adverts)


def no_scan(cb):
    raise AssertionError('scanner must not be used on the fast path')


class FakeClient:
    """Connect outcome per address: plan[address] = (delay_s, exception_or_None)."""
    created = []

    def __init__(self, device, plan, disconnected_callback=None, timeout=None):
        self.device = device
        self.address = ble.norm(getattr(device, 'address', device))
        self.plan, self.cb, self.timeout = plan, disconnected_callback, timeout
        self.is_connected = False
        self.disconnects = 0
        self.writes = []
        self.notify = {}
        FakeClient.created.append(self)

    async def connect(self):
        delay, exc = self.plan.get(self.address, (0, None))
        await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        self.is_connected = True

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False

    def drop_link(self):                     # simulate the lens going away
        self.is_connected = False
        if self.cb:
            self.cb(self)

    async def start_notify(self, uuid, cb):
        self.notify[uuid] = cb

    async def stop_notify(self, uuid):
        self.notify.pop(uuid, None)

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append(bytes(data))
        if data[6] == 0x80 and self.notify:          # auth -> reply "08 04 10 <m> 1a 00"
            for cb in self.notify.values():
                cb(None, bytes([0xAA, 0x12, data[2], 6, 1, 1, 0x80, 0x00, 0x08, 0x04, 0x10,
                                data[2], 0x1A, 0x00, 0, 0]))


def client_factory(plan):
    FakeClient.created = []
    return lambda dev, disconnected_callback=None, timeout=None: FakeClient(dev, plan, disconnected_callback, timeout)


class FakeBlueZ:
    def __init__(self, known=None):
        self.known = known or {}
        self.disconnected = []

    async def known_devices(self):
        return {a: dict(i) for a, i in self.known.items()}

    async def disconnect(self, address):
        self.disconnected.append(ble.norm(address))
        if address in self.known:
            self.known[address]['connected'] = False


def known_info(addr, name, connected=False, paired=True):
    return {'address': addr, 'path': f'/org/bluez/hci0/dev_{addr.replace(":", "_")}',
            'adapter': '/org/bluez/hci0', 'name': name, 'paired': paired, 'bonded': paired,
            'trusted': paired, 'connected': connected, 'rssi': None, 'manufacturer': {},
            'props': {'Address': addr, 'Alias': name, 'Name': name}}


def run(coro):
    return asyncio.run(coro)


def quiet(*_a, **_k):
    pass


# ── Names, manufacturer data, grouping ───────────────────────────────────────────────────────

def test_parse_name_and_manufacturer():
    assert ble.parse_name(L_NAME) == {'pair_id': '32', 'side': 'left', 'mac_tail': 'ABCDEF'}
    assert ble.parse_name('Some Headphones') is None and ble.parse_name(None) is None
    m = ble.parse_manufacturer(R_MFR)
    assert m == {'serial': 'S211GABE000001', 'mac': R_MAC, 'flag': 2}


def test_pair_grouping_by_name():
    lenses = [
        {'address': R_MAC, 'name': R_NAME, 'rssi': -50, 'serial': 'S211', 'pair_id': '32', 'side': 'right'},
        {'address': L_MAC, 'name': L_NAME, 'rssi': -45, 'serial': None, 'pair_id': '32', 'side': 'left'},
        {'address': 'AA:00:00:00:00:01', 'name': 'Even G2_7_R_000001', 'rssi': -80, 'serial': None,
         'pair_id': '7', 'side': 'right'},
    ]
    known = {L_MAC: known_info(L_MAC, L_NAME, connected=False),
             'BB:00:00:00:00:02': known_info('BB:00:00:00:00:02', 'Even G2_9_L_000002', connected=True),
             'DD:00:00:00:00:03': known_info('DD:00:00:00:00:03', 'Keyboard')}
    pairs = ble.group_pairs(lenses, known)
    assert set(pairs) == {'32', '7', '9'}
    p = pairs['32']
    assert (p['right_mac'], p['left_mac'], p['right_name'], p['left_name']) == (R_MAC, L_MAC, R_NAME, L_NAME)
    assert p['complete'] and p['serial'] == 'S211' and p['right_rssi'] == -50
    assert p['left_bonded'] and p['left_known'] and not p['right_known'] and not p['right_bonded']
    assert not pairs['7']['complete'] and pairs['7']['left_mac'] is None
    nine = pairs['9']                                   # known to BlueZ, connected, not advertising
    assert nine['left_connected'] and not nine['left_seen'] and nine['left_rssi'] is None


# ── Backoff ──────────────────────────────────────────────────────────────────────────────────

def test_backoff_schedule():
    mid = lambda: 0.5                                   # noqa: E731 -- zero jitter
    assert ble.backoff_schedule(7, rng=mid) == [1, 2, 4, 8, 16, 30, 30]
    assert ble.backoff_schedule(3, base=0.5, factor=3, cap=100, rng=mid) == [0.5, 1.5, 4.5]
    assert ble.backoff_delay(2, rng=lambda: 0.0) == 4 * 0.75     # -25 %
    assert ble.backoff_delay(2, rng=lambda: 1.0) == 4 * 1.25     # +25 %
    for i in range(10):
        d = ble.backoff_delay(i)
        assert 0 <= d <= 30 * 1.25


# ── Scanning ─────────────────────────────────────────────────────────────────────────────────

def test_scan_stops_as_soon_as_both_lenses_seen():
    adverts = [(0.01, Dev('11:22:33:44:55:66', 'Phone'), Adv('Phone')),
               (0.02, Dev(R_MAC), Adv(R_NAME)),
               (0.03, Dev('AA:00:00:00:00:01'), Adv('Even G2_7_L_000001')),
               (0.04, Dev(L_MAC.lower()), Adv(L_NAME)),
               (5.00, Dev('AA:00:00:00:00:09'), Adv('Even G2_7_R_000009'))]
    t0 = time.monotonic()
    found = run(ble.find_lenses([R_MAC, L_MAC], timeout=10, scanner_factory=scanner_factory(adverts)))
    assert set(found) == {R_MAC, L_MAC}
    assert time.monotonic() - t0 < 1.0
    assert FakeScanner.instances[0].stopped is not None


def test_scan_times_out_when_a_lens_is_missing():
    adverts = [(0.01, Dev(R_MAC), Adv(R_NAME))]
    t0 = time.monotonic()
    found = run(ble.find_lenses([R_MAC, L_MAC], timeout=0.2, scanner_factory=scanner_factory(adverts)))
    assert set(found) == {R_MAC} and 0.15 < time.monotonic() - t0 < 1.0
    assert FakeScanner.instances[0].stopped is not None


def test_session_scan_both_uses_early_stop():
    adverts = [(0.01, Dev(R_MAC), Adv(R_NAME)), (0.02, Dev(L_MAC), Adv(L_NAME))]
    s = sess.GlassesSession(initial_text='', right_mac=R_MAC.lower(), left_mac=L_MAC)
    s._scanner_factory = scanner_factory(adverts)
    r, l = run(s.scan_both(attempts=1, timeout=10))
    assert (r.address, l.address) == (R_MAC, L_MAC)


def test_stream_and_discover_pairs():
    adverts = [(0.01, Dev(R_MAC), Adv(R_NAME, -60, {ble.MANUFACTURER_ID: R_MFR})),
               (0.02, Dev(R_MAC), Adv(R_NAME, -59)),            # < 3 dB change: no event
               (0.03, Dev(R_MAC), Adv(R_NAME, -50)),            # 10 dB change: event
               (0.04, Dev('AA:00:00:00:00:01'), Adv('Even G2_7_R_000001', -90))]
    bluez = FakeBlueZ({L_MAC: known_info(L_MAC, L_NAME, connected=True)})

    async def collect():
        out = []
        async for p in ble.stream_pairs(timeout=0.3, scanner_factory=scanner_factory(adverts), bluez=bluez):
            out.append((p['pair_id'], p['right_rssi'], p['complete']))
        return out
    events = run(collect())
    assert events == [('32', None, False), ('32', -60, True), ('32', -50, True), ('7', -90, False)]

    updates = []
    pairs = run(ble.discover_pairs(timeout=0.3, scanner_factory=scanner_factory(adverts), bluez=bluez,
                                   on_update=updates.append))
    assert [p['pair_id'] for p in pairs] == ['32', '7'] and len(updates) == 4
    p = pairs[0]
    assert p['serial'] == 'S211GABE000001' and p['left_connected'] and not p['left_seen']
    for k in ('pair_id', 'right_mac', 'left_mac', 'right_name', 'left_name'):   # legacy keys
        assert k in p

    # stop_when ends the scan early
    t0 = time.monotonic()
    run(ble.discover_pairs(timeout=10, scanner_factory=scanner_factory(adverts), bluez=bluez,
                           stop_when=lambda ps: any(x['complete'] for x in ps.values())))
    assert time.monotonic() - t0 < 1.0


def test_session_discover_pairs_wrapper():
    adverts = [(0.01, Dev(R_MAC), Adv(R_NAME)), (0.02, Dev(L_MAC), Adv(L_NAME))]
    saved = ble._default_scanner_factory, ble._default_bluez
    ble._default_scanner_factory, ble._default_bluez = scanner_factory(adverts), FakeBlueZ()
    try:
        pairs = run(sess.discover_pairs(timeout=0.1))
    finally:
        ble._default_scanner_factory, ble._default_bluez = saved
    assert len(pairs) == 1 and pairs[0]['right_mac'] == R_MAC and pairs[0]['left_mac'] == L_MAC


def test_session_without_addresses_resolves_a_discovered_pair():
    adverts = [(0.01, Dev('AA:00:00:00:00:07'), Adv('Even G2_7_R_000007')),     # incomplete pair
               (0.02, Dev(R_MAC), Adv(R_NAME)), (0.03, Dev(L_MAC), Adv(L_NAME))]
    s = sess.GlassesSession(initial_text='')
    s.right_mac = s.left_mac = None                      # as with no G2_RIGHT_MAC / G2_LEFT_MAC set
    s._scanner_factory, s._bluez = scanner_factory(adverts), FakeBlueZ()
    t0 = time.monotonic()
    p = run(s.resolve_pair(timeout=10))
    assert p['pair_id'] == '32' and (s.right_mac, s.left_mac) == (R_MAC, L_MAC)
    assert time.monotonic() - t0 < 1.0                   # stops as soon as a complete pair is seen
    s2 = sess.GlassesSession(initial_text='')
    s2.right_mac = s2.left_mac = None
    s2._scanner_factory, s2._bluez = scanner_factory(adverts[:1]), FakeBlueZ()
    try:
        run(s2.resolve_pair(timeout=0.1))
        raise AssertionError('expected RuntimeError')
    except RuntimeError as e:
        assert 'G2_RIGHT_MAC' in str(e)


# ── Connecting ───────────────────────────────────────────────────────────────────────────────

def test_fast_path_by_address_skips_scan():
    bluez = FakeBlueZ({R_MAC: known_info(R_MAC, R_NAME), L_MAC: known_info(L_MAC, L_NAME)})
    cf = client_factory({R_MAC: (0.05, None), L_MAC: (0.05, None)})
    t0 = time.monotonic()
    res = run(ble.connect_with_retry(R_MAC, L_MAC, bluez=bluez, scanner_factory=no_scan,
                                     client_factory=cf, log=quiet))
    assert res.sources == {R_MAC: 'bluez', L_MAC: 'bluez'} and res.attempts == 1
    assert res.right.is_connected and res.left.is_connected
    assert res.right.device.details['path'].endswith('dev_CC_00_00_12_34_56')   # BLEDevice w/ D-Bus path
    assert time.monotonic() - t0 < 0.09                                          # concurrent, not 0.1 s
    assert bluez.disconnected == []


def test_one_lens_failure_cleans_up_the_other():
    for concurrent in (True, False):
        cf = client_factory({R_MAC: (0, None), L_MAC: (0, TimeoutError('left timed out'))})
        try:
            run(ble.connect_pair(Dev(R_MAC), Dev(L_MAC), client_factory=cf, concurrent=concurrent))
            raise AssertionError('should have failed')
        except ble.PairConnectError as e:
            assert e.errors['right'] is None and isinstance(e.errors['left'], TimeoutError)
        right = next(c for c in FakeClient.created if c.address == R_MAC)
        assert right.disconnects == 1 and not right.is_connected


def test_right_failure_sequential_does_not_try_left():
    cf = client_factory({R_MAC: (0, OSError('boom'))})
    try:
        run(ble.connect_pair(Dev(R_MAC), Dev(L_MAC), client_factory=cf, concurrent=False))
        raise AssertionError('should have failed')
    except ble.PairConnectError as e:
        assert e.errors['left'] is None and 'boom' in str(e)
    assert [c.address for c in FakeClient.created] == [R_MAC]


def test_cancel_during_connect_cleans_up():
    cf = client_factory({R_MAC: (0, None), L_MAC: (5, None)})

    async def go():
        t = asyncio.ensure_future(ble.connect_pair(Dev(R_MAC), Dev(L_MAC), client_factory=cf))
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            return 'cancelled'
    assert run(go()) == 'cancelled'
    assert all(not c.is_connected for c in FakeClient.created)
    assert next(c for c in FakeClient.created if c.address == R_MAC).disconnects == 1


def test_retry_clears_stale_link_backs_off_then_scans():
    bluez = FakeBlueZ({R_MAC: known_info(R_MAC, R_NAME, connected=True), L_MAC: known_info(L_MAC, L_NAME),
                       'EE:00:00:00:00:05': known_info('EE:00:00:00:00:05', 'Other', connected=True)})
    plan = {R_MAC: (0, RuntimeError('[org.bluez.Error.InProgress] Operation already in progress'))}
    cf = client_factory(plan)
    adverts = [(0.01, Dev(R_MAC), Adv(R_NAME)), (0.02, Dev(L_MAC), Adv(L_NAME))]
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        plan.pop(R_MAC)                                  # the stale link is gone after clearing

    res = run(ble.connect_with_retry(R_MAC, L_MAC, attempts=3, bluez=bluez,
                                     scanner_factory=scanner_factory(adverts), client_factory=cf,
                                     sleep=fake_sleep, backoff={'rng': lambda: 0.5}, log=quiet))
    assert res.attempts == 2 and res.sources == {R_MAC: 'scan', L_MAC: 'scan'}
    assert sleeps == [1.0]
    assert set(bluez.disconnected) == {R_MAC, L_MAC}          # ours only, never EE:..:05


def test_not_advertising_gives_up_after_attempts():
    bluez = FakeBlueZ()
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
    try:
        run(ble.connect_with_retry(R_MAC, L_MAC, attempts=3, scan_timeout=0.05, bluez=bluez,
                                   scanner_factory=scanner_factory([(0.01, Dev(R_MAC), Adv(R_NAME))]),
                                   client_factory=client_factory({}), sleep=fake_sleep,
                                   backoff={'rng': lambda: 0.5}, log=quiet))
        raise AssertionError('should have failed')
    except ble.PairConnectError as e:
        assert 'not advertising' in str(e.errors['left']) and e.errors['right'] is None
    assert sleeps == [1.0, 2.0]
    assert FakeClient.created == []                            # never connected half a pair
    assert isinstance(ble.PairConnectError('x'), RuntimeError)  # hub's `except Exception` still works


def test_session_connect_close_and_link_loss_callback():
    bluez = FakeBlueZ({R_MAC: known_info(R_MAC, R_NAME), L_MAC: known_info(L_MAC, L_NAME)})
    s = sess.GlassesSession(initial_text='', right_mac=R_MAC, left_mac=L_MAC)
    s._bluez, s._scanner_factory = bluez, no_scan
    s._client_factory = client_factory({})
    seen = []
    s.on_disconnect = seen.append

    async def go():
        await s.connect(launch_app=False)
        assert s.is_connected and s.right_client.is_connected and s.left_client.is_connected
        waiter = asyncio.ensure_future(s.wait_disconnected())
        await asyncio.sleep(0.01)
        assert not waiter.done()
        s.left_client.drop_link()
        side = await asyncio.wait_for(waiter, 1.0)
        await s.close()
        await s.close()                                            # idempotent
        return side
    assert run(go()) == 'left' and seen == ['left']
    assert not s.right_client.is_connected and s.right_client.disconnects == 1
    assert len(s.right_client.writes) == 1 and len(s.left_client.writes) == 1   # one auth each


def test_close_does_not_report_link_loss():
    bluez = FakeBlueZ({R_MAC: known_info(R_MAC, R_NAME), L_MAC: known_info(L_MAC, L_NAME)})
    s = sess.GlassesSession(initial_text='', right_mac=R_MAC, left_mac=L_MAC)
    s._bluez, s._scanner_factory, s._client_factory = bluez, no_scan, client_factory({})
    seen = []
    s.on_disconnect = seen.append

    async def go():
        await s.connect(launch_app=False)
        await s.close()
        s.right_client.drop_link()                                  # callback arrives after close
    run(go())
    assert seen == []


def test_agent_builds_offline():
    from dbus_fast.service import ServiceInterface
    agent = ble.make_agent(passkey_cb=lambda dev: 123456)
    methods = {m.name: m for m in ServiceInterface._get_methods(agent)}   # what the bus would export
    assert {n: (m.in_signature, m.out_signature) for n, m in methods.items()} == {
        'AuthorizeService': ('os', ''), 'Cancel': ('', ''), 'DisplayPasskey': ('ouq', ''),
        'DisplayPinCode': ('os', ''), 'Release': ('', ''), 'RequestAuthorization': ('o', ''),
        'RequestConfirmation': ('ou', ''), 'RequestPasskey': ('o', 'u'), 'RequestPinCode': ('o', 's')}
    assert methods['RequestPasskey'].fn(agent, '/org/bluez/hci0/dev_X') == 123456
    methods['RequestConfirmation'].fn(agent, '/org/bluez/hci0/dev_X', 1)       # auto-accept
    assert ('RequestConfirmation', '/org/bluez/hci0/dev_X', 1) in agent.log


def test_parse_managed_objects():
    from dbus_fast import Variant
    objs = {'/org/bluez/hci0': {'org.bluez.Adapter1': {'Address': Variant('s', '00:11:22:33:44:55')}},
            '/org/bluez/hci0/dev_CC_00_00_12_34_56': {'org.bluez.Device1': {
                'Address': Variant('s', R_MAC), 'Name': Variant('s', R_NAME), 'Paired': Variant('b', True),
                'Bonded': Variant('b', True), 'Trusted': Variant('b', True), 'Connected': Variant('b', False),
                'RSSI': Variant('n', -50), 'AddressType': Variant('s', 'random'),
                'Adapter': Variant('o', '/org/bluez/hci0'),
                'ManufacturerData': Variant('a{qv}', {ble.MANUFACTURER_ID: Variant('ay', R_MFR)})}}}
    k = ble.parse_managed_objects(objs)
    assert list(k) == [R_MAC]
    i = k[R_MAC]
    assert i['bonded'] and i['trusted'] and not i['connected'] and i['rssi'] == -50
    assert ble.parse_manufacturer(i['manufacturer'][ble.MANUFACTURER_ID])['serial'] == 'S211GABE000001'
    assert i['props']['Name'] == R_NAME and i['adapter'] == '/org/bluez/hci0'


if __name__ == '__main__':
    import contextlib
    import io
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                fn()
            print(f'PASS {name}')
        except Exception as e:
            failed += 1
            print(f'FAIL {name}: {e!r}')
    print(f'{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)
