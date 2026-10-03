"""Push weather to the status bar of the glasses' native dashboard (SID 0x01).

Two sources:
  manual (default)  --temp 72 --unit F --type 1 --status Sunny ...
  --live            current conditions from Open-Meteo (free, no API key) for --lat/--lon or
                    --city; needs internet.

Wire facts that matter (docs/g2-library.md 4.5):
  * the temperature is a protobuf FLOAT (wire type 5, 4 bytes), never a double: with a double the
    glasses silently drop the whole message;
  * it is always CELSIUS on the wire; --unit only changes how the glasses display it (and how
    --temp is read here). Whole degrees are sent unless --keep-decimals;
  * update_time is in milliseconds; strings are cut to 32 characters.
After the push the EvenHub heartbeat is stopped so the native dashboard shows the new weather.
"""
import asyncio
import json
import sys
import urllib.parse
import urllib.request

import _common as c
from g2 import dashboard

STATUS = c.ACK_CONFIRMED + ' (acked flag 0 live; visible per user report 09-29)'

WEATHER_TYPES = {0: 'Unknown', 1: 'Sunny', 2: 'Cloudy', 3: 'Drizzle', 4: 'Heavy drizzle', 5: 'Rain',
                 6: 'Heavy rain', 7: 'Thunderstorm', 8: 'Thunder', 9: 'Snow', 10: 'Mist', 11: 'Fog',
                 12: 'Dust', 13: 'Squalls', 14: 'Tornado', 15: 'Freezing rain', 16: 'Clear night'}
# WMO weather code (Open-Meteo) → glasses WeatherType
WMO = {0: 1, 1: 1, 2: 2, 3: 2, 45: 11, 48: 11, 51: 3, 53: 3, 55: 4, 56: 15, 57: 15, 61: 5, 63: 5,
       65: 6, 66: 15, 67: 15, 71: 9, 73: 9, 75: 9, 77: 9, 80: 5, 81: 5, 82: 6, 85: 9, 86: 9,
       95: 7, 96: 7, 99: 7}


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--temp', type=float, default=21.0, help='temperature in --unit (default 21)')
    p.add_argument('--unit', choices=('C', 'F'), default='C', help='input and display unit (default C)')
    p.add_argument('--keep-decimals', action='store_true', help='send e.g. 21.5 instead of rounding')
    p.add_argument('--type', type=int, default=1, choices=sorted(WEATHER_TYPES),
                   help='weather icon: ' + ', '.join(f'{k} {v}' for k, v in WEATHER_TYPES.items()))
    p.add_argument('--status', default=None, help='condition text (default: the icon name)')
    p.add_argument('--rain-icon', type=int, default=0, help='rain expected flag (0/1)')
    p.add_argument('--rain-hint', default='', help='rain hint, e.g. "Now", "3h", "Tmr", "0%%"')
    p.add_argument('--sunset-select', type=int, default=1, help='0 = sunrise next, 1 = sunset next')
    p.add_argument('--sunset-hint', default='', help='sunrise/sunset time, e.g. "18:42"')
    live = p.add_argument_group('live weather (Open-Meteo)')
    live.add_argument('--live', action='store_true', help='fetch current weather instead of --temp/--type')
    live.add_argument('--lat', type=float)
    live.add_argument('--lon', type=float)
    live.add_argument('--city', help='city name (geocoded by Open-Meteo)')
    p.add_argument('--hold', type=float, default=10.0, help='seconds to stay connected after the push')
    return p


def _get_json(url, params):
    with urllib.request.urlopen(f'{url}?{urllib.parse.urlencode(params)}', timeout=15) as r:
        return json.load(r)


def fetch_live(lat, lon, city):
    """Open-Meteo current conditions → (temp_c, weather_type, status). Blocking (run in a thread)."""
    if lat is None or lon is None:
        if not city:
            raise RuntimeError('--live needs --lat/--lon or --city')
        res = _get_json('https://geocoding-api.open-meteo.com/v1/search',
                        {'name': city, 'count': 1}).get('results') or []
        if not res:
            raise RuntimeError(f'city not found: {city}')
        lat, lon = res[0]['latitude'], res[0]['longitude']
    cur = _get_json('https://api.open-meteo.com/v1/forecast',
                    {'latitude': lat, 'longitude': lon, 'current': 'temperature_2m,weather_code,is_day'})['current']
    wtype = WMO.get(cur.get('weather_code'), 0)
    if wtype == 1 and not cur.get('is_day', 1):
        wtype = 16
    return float(cur['temperature_2m']), wtype, WEATHER_TYPES[wtype]


def to_celsius(temp, unit, keep_decimals=False):
    t = (temp - 32) * 5 / 9 if unit == 'F' else temp
    return round(t, 1) if keep_decimals else float(round(t))


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    c.banner('Weather', STATUS)
    if args.live:
        temp_c, wtype, status = await asyncio.to_thread(fetch_live, args.lat, args.lon, args.city)
        temp_c = to_celsius(temp_c, 'C', args.keep_decimals)
    else:
        temp_c = to_celsius(args.temp, args.unit, args.keep_decimals)
        wtype, status = args.type, args.status if args.status is not None else WEATHER_TYPES[args.type]
    unit = dashboard.TEMP_UNIT_FAHRENHEIT if args.unit == 'F' else dashboard.TEMP_UNIT_CELSIUS
    fields = dict(unit=unit, weather_type=wtype, update_time=int(c.now() * 1000), status_string=status[:32],
                  rainfall_icon=args.rain_icon, rainfall_hint=args.rain_hint[:32],
                  sunset_select=args.sunset_select, sunset_hint=args.sunset_hint[:32])
    print(f'Weather: {temp_c} °C on the wire (shown in °{args.unit}), {status!r}, icon {wtype}')
    async with c.glasses(args) as s:
        await c.prepare_dashboard(s)
        flag = await c.dashboard_push(s, lambda m: dashboard.build_weather_push(m, temp_c, **fields), 'weather')
        c.show_native_dashboard(s)
        await c.hold(args.hold, 'so the dashboard can take over')
    print('OK' if flag == 0 else f'FAILED (flag={flag})')
    return 0 if flag == 0 else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
