"""Connect, run the official app's connect sequence, and print battery, case, worn and firmware.

What happens:
  1. open_session(): find both lenses, connect, authenticate both, prelude, EvenHub heartbeat
     (no custom page, so the native dashboard stays visible).
  2. A listener decodes every reply: settings read-back (SID 0x09), wear status (0x10), case
     battery (0x81), device config acks (0x80).
  3. The connect sequence the app sends after auth: role RIGHT, time sync, settings read, units,
     onboarding FINISH, wear query, case query, gesture list, notifications on.
  4. query_settings(): a direct request/response read of battery, charging and firmware.
  5. Disconnect (also on Ctrl+C or an error).
"""
import asyncio
import sys

import _common as c
from g2 import device, settings as gset

STATUS = c.CONFIRMED_LIVE


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--wait', type=float, default=2.0, help='seconds to collect replies (default 2)')
    return p


def collector(status, log_lines):
    """A listener that folds every status reply into `status` (a dict)."""
    def on_frame(sid, flag, pb):
        if sid == gset.SID:
            msg = gset.decode(pb)
            if msg['values']:
                status.update(gset.with_defaults(msg['values']) if not msg['push'] else msg['values'])
            log_lines.append(f'settings  ← {msg["label"]}')
        elif sid == device.SID_ONBOARDING:
            msg = device.decode_onboarding(pb)
            if msg['worn'] is not None:
                status['worn'] = msg['worn']
            log_lines.append(f'onboarding← {msg["label"]}')
        elif sid == device.SID_CASE:
            case = device.decode_case(pb)
            if case:
                status.update({f'case_{k}': v for k, v in case.items() if k != 'magic' and v is not None})
            log_lines.append(f'case      ← {case}')
        elif sid == device.SID:
            log_lines.append(f'device    ← {device.decode(pb)["label"]}')
    return on_frame


def report(status):
    def g(k, fmt='{}'):
        v = status.get(k)
        return '?' if v is None else fmt.format(v)
    worn = status.get('worn')
    return '\n'.join([
        f"  battery        {g('battery', '{}%')}  (charging: {g('charging')})",
        f"  case battery   {g('case_battery', '{}%')}  lid={g('case_lid')} in_case={g('case_in_case')}",
        f"  worn           {'?' if worn is None else ('yes' if worn else 'no')}",
        f"  firmware       L {g('left_version')} / R {g('right_version')}",
        f"  brightness     level {g('brightness_level')} auto={g('brightness_auto')}",
        f"  head-up        {g('head_up')} angle={g('head_up_angle')}  silent={g('silent_mode')}",
    ])


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    c.banner('Connect and read status', STATUS)
    status, lines = {}, []
    async with c.glasses(args, connect_sequence=False) as s:
        c.listen(s, collector(status, lines))
        await c.run_connect_sequence(s)
        direct = await s.query_settings()          # request/response, matched by magic
        if direct:
            status.update(direct)
        await asyncio.sleep(args.wait)
    for line in lines:
        print('  ' + line)
    print('\nGlasses status:')
    print(report(status))
    return 0 if status.get('battery') is not None else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
