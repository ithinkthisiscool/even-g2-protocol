"""Push today's and tomorrow's events to the calendar widget of the native dashboard (SID 0x01).

Events are given as   --event "HH:MM-HH:MM Title @ Location"   (location optional), prefixed
with "tmr " for tomorrow, or "all-day Title". Without --event two sample events are sent.
--clear sends the empty state instead.

The official app's rules, followed here (docs/status.md, hub/calendar_events.py):
  * only today's and tomorrow's events; timed events that already ended are dropped; earliest
    first; at most 8;
  * ONE event per message: scheduleTotal = n, scheduleNum = 0-based index;
  * time string "HH:mm-HH:mm" (--24h) or "hh:mm AM-hh:mm PM", "Tmr " prefix for tomorrow,
    "All day" for all-day events, an end of 00:00 shown as 24:00;
  * byte limits: title 175, location 282, time 62 (cut with "...");
  * no display setting is attached to calendar content (the base settings are sent once).
"""
import re
import sys
import time

import _common as c
from g2 import dashboard

STATUS = c.CONFIRMED_LIVE
MAX_EVENTS = 8
TITLE_MAX, LOCATION_MAX, TIME_MAX = 175, 282, 62
EVENT_RE = re.compile(r'^(?P<tmr>tmr\s+)?(?:(?P<allday>all-day)|(?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2}))'
                      r'\s+(?P<title>.+?)(?:\s+@\s+(?P<loc>.+))?$', re.IGNORECASE)


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--event', action='append', default=[], metavar='SPEC',
                   help='"[tmr ]HH:MM-HH:MM Title [@ Location]" or "[tmr ]all-day Title"; repeatable')
    p.add_argument('--24h', dest='h24', action='store_true', help='24-hour time strings')
    p.add_argument('--clear', action='store_true', help='send "no upcoming events" instead')
    p.add_argument('--hold', type=float, default=10.0, help='seconds to stay connected after the push')
    return p


def _day_start(ts, days=0):
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days, 0, 0, 0, 0, 0, -1))


def parse_event(spec, now):
    """'tmr 09:00-09:30 Standup @ Room 4' → {title, location, start, end, all_day}."""
    m = EVENT_RE.match(spec.strip())
    if not m:
        raise ValueError(f'cannot parse event {spec!r}')
    day = _day_start(now, 1 if m['tmr'] else 0)
    if m['allday']:
        start, end, all_day = day, _day_start(day, 1), True
    else:
        def at(hhmm):
            h, mi = map(int, hhmm.split(':'))
            return day + h * 3600 + mi * 60
        start, end, all_day = at(m['start']), at(m['end']), False
        if end <= start:
            end += 86400                      # e.g. 23:00-00:00 ends the next day
    return {'title': m['title'], 'location': m['loc'] or '', 'start': int(start), 'end': int(end),
            'all_day': all_day}


def sample_events(now):
    start = int(now // 60 * 60) + 10 * 60
    return [{'title': 'Standup', 'location': 'Room 4', 'start': start, 'end': start + 30 * 60, 'all_day': False},
            parse_event('tmr 09:00-10:00 Dentist @ Main St 12', now)]


def _clock(ts, h24, is_end=False):
    t = time.localtime(ts)
    if h24:
        s = time.strftime('%H:%M', t)
        return '24:00' if is_end and s == '00:00' else s
    return time.strftime('%I:%M %p', t)


def time_string(ev, now, h24=False):
    """The app's display string for an event."""
    prefix = 'Tmr ' if _day_start(ev['start']) == _day_start(now, 1) else ''
    if ev['all_day']:
        return prefix + 'All day'
    return f"{prefix}{_clock(ev['start'], h24)}-{_clock(ev['end'], h24, is_end=True)}"


def upcoming(events, now):
    """Today's and tomorrow's events, not ended, earliest first, at most 8."""
    today, day_after = _day_start(now), _day_start(now, 2)
    keep = [e for e in sorted(events, key=lambda e: e['start'])
            if (today <= e['start'] < day_after or e['start'] <= now < e['end'])
            and (e['all_day'] or e['end'] > now)]
    return keep[:MAX_EVENTS]


def to_schedules(events, now, h24=False):
    """(title, location, time_str, end_ts) tuples for dashboard.build_calendar_event_push."""
    return [(dashboard.limit_utf8(e['title'], TITLE_MAX), dashboard.limit_utf8(e['location'], LOCATION_MAX),
             dashboard.limit_utf8(time_string(e, now, h24), TIME_MAX), int(e['end']))
            for e in upcoming(events, now)]


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    c.banner('Calendar', STATUS)
    now = c.now()
    if args.clear:
        events = []
    else:
        try:
            events = [parse_event(e, now) for e in args.event] if args.event else sample_events(now)
        except ValueError as e:
            print(f'Error: {e}', file=sys.stderr)
            return 2
    schedules = to_schedules(events, now, args.h24)
    for title, loc, ts, _ in schedules:
        print(f'  {ts:24} {title}' + (f' @ {loc}' if loc else ''))
    if events and not schedules:
        print('Nothing to send: every event is in the past or later than tomorrow.')
        return 1
    ok = True
    async with c.glasses(args) as s:
        await c.prepare_dashboard(s)
        # One message per event; for an empty calendar one message with total 0.
        # authority=1 means "calendars are connected, nothing upcoming" (app 2.3.1).
        for i, sched in (list(enumerate(schedules)) or [(0, None)]):
            flag = await c.dashboard_push(
                s, lambda m: dashboard.build_calendar_event_push(m, sched, len(schedules), i,
                                                                 authority=0 if schedules else 1),
                f'calendar {i + 1}/{max(1, len(schedules))}')
            ok = ok and flag == 0
        c.show_native_dashboard(s)
        await c.hold(args.hold, 'so the dashboard can take over')
    print('OK' if ok else 'Some events were not acknowledged.')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
