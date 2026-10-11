"""Offline tests for the small g2 conveniences used by examples/library/ (no glasses needed):
GlassesSession.start_heartbeat / stop_heartbeat / heartbeat_running, g2.session.open_session,
and g2.dashboard.ack_flag / news_fifo_count / news_status / limit_utf8.

Run from the repo root:   .venv/bin/python tests/test_g2_conveniences.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from g2 import dashboard  # noqa: E402
from g2 import evenhub as eh  # noqa: E402
from g2 import session as session_mod  # noqa: E402
from g2.session import GlassesSession, open_session  # noqa: E402


# ── heartbeat control ────────────────────────────────────────────────────────────────────────

def test_heartbeat_start_stop_is_idempotent():
    async def run():
        s = GlassesSession(initial_text='')
        assert not s.heartbeat_running
        assert s.start_heartbeat() is True
        assert s.heartbeat_running
        assert s.start_heartbeat() is False          # already running: no second task
        task = s._hb_task
        assert s.stop_heartbeat() is True
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
        assert s._hb_task is None and not s.heartbeat_running
        assert s.stop_heartbeat() is False
    asyncio.run(run())


def test_heartbeat_is_not_restarted_on_a_closing_session():
    async def run():
        s = GlassesSession(initial_text='')
        await s.close()                               # no clients: just marks the session closing
        assert s.start_heartbeat() is False and not s.heartbeat_running
    asyncio.run(run())


def test_stop_heartbeat_matches_cancelling_hb_task():
    async def run():
        s = GlassesSession(initial_text='')
        s._hb_task = asyncio.ensure_future(s._heartbeat_loop())   # what connect() does
        assert s.heartbeat_running
        s.stop_heartbeat()
        await asyncio.sleep(0)
        assert s._hb_task is None
    asyncio.run(run())


# ── open_session ─────────────────────────────────────────────────────────────────────────────

class _Patched:
    """Temporarily replace GlassesSession.connect / close with recorders."""

    def __init__(self, fail_connect=False):
        self.calls = []
        self.fail_connect = fail_connect

    def __enter__(self):
        self._orig = GlassesSession.connect, GlassesSession.close
        rec = self

        async def connect(s, launch_app=True, skip_page=False):
            rec.calls.append(('connect', s.right_mac, s.left_mac, launch_app, skip_page))
            if rec.fail_connect:
                raise RuntimeError('no complete G2 pair found')

        async def close(s):
            rec.calls.append(('close',))
        GlassesSession.connect, GlassesSession.close = connect, close
        return self

    def __exit__(self, *exc):
        GlassesSession.connect, GlassesSession.close = self._orig


def test_open_session_connects_with_hub_defaults_and_closes():
    async def run():
        with _Patched() as p:
            async with open_session('AA:R', 'AA:L', initial_text='x') as s:
                assert isinstance(s, GlassesSession) and s._initial_text == 'x'
                p.calls.append(('body',))
        return p.calls
    calls = asyncio.run(run())
    assert calls == [('connect', 'AA:R', 'AA:L', True, True), ('body',), ('close',)], calls


def test_open_session_closes_when_the_body_raises():
    async def run():
        with _Patched() as p:
            try:
                async with open_session('R', 'L', skip_page=False):
                    raise ValueError('boom')
            except ValueError:
                pass
        return p.calls
    calls = asyncio.run(run())
    assert calls == [('connect', 'R', 'L', True, False), ('close',)], calls


def test_open_session_closes_when_connect_fails():
    async def run():
        with _Patched(fail_connect=True) as p:
            try:
                async with open_session():
                    raise AssertionError('body must not run')
            except RuntimeError as e:
                assert 'no complete G2 pair' in str(e)
        return p.calls
    calls = asyncio.run(run())
    assert [c[0] for c in calls] == ['connect', 'close'], calls


def test_open_session_is_exported_next_to_the_class():
    assert session_mod.open_session is open_session


# ── dashboard reply helpers ──────────────────────────────────────────────────────────────────

def _pb(*fields):
    out = b''
    for f, v in fields:
        out += eh.encode_message_field(f, v) if isinstance(v, bytes) else eh.encode_varint_field(f, v)
    return out


def test_ack_flag_reads_field_2_not_the_package_id():
    ok = _pb((1, 1), (2, 77), (3, eh.encode_varint_field(1, 1)))        # packageId 1, flag absent
    assert dashboard.ack_flag(ok) == dashboard.ACK_SUCCESS
    bad = _pb((1, 1), (2, 77), (3, eh.encode_varint_field(1, 1) + eh.encode_varint_field(2, 1)))
    assert dashboard.ack_flag(bad) == dashboard.ACK_PARAMETER_ERROR
    assert dashboard.ACK_FLAG_NAMES[dashboard.ack_flag(bad)] == 'DASHBOARD_PARAMETER_ERROR'
    assert dashboard.ack_flag(_pb((1, 1), (2, 77), (3, b''))) == 0      # empty respond = success
    assert dashboard.ack_flag(None) is None
    assert dashboard.ack_flag(_pb((1, 8), (2, 77))) is None


def test_news_fifo_count_and_status():
    assert dashboard.news_fifo_count(_pb((1, 8), (2, 5), (10, eh.encode_varint_field(1, 4)))) == 4
    assert dashboard.news_fifo_count(None) == 5
    assert dashboard.news_fifo_count(_pb((1, 8), (2, 5), (10, b''))) == 5
    assert dashboard.news_status(_pb((1, 10), (2, 6), (12, b''))) == 0
    assert dashboard.news_status(_pb((1, 10), (2, 6), (12, eh.encode_varint_field(5, 3)))) == 3
    assert dashboard.news_status(None) is None


def test_limit_utf8_cuts_on_character_boundaries():
    assert dashboard.limit_utf8('short', 10) == 'short'
    assert dashboard.limit_utf8('', 10) == '' and dashboard.limit_utf8(None, 10) == ''
    cut = dashboard.limit_utf8('héllo world', 8)
    assert cut == 'héll...' and len(cut.encode('utf-8')) <= 8
    cut = dashboard.limit_utf8('€' * 50, 62)                              # 3-byte characters
    assert len(cut.encode('utf-8')) <= 62 and cut.endswith('...') and cut[:-3] == '€' * 19
    assert dashboard.limit_utf8('abcdef', 4, ellipsis='') == 'abcd'


if __name__ == '__main__':
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'PASS {name}')
        except Exception as e:
            failed += 1
            print(f'FAIL {name}: {e!r}')
    print(f'{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)
