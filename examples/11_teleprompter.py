"""Show a script in the glasses' Teleprompter app (SID 0x06), serving pages as the glasses ask.

    us      → CONTROL START {mode, totalPages, totalLines, displayWidth 567, displayLines 9}
    glasses → COMM_RESP (0 = OK), then PAGE_DATA_REQUEST {pageId}
    us      → PAGE_DATA {pageId, lineCount, "line\\nline ... \\n"}
    glasses → PAGE_LOAD_DONE, PAGE_SCROLL_SYNC while the user scrolls
    us      → HEARTBEAT every 6 s, CONTROL CLOSE at the end (or the user exits first)

The script comes from --text, --file or a built-in sample. It is word-wrapped to
--chars-per-line (the real width in characters is not known; 25-30 is a good range) and split
into pages of 10 lines.
"""
import asyncio
import sys

import _common as c
from g2 import teleprompter as tp

STATUS = (c.ACK_CONFIRMED + ' (START, page requests, PAGE_LOAD_DONE and CLOSE all acked live 2026-10-01; '
          'how it looks on the glass is not verified: docs/visual_checks.md #4)')
SAMPLE = ('Good morning everyone. Thank you for coming. Today I want to show you how a few lines of '
          'Python can put a script in front of your eyes.\n\nThe glasses ask for each page when they '
          'need it, and this script answers. Scroll with the touch bar to move through the text.')


def build_parser():
    p = c.parser(__doc__, STATUS)
    src = p.add_mutually_exclusive_group()
    src.add_argument('--text', help='script text')
    src.add_argument('--file', help='read the script from a text file')
    p.add_argument('--mode', choices=('manual', 'auto'), default='manual',
                   help='manual: scroll by touch (default); auto: scroll on a timer')
    p.add_argument('--scroll-ms', type=int, default=3000, help='auto mode: ms per line (default 3000)')
    p.add_argument('--chars-per-line', type=int, default=tp.CHARS_PER_LINE)
    p.add_argument('--seconds', type=float, default=60.0, help='how long to keep it open (default 60)')
    return p


class Teleprompter:
    def __init__(self, session, pages):
        self.s, self.pages = session, pages
        self.closed = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.tasks = set()
        self.served = []
        self.responses = asyncio.Queue()     # COMM_RESP error codes, in arrival order

    def on_frame(self, sid, flag, pb):
        if sid != tp.SID:
            return
        msg = tp.decode(pb)
        if msg['cmd'] != tp.CMD_HEARTBEAT:
            print(f'  glasses: {msg["label"]}', flush=True)
        if msg['cmd'] == tp.CMD_PAGE_DATA_REQUEST and msg['page'] is not None:
            t = self.loop.create_task(self.serve(msg['page']))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
        elif msg['cmd'] == tp.CMD_COMM_RESP:
            self.responses.put_nowait(msg['error'] or 0)
        elif tp.is_close(msg):
            self.closed.set()

    async def serve(self, page_id):
        if 0 <= page_id < len(self.pages):
            await self.s.send(tp.SID, tp.build_page(self.s.next_magic(), page_id, self.pages[page_id]))
            self.served.append(page_id)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(tp.HEARTBEAT_INTERVAL_S)
            await self.s.send(tp.SID, tp.build_heartbeat(self.s.next_magic()))


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    if args.file:
        with open(args.file, encoding='utf-8') as f:
            script = f.read()
    else:
        script = args.text or SAMPLE
    pages = tp.paginate(script, chars_per_line=args.chars_per_line)
    if not pages:
        print('Error: the script is empty', file=sys.stderr)
        return 2
    total_pages, total_lines = tp.totals(pages)
    c.banner('Teleprompter', STATUS, f'{total_pages} page(s), {total_lines} line(s)')
    mode = tp.MODE_AUTO if args.mode == 'auto' else tp.MODE_MANUAL
    extra = {'scroll_interval_ms': args.scroll_ms} if mode == tp.MODE_AUTO else {}
    async with c.glasses(args) as s:
        app = Teleprompter(s, pages)
        c.listen(s, app.on_frame)
        await s.send(tp.SID, tp.build_start(s.next_magic(), total_pages, total_lines, mode=mode, **extra))
        try:      # every app message is answered with COMM_RESP {errCode}; 0 = SUCCESS
            err = await asyncio.wait_for(app.responses.get(), 3.0)
        except asyncio.TimeoutError:
            err = None
        print(f'START → {"no answer" if err is None else tp.ERROR_NAMES.get(err, err)}')
        hb = asyncio.ensure_future(app.heartbeat())
        try:
            closed = asyncio.ensure_future(app.closed.wait())
            await asyncio.wait({closed}, timeout=args.seconds)
            closed.cancel()
        finally:
            hb.cancel()
            if app.tasks:
                await asyncio.gather(*app.tasks, return_exceptions=True)
            if not app.closed.is_set():
                await s.send(tp.SID, tp.build_close(s.next_magic()))
                await asyncio.sleep(0.5)
    print(f'Pages served: {app.served}')
    return 0 if err == 0 else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
