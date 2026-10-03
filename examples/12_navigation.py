"""Show turn-by-turn steps in the glasses' Navigation app (SID 0x08). You do the routing.

    us      → START                         opens the navigation screen (echoed)
    us      → BASIC_INFO per step           arrow, "200 m", road, time/distance left, ETA, speed
                                            (no ack by design)
    us      → HEARTBEAT every 5 s           the app gives up after 30 s without one
    us      → ARRIVE (--arrive), then STOP  closes it (echoed; the glasses answer NOTIFY_EXIT)

Steps are given as  --step "DIRECTION|DISTANCE|ROAD"  (repeatable), e.g.
--step "turn right|200 m|Main St" --step "straight|1.2 km|Oak Ave". DIRECTION is a name from
g2.navigation.DIRECTION ("turn right", "turn slight left", "uturn", "roundabout exit cw", ...),
the short forms "right"/"left", or the arrow number. Text fields are cut to 60 bytes; empty
ones show as "--". Map images are not supported.
"""
import asyncio
import sys

import _common as c
from g2 import navigation as nav

STATUS = (c.ACK_CONFIRMED + ' (START and STOP echoed live 2026-10-01; BASIC_INFO has no ack by design; '
          'how it looks on the glass is not verified: docs/visual_checks.md #7)')
DEFAULT_STEPS = ['depart|50 m|Station Rd', 'turn right|200 m|Main St', 'turn slight left|1.2 km|Oak Ave']


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--step', action='append', default=[], metavar='"DIR|DIST|ROAD"', help='one step (repeatable)')
    p.add_argument('--interval', type=float, default=8.0, help='seconds between steps (default 8)')
    p.add_argument('--remaining', default='12 min', help='time remaining text')
    p.add_argument('--distance-left', default='2.1 km', help='distance remaining text')
    p.add_argument('--eta', default='14:35', help='arrival time text ("ETA: " is added)')
    p.add_argument('--speed', default='5 km/h', help='speed text')
    p.add_argument('--cycling', action='store_true', help='cycling instead of walking')
    p.add_argument('--arrive', action='store_true', help='send ARRIVE after the last step')
    return p


def parse_step(spec):
    direction, distance, road = (spec.split('|', 2) + ['', ''])[:3]
    d = direction.strip()
    nav.direction_index(int(d) if d.isdigit() else d)            # raises KeyError if unknown
    return (int(d) if d.isdigit() else d), distance.strip(), road.strip()


def on_frame(sid, flag, pb):
    if sid == nav.SID:
        print(f'  glasses: {nav.decode(pb)["label"]}', flush=True)


async def heartbeat(s):
    while True:
        await asyncio.sleep(nav.HEARTBEAT_INTERVAL_S)
        await s.send(nav.SID, nav.build_heartbeat(s.next_magic()))


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    try:
        steps = [parse_step(x) for x in (args.step or DEFAULT_STEPS)]
    except (KeyError, ValueError) as e:
        print(f'Error: unknown direction {e} (see g2.navigation.DIRECTION)', file=sys.stderr)
        return 2
    c.banner('Navigation', STATUS, f'{len(steps)} step(s)')
    method = nav.METHOD_CYCLING if args.cycling else nav.METHOD_WALKING
    async with c.glasses(args) as s:
        c.listen(s, on_frame)
        magic = s.next_magic()
        started = await s.request(nav.SID, nav.build_start(magic), magic, 'navigation start') is not None
        print(f'START → {"echoed" if started else "no echo"}')
        hb = asyncio.ensure_future(heartbeat(s))
        try:
            for n, (direction, distance, road) in enumerate(steps, 1):
                print(f'Step {n}: {direction} {distance} {road}', flush=True)
                await s.send(nav.SID, nav.build_basic_info(
                    s.next_magic(), direction, distance=distance, road=road, remaining_time=args.remaining,
                    remaining_distance=args.distance_left, eta=args.eta, speed=args.speed, method=method, counter=n))
                await asyncio.sleep(args.interval)
            if args.arrive:
                await s.send(nav.SID, nav.build_arrive(s.next_magic()))
                await asyncio.sleep(min(args.interval, 3.0))
        finally:
            hb.cancel()
            magic = s.next_magic()
            stopped = await s.request(nav.SID, nav.build_stop(magic), magic, 'navigation stop') is not None
            print(f'STOP → {"echoed" if stopped else "no echo"}')
    return 0 if started and stopped else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
