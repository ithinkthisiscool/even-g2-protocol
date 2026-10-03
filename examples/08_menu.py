"""Set the glasses' app menu (SID 0x03) to a list of built-in apps.

The menu is always replaced as a whole: 5 to 10 items, in the order given. Built-in apps carry
only their app id (itemType 0); the glasses draw name and icon themselves, so adding a name or
an id that is not in the table breaks the item. Apps can be given by id or name:

    08_menu.py 1 7 5 6 4            08_menu.py dashboard "even ai" translate teleprompt notifications

Run without apps (or with --list) to print the table without connecting. There is no command to
read the menu back; the hub re-sends its own saved menu the next time it connects.
"""
import sys

import _common as c
from g2 import menu

STATUS = c.CONFIRMED_LIVE + ' (built-in items)'


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('apps', nargs='*', help='app ids or names, 5 to 10, in menu order')
    p.add_argument('--list', action='store_true', help='print the built-in apps and exit')
    return p


def resolve(apps):
    """['1', 'even ai', ...] → [1, 7, ...]. Raises ValueError with a readable message."""
    by_name = {v.lower(): k for k, v in menu.BUILTIN_MENU_APPS.items()}
    ids = []
    for a in apps:
        key = str(a).strip().lower()
        app_id = int(key) if key.isdigit() else by_name.get(key)
        if app_id not in menu.BUILTIN_MENU_APPS:
            raise ValueError(f'not a built-in menu app: {a!r} (see --list)')
        ids.append(app_id)
    if len(set(ids)) != len(ids):
        raise ValueError('an app appears twice')
    if not menu.MENU_MIN_ITEMS <= len(ids) <= menu.MENU_MAX_ITEMS:
        raise ValueError(f'the menu needs {menu.MENU_MIN_ITEMS} to {menu.MENU_MAX_ITEMS} items, got {len(ids)}')
    return ids


def print_table():
    print('Built-in menu apps (id  name):')
    for app_id, name in menu.BUILTIN_MENU_APPS.items():
        print(f'  {app_id:4}  {name}')


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    if args.list or not args.apps:
        print_table()
        return 0
    try:
        ids = resolve(args.apps)
    except ValueError as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    c.banner('Menu', STATUS, ', '.join(menu.BUILTIN_MENU_APPS[i] for i in ids))
    async with c.glasses(args) as s:
        magic = s.next_magic()
        ack = await s.request(menu.SID_MENU, menu.build_builtin_menu_push(magic, ids), magic, 'menu')
    print('OK: menu replaced' if ack is not None else 'FAILED: no ack')
    return 0 if ack is not None else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
