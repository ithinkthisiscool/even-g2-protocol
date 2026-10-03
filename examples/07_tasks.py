"""Push a task list (the quicklist widget, SID 0x0c) and react to ticks and paging from the glasses.

The phone owns the list: the glasses can only tick tasks done/undone and scroll. Each task keeps
a stable uid (the glasses report ticks by uid). At most 20 tasks fit in one packet; when the user
scrolls past them the glasses send a paging EVENT (UPDATE_DATA_DOWN/UP + pivot uid), answered
with the next/previous 20 (build_quicklist_add_page). Titles go out as NUL-terminated UTF-8
bytes, at most 200 bytes.

Sequence (as the hub does it): heartbeat + base settings, FULL_UPDATE on SID 0x0c (acked by
echoing the magic), quicklist display setting on SID 0x01, stop the heartbeat, then listen for
--listen seconds and print every tick.
"""
import asyncio
import sys

import _common as c
from g2 import dashboard, evenhub as eh, quicklist as ql

STATUS = (f'{c.CONFIRMED_LIVE} list push; {c.EXPERIMENTAL} ticks and paging '
          '(decoders implemented, never seen live; docs/visual_checks.md #11)')


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('tasks', nargs='*', help='task titles (default: a few samples); prefix "x " for done')
    p.add_argument('--many', type=int, default=0, metavar='N', help='add N numbered tasks (try 30 for paging)')
    p.add_argument('--listen', type=float, default=60.0, help='seconds to listen for ticks (default 60)')
    return p


def make_tasks(titles, many=0):
    """[{uid, title, completed}] with stable uids 1..n."""
    titles = list(titles) or ['Buy milk', 'x Call mom', 'Book flights']
    titles += [f'Task {i}' for i in range(1, many + 1)]
    tasks = []
    for t in titles:
        done = t.startswith('x ')
        tasks.append({'uid': len(tasks) + 1, 'title': t[2:] if done else t, 'completed': done})
    return tasks


def items(tasks, all_tasks):
    """build_* item tuples: (uid, index, is_completed, timestamp, title, ts_type). No reminder:
    timestamp 0 + TS_TYPE_TIME, like the app."""
    return [(t['uid'], all_tasks.index(t), t['completed'], 0, t['title'], ql.TS_TYPE_TIME) for t in tasks]


class TaskListener:
    """Folds glasses → app quicklist messages into the local task list. Runs inside the Bluetooth
    callback, so paging answers are scheduled as tasks."""

    def __init__(self, session, tasks, sent_magics):
        self.session, self.tasks, self.sent = session, tasks, sent_magics
        self.loop = asyncio.get_running_loop()
        self.pending = set()

    def __call__(self, sid, flag, pb):
        if sid != ql.SID_QUICKLIST:
            return
        if eh.read_varint_field(pb, 2, -1) in self.sent:
            return                                          # an ack echo of our own push
        event = ql.decode_event(pb)
        if event is not None:
            task = self.loop.create_task(self.answer_paging(*event))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)
            return
        by_uid = {t['uid']: t for t in self.tasks}
        for u in ql.decode_status_updates(pb):
            t = by_uid.get(u['uid'])
            if t is None:
                print(f'  tick for unknown uid {u["uid"]}')
            elif t['completed'] != u['completed']:
                t['completed'] = u['completed']
                print(f'  ✓ "{t["title"]}" marked {"done" if t["completed"] else "not done"} on the glasses', flush=True)

    async def answer_paging(self, event, pivot_uid):
        page = ql.page_around(self.tasks, event, pivot_uid)
        magic = self.session.next_magic()
        self.sent.add(magic)
        print(f'  paging {"up" if event == ql.EVENT_UPDATE_DATA_UP else "down"} from uid {pivot_uid}: '
              f'sending {len(page)} task(s)', flush=True)
        await self.session.send(ql.SID_QUICKLIST, ql.build_quicklist_add_page(items(page, self.tasks), magic))


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    c.banner('Tasks (quicklist)', STATUS)
    tasks = make_tasks(args.tasks, args.many)
    first = tasks[:ql.MAX_ITEMS_PER_PACKET]
    async with c.glasses(args) as s:
        sent = set()
        listener = TaskListener(s, tasks, sent)
        c.listen(s, listener)
        await c.prepare_dashboard(s)
        magic = s.next_magic()
        sent.add(magic)
        ack = await s.request(ql.SID_QUICKLIST, ql.build_quicklist_full_update(items(first, tasks), magic),
                              magic, 'quicklist')
        print(f'Pushed {len(first)} of {len(tasks)} task(s): {"acked" if ack is not None else "NO ACK"}')
        await c.dashboard_push(s, dashboard.build_quicklist_display_enable, 'quicklist display setting')
        c.show_native_dashboard(s)
        await c.hold(args.listen, 'listening for ticks and paging')
        if listener.pending:
            await asyncio.gather(*listener.pending)
    print('\nFinal list:')
    for t in tasks:
        print(f"  [{'x' if t['completed'] else ' '}] {t['uid']:3} {t['title']}")
    return 0 if ack is not None else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
