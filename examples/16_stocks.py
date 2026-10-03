"""Push quotes to the stock card of the glasses' native dashboard (SID 0x01).

Two sources:
  manual (default)  --quote AAPL:190.5:1.25 --quote MSFT:420:-0.4   (code:price:change%)
  --live AAPL MSFT  current quotes from Yahoo Finance's public chart data (no key; needs internet)

Wire facts (g2/stocks.py):
  * ONE stock per message, tagged stockNum (0-based) of stockTotal; wait for each ack;
  * prices are protobuf FLOATs; marketCap / marketValue would be DOUBLEs (not sent here);
  * the card only shows if WIDGET_STOCK (2) is in the dashboard's widget order, which the
    dashboard init sent first already includes ([NEWS, SCHEDULE, STOCK, STOCK]).
Not yet seen on real glasses: after the push, open the dashboard and check the stock card
(docs/visual_checks.md #10).
"""
import sys

import _common as c
from g2 import stocks

STATUS = c.EXPERIMENTAL + ' (bytes match the decoded spec; never sent to glasses)'


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--quote', action='append', default=[], metavar='CODE:PRICE:CHANGE%',
                   help='a manual quote, repeatable (default AAPL:190.5:1.25)')
    p.add_argument('--live', nargs='+', metavar='SYMBOL', help='fetch these symbols live instead')
    p.add_argument('--hold', type=float, default=10.0, help='seconds to stay connected after the push')
    return p


def parse_manual(items):
    quotes = []
    for item in items or ['AAPL:190.5:1.25']:
        try:
            code, price, change = item.split(':')
            change = float(change)
            quotes.append({'code': code.upper(), 'market_suffix': 'US', 'price': float(price),
                           'change_percent': change,
                           'trend': stocks.TREND_UP if change > 0 else stocks.TREND_DOWN if change < 0
                           else stocks.TREND_FLAT})
        except ValueError:
            raise SystemExit(f'bad --quote {item!r}: use CODE:PRICE:CHANGE%, e.g. AAPL:190.5:1.25')
    return quotes


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    c.banner('Stocks', STATUS)
    if args.live:
        from hub import stock_quotes
        quotes, errors = await stock_quotes.fetch_quotes(stock_quotes.parse_symbols(' '.join(args.live)))
        for e in errors:
            print('quote error:', e)
        if not quotes:
            return 1
    else:
        quotes = parse_manual(args.quote)
    for q in quotes:
        print(f"  {q['code']:6} {q['price']:10.2f}  {q['change_percent']:+.2f}%  {q.get('name', '')}")
    flags = []
    async with c.glasses(args) as s:
        await c.prepare_dashboard(s)
        for i, q in enumerate(quotes):
            flags.append(await c.dashboard_push(
                s, lambda m, q=q, i=i: stocks.build_stock_push(m, q, len(quotes), i), f'stock {q["code"]}'))
        c.show_native_dashboard(s)
        await c.hold(args.hold, 'so the dashboard can take over')
    ok = all(f == 0 for f in flags)
    print('OK: all acked' if ok else f'FAILED: flags {flags}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
