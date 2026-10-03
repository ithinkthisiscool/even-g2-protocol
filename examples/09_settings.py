"""Change brightness, silent mode and head-up, read the settings back, then restore them.

    09_settings.py                              read and print the settings only
    09_settings.py --brightness 25 --silent on  change, read back, wait --hold s, restore
    ... --keep                                  leave the new values in place

Brightness: the app range is 1-50 (the library enforces it). The firmware stores
clamp(level, 2, 100) rounded down to an even number, so 25 reads back as 24 and 1 as 2. A level
written while auto-brightness is on trains the light-sensor curve, so auto-brightness is turned
off first, as the app does. Restoring writes the old manual level (if auto was off before) or
just switches auto-brightness back on (if it was on).
Only the settings you change are restored. Every write is a SID 0x09 "set" that the glasses
acknowledge by echoing it.
"""
import asyncio
import sys

import _common as c
from g2 import settings as gset

STATUS = f'{c.CONFIRMED_LIVE} read-back; {c.ACK_CONFIRMED} brightness, silent mode and head-up writes'


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--brightness', type=int, metavar='1-50', help='manual brightness level (turns auto off)')
    p.add_argument('--auto-brightness', type=c.on_off, metavar='on|off')
    p.add_argument('--silent', type=c.on_off, metavar='on|off', help='silent mode / do not disturb')
    p.add_argument('--head-up', type=c.on_off, metavar='on|off', help='tilt head up to show the dashboard')
    p.add_argument('--head-up-angle', type=int, metavar='0-60', help='head-up angle in degrees')
    p.add_argument('--keep', action='store_true', help='do not restore the previous values')
    p.add_argument('--hold', type=float, default=5.0, help='seconds to keep the new values before restoring')
    return p


def validate(args):
    if args.brightness is not None and not gset.BRIGHTNESS_MIN <= args.brightness <= gset.BRIGHTNESS_MAX:
        raise ValueError(f'--brightness must be {gset.BRIGHTNESS_MIN}..{gset.BRIGHTNESS_MAX}')
    if args.head_up_angle is not None and not gset.HEAD_UP_ANGLE_MIN <= args.head_up_angle <= gset.HEAD_UP_ANGLE_MAX:
        raise ValueError(f'--head-up-angle must be {gset.HEAD_UP_ANGLE_MIN}..{gset.HEAD_UP_ANGLE_MAX}')
    if args.brightness is not None and args.auto_brightness:
        raise ValueError('--brightness needs auto-brightness off')


def changes_from_args(args):
    """{key: value} for the settings this run changes."""
    ch = {}
    if args.auto_brightness is not None:
        ch['brightness_auto'] = int(args.auto_brightness)
    if args.brightness is not None:
        ch['brightness_auto'], ch['brightness_level'] = 0, args.brightness
    if args.silent is not None:
        ch['silent_mode'] = int(args.silent)
    if args.head_up is not None or args.head_up_angle is not None:
        ch['head_up'] = int(args.head_up) if args.head_up is not None else None
        ch['head_up_angle'] = args.head_up_angle
    return ch


def payloads(magic_fn, target, current):
    """(label, payload) writes that turn `current` into `target` (both read-back style dicts).
    Order matters for brightness: auto off → level → auto on."""
    out = []
    auto, level = target.get('brightness_auto'), target.get('brightness_level')
    if level is not None:
        if current.get('brightness_auto', 0):
            out.append(('auto brightness off', gset.build_auto_brightness(magic_fn(), False)))
        level = max(gset.BRIGHTNESS_MIN, min(gset.BRIGHTNESS_MAX, int(level)))
        out.append((f'brightness {level}', gset.build_brightness(magic_fn(), level)))
        if auto:
            out.append(('auto brightness on', gset.build_auto_brightness(magic_fn(), True)))
    elif auto is not None:
        out.append((f'auto brightness {"on" if auto else "off"}', gset.build_auto_brightness(magic_fn(), bool(auto))))
    if target.get('silent_mode') is not None:
        out.append((f'silent mode {target["silent_mode"]}', gset.build_silent_mode(magic_fn(), bool(target['silent_mode']))))
    if 'head_up' in target:
        enabled = target['head_up'] if target['head_up'] is not None else current.get('head_up', 0)
        angle = target.get('head_up_angle')
        out.append((f'head-up {enabled} angle {angle}', gset.build_head_up(magic_fn(), bool(enabled), angle)))
    return out


async def read_settings(s):
    magic = s.next_magic()
    raw = await s.request(gset.SID, gset.build_read_settings(magic), magic, 'read settings')
    if raw is None:
        raise RuntimeError('the glasses did not answer the settings read')
    return gset.with_defaults(gset.decode(raw)['values'] or {})


async def apply(s, writes):
    ok = True
    for label, pb in writes:
        magic = gset.decode(pb)['magic']
        acked = await s.request(gset.SID, pb, magic, label) is not None
        c.log.info('set %s → %s', label, 'acked' if acked else 'NO ACK')
        ok = ok and acked
        await asyncio.sleep(0.2)
    return ok


def show(title, v):
    print(f"{title}: brightness level {v.get('brightness_level')} (auto {v.get('brightness_auto')}), "
          f"silent {v.get('silent_mode')}, head-up {v.get('head_up')} at {v.get('head_up_angle')}°, "
          f"battery {v.get('battery')}%")


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    try:
        validate(args)
    except ValueError as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    c.banner('Settings', STATUS)
    target = changes_from_args(args)
    async with c.glasses(args) as s:
        before = await read_settings(s)
        show('Before', before)
        if not target:
            return 0
        ok = await apply(s, payloads(s.next_magic, target, before))
        show('After ', await read_settings(s))
        if args.keep:
            print('Keeping the new values (--keep).')
            return 0 if ok else 1
        await c.hold(args.hold, 'with the new values')
        now = await read_settings(s)
        restore = {k: before.get(k) for k in target}
        if before.get('brightness_auto') and 'brightness_level' in restore:
            del restore['brightness_level']      # auto was on: switching it back on is the restore
        ok = await apply(s, payloads(s.next_magic, restore, now)) and ok
        show('Restored', await read_settings(s))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
