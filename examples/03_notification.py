"""Send a phone-style notification (title, message, app name) to the glasses.

Two parts:
  1. SID 0x04 notification settings: notifications on, auto-display on, app whitelist off. The
     glasses forget these on reboot, so the hub sends them on every connect (the connect
     sequence here does too; this script sends them again explicitly).
  2. The notification itself: a small JSON "file" (type 1) sent over the file service
     (SIDs 0xC4/0xC5 on characteristic 7401): START, DATA blocks, RESULT_CHECK.

Note: the notification is added to the glasses' notification list and cannot be removed again
from here.
"""
import asyncio
import sys

import _common as c
from g2 import file_service as fs

STATUS = (f'{c.ACK_CONFIRMED} settings (SID 0x04 acked on every hub connect); '
          f'{c.EXPERIMENTAL} delivery (the JSON file was reported working by the user, '
          'not yet re-checked with logs; docs/visual_checks.md #9)')


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--title', default='Hello from Python', help='notification title (max 64 chars)')
    p.add_argument('--message', default='Sent with the g2 library.', help='body text (max 512 chars)')
    p.add_argument('--subtitle', default='', help='optional subtitle (max 64 chars)')
    p.add_argument('--app-name', default='Even Realities', help='display name of the sending app (max 32)')
    p.add_argument('--display-seconds', type=int, default=5, help='auto-display time (default 5)')
    return p


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    c.banner('Notification', STATUS)
    body = fs.notification_json(args.title[:64], args.message[:512], subtitle=args.subtitle[:64],
                                display_name=args.app_name[:32], now=c.now())
    async with c.glasses(args) as s:
        await s.send(fs.SID_NOTIFICATION, fs.build_notification_ctrl(s.next_magic(), disp_time=args.display_seconds))
        await asyncio.sleep(0.4)
        await s.send(fs.SID_NOTIFICATION, fs.build_whitelist_disable(s.next_magic()))
        await asyncio.sleep(0.4)
        print(f'Sending {len(body)} bytes of notification JSON...')
        ok, result = await fs.send_file(s, fs.FILE_TYPE_NOTIFICATION, body,
                                        log=lambda msg, good: c.log.info('%s', msg))
    print(f'{"OK" if ok else "FAILED"}: {result}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
