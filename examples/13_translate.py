"""Show live two-language subtitles in the glasses' Translate app (SID 0x05). You translate.

    us      → CTRL OPEN {"EN>ES", srcKey, dstKey}   opens the Translate screen
    glasses → COMM_RESP {errorCode}                  for every message we send; 0 = OK
    us      → HEARTBEAT every 4 s
    us      → RESULT {src, dst, endFlag}             a partial line (endFlag 0) is replaced by the
                                                     next one; endFlag 1 commits it
    glasses → NOTIFY PAUSE/RESUME/CLOSE, LANG_SWITCH_REQ (answered with LANG_SWITCH_RSP OK)
    us      → CTRL CLOSE

Lines are given as  --line "Original|Translation"  (repeatable). Each line is first sent as a
partial (its first half) and then as the final line, the way live speech recognition updates.
The OPEN deliberately leaves useAudio unset, so the glasses do not stream their microphone.
Every message gets a fresh magic: the glasses reject a repeated one (error 66).
"""
import asyncio
import sys

import _common as c
from g2 import translate as tr

STATUS = (c.ACK_CONFIRMED + ' (OPEN, final and partial TEXT, heartbeats and CLOSE all answered COMM_RESP OK '
          'live 2026-10-01; how it looks on the glass is not verified: docs/visual_checks.md #3)')
DEFAULT_LINES = ['Good morning|Buenos días', 'Where is the train station?|¿Dónde está la estación de tren?',
                 'Thank you very much|Muchas gracias']


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--src', default='EN', help='source language code (default EN)')
    p.add_argument('--dst', default='ES', help='target language code (default ES)')
    p.add_argument('--line', action='append', default=[], metavar='"SRC|DST"', help='one subtitle (repeatable)')
    p.add_argument('--interval', type=float, default=4.0, help='seconds per line (default 4)')
    p.add_argument('--no-partials', action='store_true', help='send only final lines')
    return p


def parse_line(spec):
    if '|' not in spec:
        raise ValueError(f'expected "original|translation", got {spec!r}')
    src, dst = spec.split('|', 1)
    return src.strip(), dst.strip()


def halves(text):
    words = text.split()
    return ' '.join(words[:max(1, len(words) // 2)])


class Translate:
    def __init__(self, session):
        self.s = session
        self.closed = asyncio.Event()
        self.errors = []
        self.loop = asyncio.get_running_loop()
        self.tasks = set()

    def on_frame(self, sid, flag, pb):
        if sid != tr.SID:
            return
        msg = tr.decode(pb)
        if msg['cmd'] == tr.CMD_COMM_RESP:
            self.errors.append(msg['error'] or 0)
            if msg['error']:
                print(f'  glasses: {msg["label"]}', flush=True)
            return
        if msg['cmd'] != tr.CMD_HEARTBEAT:
            print(f'  glasses: {msg["label"]}', flush=True)
        if msg['cmd'] == tr.CMD_LANG_SWITCH_REQ:
            t = self.loop.create_task(self.s.send(tr.SID, tr.build_lang_switch_response(self.s.next_magic())))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
        elif msg['cmd'] == tr.CMD_NOTIFY and msg['value'] == tr.TCMD_CLOSE:
            self.closed.set()

    async def heartbeat(self):
        while True:
            await asyncio.sleep(tr.HEARTBEAT_INTERVAL_S)
            await self.s.send(tr.SID, tr.build_heartbeat(self.s.next_magic()))


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    try:
        lines = [parse_line(x) for x in (args.line or DEFAULT_LINES)]
    except ValueError as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    c.banner('Translate', STATUS, f'{args.src.upper()}>{args.dst.upper()}, {len(lines)} line(s)')
    async with c.glasses(args) as s:
        app = Translate(s)
        c.listen(s, app.on_frame)
        await s.send(tr.SID, tr.build_open(s.next_magic(), args.src, args.dst))
        hb = asyncio.ensure_future(app.heartbeat())
        try:
            await asyncio.sleep(0.5)
            for src, dst in lines:
                if app.closed.is_set():
                    print('Closed on the glasses.')
                    break
                print(f'  {src}  →  {dst}', flush=True)
                if not args.no_partials:
                    await s.send(tr.SID, tr.build_text(s.next_magic(), halves(src), halves(dst), final=False))
                    await asyncio.sleep(min(1.0, args.interval / 2))
                await s.send(tr.SID, tr.build_text(s.next_magic(), src, dst, final=True))
                await asyncio.sleep(args.interval)
        finally:
            hb.cancel()
            if app.tasks:
                await asyncio.gather(*app.tasks, return_exceptions=True)
            if not app.closed.is_set():
                await s.send(tr.SID, tr.build_close(s.next_magic()))
                await asyncio.sleep(0.5)
    bad = [e for e in app.errors if e]
    print(f'{len(app.errors)} COMM_RESP answer(s), {len(bad)} error(s)' + (f': {bad}' if bad else ''))
    return 0 if app.errors and not bad else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
