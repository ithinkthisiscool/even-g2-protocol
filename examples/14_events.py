"""Print what the glasses report by themselves: gestures, wear, case, battery, apps, Even AI.

Listens for --seconds and decodes every frame the glasses send:
  SID 0x09  settings pushes: battery, charging, display awake, brightness ...   confirmed live
  0x0d      app sync: which app is in the foreground                           confirmed live
  0x07      Even AI wake-ups ("Hey Even"; not answered here, see 10_even_ai)   confirmed live
  0xE0      EvenHub events: clicks / long presses on our page, IMU (--page)    clicks + IMU confirmed
                                                                               live; long press not
  0x10      wear status (taking the glasses off / on)                          experimental
  0x81      case: lid, in case, case battery                                   experimental
  0x01/0x0c dashboard news and quicklist events                                experimental
Everything else is printed as raw hex, which is how new events get reverse-engineered.

EvenHub gestures only arrive while one of our pages is on screen: --page draws a full-screen
text page (it covers the native dashboard until the script ends). --imu also streams the motion
sensor (implies --page).
"""
import asyncio
import sys

import _common as c
from g2 import dashboard, device, even_ai as ai, evenhub as eh, quicklist as ql, settings as gset, sids

STATUS = (f'{c.CONFIRMED_LIVE} settings pushes, app sync, Even AI wake, EvenHub clicks and IMU; '
          f'{c.EXPERIMENTAL} wear/case pushes, long presses and widget events (docs/visual_checks.md #5, #14)')


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--seconds', type=float, default=120.0, help='how long to listen (default 120)')
    p.add_argument('--page', action='store_true', help='draw an EvenHub page so gestures are reported')
    p.add_argument('--imu', action='store_true', help='stream IMU (motion) events (implies --page)')
    p.add_argument('--raw', action='store_true', help='also print the hex of every frame')
    return p


def describe(sid, pb):
    """One readable line for a frame from the glasses, or None to skip it (acks, heartbeats)."""
    cmd = eh.read_varint_field(pb, 1, -1)
    if sid == gset.SID:
        msg = gset.decode(pb)
        return f'settings {msg["label"]}' if msg['push'] or msg['event'] else None
    if sid == sids.APP_SYNC:
        sub = eh.read_bytes_field(pb, 3)
        fg = eh.read_varint_field(sub, 2, 0) if sub is not None else 0
        bg = eh.read_varint_field(sub, 1, 0) if sub is not None else 0
        return f'app sync: foreground={sids.name(fg) if fg else "none"} background={sids.name(bg) if bg else "none"}'
    if sid == ai.SID:
        msg = ai.decode(pb)
        return None if msg['cmd'] == ai.CMD_HEARTBEAT else f'Even AI {msg["label"]}'
    if sid == eh.SID_EVENHUB:
        ev = eh.decode_device_event(pb)
        if ev is None:
            return None
        if ev.get('imu'):
            i = ev['imu']
            return f'IMU x={i["x"]:+.2f} y={i["y"]:+.2f} z={i["z"]:+.2f}'
        where = ev.get('container_name') or ev.get('item_name') or ''
        return f'EvenHub {ev["type"]} {ev.get("event_name", "")} {where}'.rstrip()
    if sid == device.SID_ONBOARDING:
        return f'onboarding {device.decode_onboarding(pb)["label"]}'
    if sid == device.SID_CASE:
        return f'case {device.decode_case(pb)}'
    if sid == device.SID:
        msg = device.decode(pb)
        return None if msg['cmd'] == device.CMD_KEEPALIVE else f'device {msg["label"]}'
    if sid == dashboard.SID_DASHBOARD:
        if cmd == dashboard.CMD_DEV_REQUEST_NEWS_UPGRADE:
            return 'dashboard: news widget asks for fresh news (scrolled past the end)'
        if cmd == dashboard.CMD_DEV_NOTIFY_NEWS_EVENT:
            ev = eh.read_bytes_field(pb, 14) or b''
            kinds = {0: 'expand list', 1: 'back to widget', 2: 'scroll', 3: 'open article', 4: 'back to list'}
            return f'dashboard news: {kinds.get(eh.read_varint_field(ev, 1, 0), "?")} (newsId {eh.read_varint_field(ev, 2, 0)})'
        return None
    if sid == ql.SID_QUICKLIST:
        ev = ql.decode_event(pb)
        if ev is not None:
            return f'quicklist paging {"up" if ev[0] == ql.EVENT_UPDATE_DATA_UP else "down"} from uid {ev[1]}'
        ticks = ql.decode_status_updates(pb)
        return f'quicklist ticks {ticks}' if ticks else None
    return f'{sids.name(sid)} (0x{sid:02x}) cmd={cmd} {pb.hex()}'


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    page = args.page or args.imu
    c.banner('Events', STATUS, f'listening {args.seconds:.0f} s' + (' with an EvenHub page' if page else ''))
    seen = []

    def on_frame(sid, flag, pb):
        if args.raw:
            print(f'  raw {sids.name(sid)} flag=0x{flag:02x} {pb.hex()}', flush=True)
        line = describe(sid, pb)
        if line:
            seen.append(line)
            print(f'  {line}', flush=True)

    async with c.glasses(args, page=page, page_text='Tap, double-tap or long-press the touch bar.') as s:
        c.listen(s, on_frame)
        if args.imu:
            await s.enable_imu(True)
        try:
            await asyncio.sleep(args.seconds)
        finally:
            if args.imu:
                await s.enable_imu(False)
    print(f'{len(seen)} event(s).')
    return 0


if __name__ == '__main__':
    sys.exit(c.run(main))
