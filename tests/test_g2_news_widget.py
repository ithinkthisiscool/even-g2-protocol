"""Push news articles to the news widget of the native dashboard (SID 0x01, the V3 FIFO path).

Articles are given as  --item "Title|Source|Article text"  (repeatable); without --item three
samples are sent. The sequence is the one the official app and the hub use:
  1. EvenHub heartbeat running, base settings sent;
  2. reset the news FIFO (cmd 13);
  3. ask how many slots there are (cmd 7 → 5 so far);
  4. one AppSendNewsData (cmd 9) per slot: news_id = slot index, report_time in MILLISECONDS of
     LOCAL wall-clock time; the articles are repeated to fill every slot, like the app;
  5. stop the heartbeat so the dashboard shows the news.

Byte limits: the app cuts the strings to 62 / 126 / 7000 UTF-8 bytes. Which of the first two
belongs to title and which to source is inferred, so this script cuts both title and source to
62 bytes (fits either reading) and the article text to 7000.
"""
import asyncio
import sys
import time

import _common as c
from g2 import dashboard

STATUS = c.CONFIRMED_LIVE + ' (every step acked live; on-glass rendering of this exact sequence not logged)'
TITLE_MAX = SOURCE_MAX = 62
CONTENT_MAX = 7000
SAMPLES = [('Python reaches the glasses', 'g2 examples', 'This article was pushed with the g2 library.'),
           ('Five slots, one widget', 'g2 examples', 'The glasses keep five news slots and rotate them.'),
           ('Scroll to refresh', 'g2 examples', 'Scrolling past the last article sends cmd 11.')]


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--item', action='append', default=[], metavar='"TITLE|SOURCE|TEXT"',
                   help='one article (repeatable)')
    p.add_argument('--hold', type=float, default=15.0, help='seconds to stay connected after the push')
    return p


def parse_item(spec):
    title, source, content = (spec.split('|', 2) + ['', ''])[:3]
    if not title.strip():
        raise ValueError(f'article needs a title: {spec!r}')
    return title.strip(), source.strip(), content.strip()


def limited(article):
    title, source, content = article
    return (dashboard.limit_utf8(title, TITLE_MAX), dashboard.limit_utf8(source, SOURCE_MAX),
            dashboard.limit_utf8(content, CONTENT_MAX))


def local_ms(ts):
    """Local wall-clock milliseconds: UTC ms + the UTC offset, what the app sends."""
    return int((ts + time.localtime(ts).tm_gmtoff) * 1000)


async def push_news(s, articles, now):
    """The V3 sequence. Returns (slot count, list of per-slot newsStatus or None)."""
    magic = s.next_magic()
    raw = await s.request(dashboard.SID_DASHBOARD, dashboard.build_news_reset(magic), magic, 'news reset')
    c.log.info('news reset → %s', 'acked' if raw is not None else 'no ack')
    await asyncio.sleep(0.5)
    magic = s.next_magic()
    raw = await s.request(dashboard.SID_DASHBOARD, dashboard.build_news_fifo_count_request_v3(magic),
                          magic, 'news fifo count')
    count = dashboard.news_fifo_count(raw)
    c.log.info('news FIFO slots: %d', count)
    slots = (articles * (count // len(articles) + 1))[:count]
    report_time, statuses = local_ms(now), []
    for idx, (title, source, content) in enumerate(slots):
        magic = s.next_magic()
        pb = dashboard.build_news_push_v3(magic, news_id=idx, title=title, report_time=report_time, source=source,
                                          content=content, session_total_count=count, session_news_index=idx)
        st = dashboard.news_status(await s.request(dashboard.SID_DASHBOARD, pb, magic, f'news {idx}', 5.0))
        c.log.info('news %d %r → %s', idx, title[:40], 'no ack' if st is None else f'newsStatus={st}')
        statuses.append(st)
        await asyncio.sleep(0.15)
    return count, statuses


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    c.banner('News', STATUS)
    try:
        articles = [limited(parse_item(i)) for i in args.item] if args.item else [limited(a) for a in SAMPLES]
    except ValueError as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    async with c.glasses(args) as s:
        await c.prepare_dashboard(s)
        count, statuses = await push_news(s, articles, c.now())
        c.show_native_dashboard(s)
        await c.hold(args.hold, 'so the dashboard can take over')
    ok = all(st == 0 for st in statuses)
    print(f'{count} slots filled: ' + ('all stored' if ok else f'statuses {statuses} (non-zero = resend)'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
