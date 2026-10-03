import json
import sys

import _common as c
from g2 import ble

STATUS = c.CONFIRMED_LIVE + ' (the hub discovers pairs this way)'


def build_parser():
    p = c.parser(__doc__, STATUS, glasses=False)
    p.add_argument('--timeout', type=float, default=8.0, help='scan seconds (default 8)')
    p.add_argument('--json', action='store_true', help='print the pairs as JSON')
    p.add_argument('--quiet', action='store_true', help='no live updates while scanning')
    return p


def _rssi(v):
    return f'{v} dBm' if v is not None else '-'


def format_pair(p):
    lines = [f"Pair {p['pair_id']}  serial={p.get('serial') or '?'}  "
             f"{'complete' if p.get('complete') else 'INCOMPLETE'}"]
    for side in ('right', 'left'):
        if not p.get(f'{side}_mac'):
            lines.append(f'  {side:5}  not seen')
            continue
        flags = [k for k in ('seen', 'bonded', 'trusted', 'connected') if p.get(f'{side}_{k}')]
        lines.append(f"  {side:5}  {p[f'{side}_mac']}  {p.get(f'{side}_name') or '':24}  "
                     f"RSSI {_rssi(p.get(f'{side}_rssi')):8}  {', '.join(flags) or '-'}")
    return '\n'.join(lines)


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    if not args.json:
        c.banner('Discover G2 pairs', STATUS, f'scanning {args.timeout:.0f} s')

    def update(p):
        if not (args.quiet or args.json):
            side = 'R' if p.get('right_seen') else 'L'
            print(f"  ... pair {p['pair_id']}: R {p.get('right_mac') or '-'} ({_rssi(p.get('right_rssi'))})  "
                  f"L {p.get('left_mac') or '-'} ({_rssi(p.get('left_rssi'))})  [{side} update]", flush=True)

    pairs = await ble.discover_pairs(timeout=args.timeout, on_update=update)
    if args.json:
        print(json.dumps(pairs, indent=2))
        return 0
    print()
    if not pairs:
        print('No G2 lenses found. Are the glasses out of the case and switched on?')
        return 1
    for p in pairs:
        print(format_pair(p))
    best = next((p for p in pairs if p.get('complete')), None)
    if best:
        print(f"\nexport G2_RIGHT_MAC={best['right_mac']} G2_LEFT_MAC={best['left_mac']}")
    return 0


if __name__ == '__main__':
    sys.exit(c.run(main))
