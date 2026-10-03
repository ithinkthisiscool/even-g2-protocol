"""Native dashboard (SID 0x01): calendar, news, weather and the base-settings packet.

Plain English
-------------
The "dashboard" is the glasses' own home screen (tilt your head up to see it): a clock, a status
bar (weather, unread count, battery) and a carousel of widgets (news, calendar, stocks, tasks...).
The glasses never fetch data themselves; the phone app pushes it. This module builds the
protobuf payloads the official app pushes, so we can fill those widgets ourselves.

Every function here only BUILDS bytes (a protobuf payload). To send one:
    magic = session.next_magic()
    pb = build_calendar_push(magic, [(1, 'Standup', 'Room 4', '', end_unix_s)])
    ack = await session._send_and_get_payload(SID_DASHBOARD, evenhub.FLAG_REQUEST, magic, pb,
                                              session.next_seq(), 'calendar')
The ack is a DashboardDataPackage whose field 3 (DashboardRespondToApp) carries a flag:
0 = DASHBOARD_RECEIVED_SUCCESS, 1 = PARAMETER_ERROR, 2 = NEWS_VERSION_ERROR.

Rules the glasses enforce (all live-confirmed):
  * The EvenHub heartbeat (SID 0xE0) must be running, or SID 0x01 writes are silently dropped.
    Connect with GlassesSession.connect(launch_app=True, skip_page=True).
  * Send build_dashboard_init() once after connecting, before any content.
  * After pushing, cancel the heartbeat (session._hb_task.cancel()) -- the native dashboard then
    takes over within ~5 s and shows the new content.

Status per function
  build_dashboard_init            LIVE-CONFIRMED (base settings: displayMode 4 = news main widget)
  build_calendar_push             LIVE-CONFIRMED
  news V3 flow                    LIVE-CONFIRMED: build_news_reset -> build_news_fifo_count_request_v3
                                  -> build_news_push_v3 x count (report_time in MILLISECONDS,
                                  news_id = slot index 0..4). Scroll-past-end refresh (glasses
                                  send cmd 11) is handled by re-running this flow in the hub.
  build_weather_push              IMPLEMENTED, UNTESTED LIVE (temperature is now a protobuf FLOAT)
  build_news_push (V2)            CONFIRMED NOT TO WORK (kept for reference)
  stock widget                    schema known (below and in esp32_ai/docs/research/unknowns_findings.md
                                  section 5), NOT implemented here

Source of the schema: extracted directly from the official Even Android app's own compiled code,
not guessed. Further detail: esp32_ai/docs/research/unknowns_findings.md sections 1, 2 and 5,
and esp32_ai/docs/research/dashboard_fields.txt.

IMPORTANT: the real transport SID is 0x01, not 0x08. Static analysis (decompiled string search
in an earlier pass) had suggested SID=8 carried "dashboard" traffic, and every live probe this
project ran against SID=8 got zero effect. Real captured traffic from the actual app (HCI snoop
during a live sync, 2026-09-27) showed a `DashboardDataPackage` message -- decoded field-for-field
exactly matching the schema below, including our own injected calendar event's title string --
going out with envelope byte 6 (svc_hi/SID) = 0x01, byte 7 (svc_lo/flag) = 0x20 (matching this
project's own FLAG_REQUEST). The full protobuf schema recovered below was correct all along; only
the SID constant was wrong.

How this was obtained: the app (com.even.sg) is a Flutter/Dart app. Its Dart AOT-compiled code
(lib/arm64-v8a/libapp.so inside the app's split APK) was decompiled with blutter
(https://github.com/worawit/blutter), which reconstructs real Dart source-level structure --
including class names, field names, and (critically) protobuf field numbers -- from the compiled
binary. Field names were NOT stripped/obfuscated in this build, so the recovered schema is exact.

The relevant generated-protobuf file was:
  package:even_connect/g2/proto/generated/dashboard/dashboard.pb.dart
and its companion enum file:
  package:even_connect/g2/proto/generated/dashboard/dashboard.pbenum.dart

Caveat: blutter's disassembly-derived field numbers for the *first* field following a `oneof`
registration (BuilderInfo::oo()) are unreliable -- the disassembly briefly holds an unrelated
"oneof case index" constant in the same register slot my extraction script keyed off, so that
first number was always wrong (e.g. it read 28 for `commandId`, which is actually field 1). Every
number below was cross-checked by reading the raw disassembly for each affected class by hand
(DashboardDataPackage, DashboardReceiveFromApp, DashboardContent, rWidgetComponent) rather than
trusted from the automated pass. Non-oneof classes (Schedule, rScheduleWidget, etc.) do not have
this issue.

Top-level envelope (this is the raw SID=0x01 BLE payload -- a plain protobuf message, no extra
wrapper observed):

  DashboardDataPackage (oneof "package", one of these 16 fields set at a time):
    1  commandId        enum eDashboardCommandId
    2  magicRandom      int32
    3  dashboardRespond  -> DashboardRespondToApp   (device -> app ack)
    4  dashboardReceive  -> DashboardReceiveFromApp  (APP -> DEVICE: push content/settings/request)
    5  appRespond        -> AppRespondToDashboard    (device -> app ack)
    6  appReceive        -> DashboardSendToApp       (DEVICE -> APP: device's own status/sync)
    7-16: news-subsystem request/response messages (unrelated to widget content push)

  eDashboardCommandId enum (real names+values, from the app's own constant pool):
    0 NONE_COMMAND, 1 Dashboard_Respond, 2 Dashboard_Receive, 3 APP_Respond, 4 APP_RECEIVE,
    5 REQUEST_NEWS_INFO, 6 RESPONSE_NEWS_INFO, 7 APP_REQUEST_NEWS_INFO, 8 DEV_RESPONSE_NEWS_INFO,
    9 APP_SEND_NEWS_DATA, 10 DEV_RESPONSE_NEWS_DATA, 11 DEV_REQUEST_NEWS_UPGRADE,
    12 DEV_NOTIFY_NEWS_EVENT, 13 APP_REQUEST_CLEAR_ALL_DATA, 14 DEV_RESPONSE_CLEAR_ALL_DATA
  To push dashboard content: commandId = Dashboard_Receive (2), and set the `dashboardReceive`
  field (#4) of DashboardDataPackage.

  DashboardReceiveFromApp (App -> Device message body):
    1  packageId               int32
    2  bashboardDisplaySetting -> DashboardDisplaySetting  (layout/order settings; [sic] app's
                                                              own typo, kept verbatim)
    3  bashboardConfig         -> DashboardContent          (actual widget content)
    4  appRequest              -> AppRequest

  DashboardContent (oneof):
    1  statusComponents -> rStatusComponent   (weather, power, notifications)
    2  widgetComponents -> rWidgetComponent   (news, stock, schedule/calendar)

  rWidgetComponent (oneof):
    1  news     -> rNewsWidget
    2  stock    -> rStockWidget
    3  schedule -> rScheduleWidget

  rScheduleWidget (real calendar-push structure):
    1  scheduleTotal      int32
    2  scheduleNum        int32
    3  schedule           -> Schedule (repeated)
    4  scheduleAuthority  int32

  Schedule (one calendar event):
    1  scheduleId    int32
    2  title         string
    3  location      string
    4  time          string   (app's own pre-formatted display string, e.g. "14:00-15:00")
    5  endTimestamp  int32 (unix seconds, based on call site usage elsewhere in the app)

  rNewsWidget:
    1  newsTotal         int32  (total count across all batches)
    2  newsNum           int32  (0-based START INDEX of this batch -- confirmed by symmetry with
                                  rScheduleWidget's scheduleNum, which real captured traffic proved
                                  is a start index, not a count; same Total/Num naming convention
                                  used uniformly across all r*Widget types in this schema)
    3  news              -> News (repeated)
    4  newsForceUpgrade  int32

  News (all fields confirmed via direct disassembly read of News::_i(), not automated/guessed):
    1  newsId       int32
    2  title        string
    3  reportTime   int64 (typed as Dart Int64, not plain int -- still varint on the wire)
    4  source       string
    5  content      string

  rStatusComponent (oneof):
    1  weather -> rWeatherStatus

  rWeatherStatus (all fields confirmed via direct disassembly read of rWeatherStatus::_i()):
    1  temperature                    FLOAT (4 bytes, wire type 5). An older version of this
                                      module sent a double (wire type 1) and the glasses dropped
                                      the whole message -- see unknowns_findings.md section 1.
    2  unit                           enum TemperatureUnit (0=UNKNOWN, 1=CELSIUS, 2=FAHRENHEIT)
    3  type                           enum WeatherType (0=UNKNOWN, 1=SUNNY, 2=CLOUDS, 3=DRIZZLE,
                                        4=HEAVY_DRIZZLE, 5=RAIN, 6=HEAVY_RAIN, 7=THUNDERSTORM,
                                        8=THUNDER, 9=SNOW, 10=MIST, 11=FOG, 12=DUST, 13=SQUALLS,
                                        14=TORNADO, 15=FREEZING_RAIN, 16=NIGHT)
    4  updateTime                     int64
    5  weatherStatusString            string (e.g. "Sunny")
    6  rainfallProbabilityIcon        int32
    7  rainfallProbabilityHintString  string (e.g. "30% chance of rain")
    8  sunsetSelect                   int32
    9  sunsetStringHint               string (e.g. "Sunset at 7:45 PM")

  Stock (not implemented as an encoder yet; CORRECT types per unknowns_findings.md section 5:
  marketCap and marketValue are double, the price/percent fields are FLOAT):
    1  stockCode           string
    2  marketSuffix        string
    3  marketCap           double
    4  priceChangePercent  float
    5  currentPrice        float
    6  companyName         string
    7-16: dayHigh, dayLow, openPrice, volume, marketValue, peRatio, changingTrend, pointTotal,
          darkPrice, brightPrice(packed) -- numeric fields, names already correct from the
          automated pass (Stock has no `oneof`, so it wasn't affected by that bug); per-field
          types are listed in unknowns_findings.md section 5 (mostly float, volume uint64).

This module implements calendar (rScheduleWidget/Schedule), news (rNewsWidget/News and the V3
AppSendNewsData path), weather (rStatusComponent/rWeatherStatus) and base-settings
(DashboardDisplaySetting) pushes -- all with a field-verified schema, not guessed. Every field name above was read directly out of the real BuilderInfo registration calls
in dashboard.pb.dart (not the automated extraction pass, which is unreliable for the field
immediately following a `oneof` registration -- see the calendar section above for why).
"""
import sys
import os

from . import evenhub as eh

SID_DASHBOARD = 0x01  # confirmed via real capture, see module docstring -- NOT 0x08

# eDashboardCommandId: field 1 of every DashboardDataPackage. The payload field is cmd + 2.

CMD_NONE = 0
CMD_DASHBOARD_RESPOND = 1
CMD_DASHBOARD_RECEIVE = 2   # app -> device content/settings push
CMD_APP_RESPOND = 3
CMD_APP_RECEIVE = 4
CMD_REQUEST_NEWS_INFO = 5
CMD_RESPONSE_NEWS_INFO = 6
CMD_APP_REQUEST_NEWS_INFO = 7
CMD_DEV_RESPONSE_NEWS_INFO = 8
CMD_APP_SEND_NEWS_DATA = 9
CMD_DEV_RESPONSE_NEWS_DATA = 10
CMD_DEV_REQUEST_NEWS_UPGRADE = 11
CMD_DEV_NOTIFY_NEWS_EVENT = 12
CMD_APP_REQUEST_CLEAR_ALL_DATA = 13
CMD_DEV_RESPONSE_CLEAR_ALL_DATA = 14

# WidgetType enum (real values, from the app's own constant pool)
WIDGET_UNKNOWN = 0
WIDGET_NEWS = 1
WIDGET_STOCK = 2
WIDGET_SCHEDULE = 3
WIDGET_QUICKLIST = 4
WIDGET_HEALTH = 5

# TemperatureUnit enum (real values)
TEMP_UNIT_UNKNOWN = 0
TEMP_UNIT_CELSIUS = 1
TEMP_UNIT_FAHRENHEIT = 2
WEATHER_UNIT_CELSIUS = TEMP_UNIT_CELSIUS  # alias used as a default elsewhere in this module

# WeatherType enum (real values)
WEATHER_UNKNOWN = 0
WEATHER_SUNNY = 1
WEATHER_CLOUDS = 2
WEATHER_DRIZZLE = 3
WEATHER_HEAVY_DRIZZLE = 4
WEATHER_RAIN = 5
WEATHER_HEAVY_RAIN = 6
WEATHER_THUNDERSTORM = 7
WEATHER_THUNDER = 8
WEATHER_SNOW = 9
WEATHER_MIST = 10
WEATHER_FOG = 11
WEATHER_DUST = 12
WEATHER_SQUALLS = 13
WEATHER_TORNADO = 14
WEATHER_FREEZING_RAIN = 15
WEATHER_NIGHT = 16


def encode_packed_varint_field(field_number, values):
    """Packed repeated field (used for enum lists like widgetDisplayOrder): a single
    length-delimited field containing the concatenated varint encoding of each value, with no
    per-value tag."""
    inner = b''.join(eh.encode_varint(v) for v in values)
    return eh.encode_message_field(field_number, inner)


def encode_float_field(field_number, value):
    """protobuf float: wire type 5, 4 bytes little-endian."""
    import struct
    return eh.encode_key(field_number, 5) + struct.pack('<f', float(value))


def encode_double_field(field_number, value):
    """protobuf 'double' field: wire type 1 (fixed64), little-endian IEEE754 -- NOT a varint.
    Only for the stock marketCap / marketValue fields. Weather temperature and the other stock
    fields are protobuf FLOATs; a wrong wire type makes the glasses drop the whole message."""
    import struct
    return eh.encode_key(field_number, 1) + struct.pack('<d', float(value))


def encode_dashboard_display_setting(widget_display_order, status_display_order=None,
                                      display_mode=0, half_day_format=0, temperature_unit=0):
    """DashboardDisplaySetting -- tells the firmware which widgets are enabled/visible and in
    what order. Content pushed for a widget not listed here appears to have nowhere to render
    (this is the real-schema analogue of the old cmd=7 'create page/container' step)."""
    status_display_order = status_display_order or []
    out = eh.encode_varint_field(1, display_mode)
    out += eh.encode_varint_field(2, len(status_display_order))
    if status_display_order:
        out += encode_packed_varint_field(3, status_display_order)
    out += eh.encode_varint_field(4, len(widget_display_order))
    if widget_display_order:
        out += encode_packed_varint_field(5, widget_display_order)
    out += eh.encode_varint_field(6, half_day_format)
    out += eh.encode_varint_field(7, temperature_unit)
    return out


def encode_schedule(schedule_id, title, location, time_str, end_timestamp):
    """Schedule message (one calendar event). end_timestamp is Unix SECONDS; time_str is a
    free display string such as "14:00-15:00" (may be empty)."""
    out = eh.encode_varint_field(1, schedule_id)
    if title:
        out += eh.encode_string_field(2, title)
    if location:
        out += eh.encode_string_field(3, location)
    if time_str:
        out += eh.encode_string_field(4, time_str)
    out += eh.encode_varint_field(5, end_timestamp)
    return out


def encode_schedule_widget(schedules, schedule_total=None, start_index=0, schedule_authority=0):
    """rScheduleWidget wrapping a list of Schedule messages. Real captured traffic (a single,
    complete batch of one event) showed scheduleTotal=1, scheduleNum=0 -- i.e. field 2 is a
    0-based START INDEX for this batch, not a repeated count (this supports paginated multi-batch
    sends for many events). An earlier version of this encoder wrongly set both fields to
    len(schedules), which likely looked like "this is entry #2 of a bigger batch" to the firmware
    and got silently dropped as incomplete/out-of-order."""
    total = schedule_total if schedule_total is not None else len(schedules)
    out = eh.encode_varint_field(1, total)
    out += eh.encode_varint_field(2, start_index)
    for s in schedules:
        out += eh.encode_message_field(3, s)
    out += eh.encode_varint_field(4, schedule_authority)
    return out


def encode_widget_component_schedule(schedule_widget_bytes):
    """rWidgetComponent{schedule: rScheduleWidget}."""
    return eh.encode_message_field(3, schedule_widget_bytes)


def encode_dashboard_content_widget(widget_component_bytes):
    """DashboardContent{widgetComponents: rWidgetComponent}."""
    return eh.encode_message_field(2, widget_component_bytes)


def encode_dashboard_receive_from_app(package_id, dashboard_content_bytes, display_setting_bytes=None):
    """DashboardReceiveFromApp{packageId, bashboardDisplaySetting, bashboardConfig}."""
    out = eh.encode_varint_field(1, package_id) if package_id is not None else b''
    if display_setting_bytes is not None:
        out += eh.encode_message_field(2, display_setting_bytes)
    out += eh.encode_message_field(3, dashboard_content_bytes)
    return out


def encode_dashboard_data_package(command_id, magic_random, dashboard_receive_bytes=None):
    """Top-level DashboardDataPackage envelope -- this IS the raw BLE payload on SID=0x01."""
    out = eh.encode_varint_field(1, command_id)
    out += eh.encode_varint_field(2, magic_random)
    if dashboard_receive_bytes is not None:
        out += eh.encode_message_field(4, dashboard_receive_bytes)
    return out


def encode_news(news_id, title, report_time, source, content):
    """News message for the (non-working) V2 news path. See encode_app_send_news_data for V3."""
    out = eh.encode_varint_field(1, news_id)
    if title:
        out += eh.encode_string_field(2, title)
    out += eh.encode_varint_field(3, report_time)
    if source:
        out += eh.encode_string_field(4, source)
    if content:
        out += eh.encode_string_field(5, content)
    return out


def encode_news_widget(news_items, news_total=None, start_index=0, force_upgrade=0):
    """rNewsWidget wrapping a list of News messages. newsNum is a 0-based start index, by
    symmetry with the empirically-confirmed rScheduleWidget.scheduleNum (see module docstring)."""
    total = news_total if news_total is not None else len(news_items)
    out = eh.encode_varint_field(1, total)
    out += eh.encode_varint_field(2, start_index)
    for n in news_items:
        out += eh.encode_message_field(3, n)
    out += eh.encode_varint_field(4, force_upgrade)
    return out


def encode_widget_component_news(news_widget_bytes):
    """rWidgetComponent{news: rNewsWidget}."""
    return eh.encode_message_field(1, news_widget_bytes)


def encode_weather_status(temperature, unit=WEATHER_UNIT_CELSIUS, weather_type=WEATHER_UNKNOWN,
                           update_time=0, status_string='', rainfall_icon=0, rainfall_hint='',
                           sunset_select=0, sunset_hint=''):
    """rWeatherStatus message. temperature is a protobuf FLOAT (blutter: BuilderInfo type 0x100,
    setter calls $_setFloat) and is always Celsius -- `unit` only tells the glasses how to display
    it. update_time is epoch MILLISECONDS. Strings are capped at 32 chars like the app."""
    out = encode_float_field(1, temperature)
    out += eh.encode_varint_field(2, unit)
    out += eh.encode_varint_field(3, weather_type)
    out += eh.encode_varint_field(4, update_time)
    if status_string:
        out += eh.encode_string_field(5, status_string[:32])
    out += eh.encode_varint_field(6, rainfall_icon)
    if rainfall_hint:
        out += eh.encode_string_field(7, rainfall_hint[:32])
    out += eh.encode_varint_field(8, sunset_select)
    if sunset_hint:
        out += eh.encode_string_field(9, sunset_hint[:32])
    return out


def encode_status_component_weather(weather_status_bytes):
    """rStatusComponent{weather: rWeatherStatus}."""
    return eh.encode_message_field(1, weather_status_bytes)


def encode_dashboard_content_status(status_component_bytes):
    """DashboardContent{statusComponents: rStatusComponent}."""
    return eh.encode_message_field(1, status_component_bytes)


def build_news_push(magic_random, news_items, package_id=1, set_display_order=True):
    """Full top-level payload to push one or more news items to the native dashboard via the
    simple rNewsWidget content-push path.

    CONFIRMED NOT TO WORK LIVE: every variant tried gets DASHBOARD_PARAMETER_ERROR regardless of
    field values. Real disassembly of the actual send chain (dashboard_pro_helper.dart's
    sendNewsContentToGlassV3, called by news_service.dart's real production path) shows the real
    app does NOT go through dashboardReceive/DashboardContent/rWidgetComponent for news at all --
    it builds an AppSendNewsData message and sets it directly as DashboardDataPackage's own field
    11 ("SendNewsData"), with commandId=APP_SEND_NEWS_DATA(9), bypassing this whole nesting chain.
    Kept for reference/comparison; use build_news_push_v3 instead.

    news_items: list of (news_id, title, report_time_unix_seconds, source, content)
    """
    news_msgs = [encode_news(*n) for n in news_items]
    news_widget = encode_news_widget(news_msgs)
    widget_component = encode_widget_component_news(news_widget)
    content = encode_dashboard_content_widget(widget_component)
    display_setting = encode_dashboard_display_setting([WIDGET_NEWS]) if set_display_order else None
    receive = encode_dashboard_receive_from_app(package_id, content, display_setting)
    return encode_dashboard_data_package(CMD_DASHBOARD_RECEIVE, magic_random, receive)


def build_news_probe(magic_random, all_count, report_time, package_id=1):
    """Reproduces DashboardProHelper.probeNewsSendToGlass EXACTLY, field-for-field, confirmed via
    direct disassembly read (2026-09-27): this is a real, mandatory step the app always runs
    BEFORE sending any actual news content, to detect which protocol version (V2's plain
    rNewsWidget push, kept in build_news_push above, vs V3's AppSendNewsData, build_news_push_v3)
    this specific firmware/device wants -- it is NOT a guess like build_news_push was.

    Real shape: rNewsWidget{itemTotalNum: all_count, newsNum: 0, news: [News{reportTime: X}]} --
    note only reportTime is set on the News item, no title/source/content at all -- sent through
    the same DashboardReceiveFromApp/DashboardContent/rWidgetComponent nesting as build_news_push,
    but with NO DashboardDisplaySetting attached (the real probe function never touches it).

    The response's flag tells the app which path to use next:
      0 (DASHBOARD_RECEIVED_SUCCESS)     -> stick with this same V2 rNewsWidget push for content
      2 (DASHBOARD_NEWS_VERSION_ERROR)   -> switch to V3 (AppSendNewsData) for content
      1 (DASHBOARD_PARAMETER_ERROR)      -> what build_news_push's full-content variants got
                                             every time; unclear if the real app treats this the
                                             same as success (falls through to the same code path
                                             in the disassembly) or as a real failure -- this
                                             probe call is what will observe the real answer.
    """
    news_item = eh.encode_varint_field(3, report_time)  # ONLY reportTime -- matches probe exactly
    news_widget = eh.encode_varint_field(1, all_count) + eh.encode_varint_field(2, 0) + eh.encode_message_field(3, news_item)
    widget_component = encode_widget_component_news(news_widget)
    content = encode_dashboard_content_widget(widget_component)
    receive = encode_dashboard_receive_from_app(package_id, content, None)
    return encode_dashboard_data_package(CMD_DASHBOARD_RECEIVE, magic_random, receive)


def build_news_fifo_count_request(magic_random, cmd=1):
    """Reproduces DashboardProHelper.needSendNewsCount / ProtoDashboardExt.sendNeedSendNewsCount:
    the app asks the device how many news slots it wants filled BEFORE ever pushing content, via
    RequestNewsFifoCountCmd{cmd} at DashboardDataPackage field 7, commandId=REQUEST_NEWS_INFO(5).
    The response (ResponseNewsFifoMsg{count}, field 8, commandId=RESPONSE_NEWS_INFO(6)) tells the
    app how many items to actually send. Never attempted live before this -- every previous
    AppSendNewsData push skipped straight to content with no preceding negotiation. `cmd`'s real
    meaning is unconfirmed (RequestNewsFifoCountCmd's only field, presumably some request-type
    selector); the real call site passes a literal 1.
    """
    request = eh.encode_varint_field(1, cmd)
    out = eh.encode_varint_field(1, CMD_REQUEST_NEWS_INFO)
    out += eh.encode_varint_field(2, magic_random)
    out += eh.encode_message_field(7, request)
    return out


def encode_app_send_news_data(session_id, session_status, session_total_count, session_news_index,
                               news_id, title, report_time, source, content):
    """AppSendNewsData -- the REAL message the app sends for news (confirmed via direct
    disassembly of DashboardProHelper.sendNewsContentToGlassV3, the actual production send path,
    as opposed to the older sendNewsContentToGlass/build_news_push above which the firmware
    rejects). Session field semantics PARTIALLY resolved from sendEmptyNewsToGlassV3 (2026-09-27):
    that function uses $_setUnsignedInt32 DIRECTLY (not via dedup), revealing:
      internal index 1 (field 2) = session_status = 1 in the empty/force-clear path
      internal index 2 (field 3) = session_total_count = 1 for the single-item case
      internal index 3 (field 4) = session_news_index = 0

    sendNewsContentToGlassV3 dedup setter analysis (corrected 2026-09-28 full-loop analysis):
      distanceUnit= uses dedup index=1 → internal index 1 → field 2 (sessionStatus)
        value: (allCount > 0 ? 0 : 1)  where allCount = FIFO count (constant for all items in batch)
      timeFormat= uses dedup index=2 → internal index 2 → field 3 (sessionTotalCount)
        value: allCount (the FIFO count, same value for all items in batch)
      dateFormat= uses dedup index=3 → internal index 3 → field 4 (sessionNewsIndex)
        value: newsIndex, the 0-based loop iteration index
      temperatureUnit= uses dedup index=4 → internal index 4 → field 5 (newsId)
        value: newsIndex (same as sessionNewsIndex — 0 for first item)

    For a single-item send (fifo_count=N, newsIndex=0):
      session_status = (N > 0 ? 0 : 1) = 0  (NOT IS_LAST when fifo_count > 0)
      session_total_count = N                (the FIFO count — constant across all sends)
      session_news_index = 0                 (loop iteration index)
      news_id = 0                            (same as newsIndex, NOT a timestamp)
      session_id = 0                         (proto3 default; real app never sets it)

    sendEmptyNewsToGlassV3 (force path) sets index=1→1, index=2→1 DIRECTLY (no dedup),
    i.e. session_status=1, session_total_count=1; that path is the empty/clear marker
    and is NOT the same as real-content semantics.

    session_id is arbitrary and not assigned by the device.
    """
    out = eh.encode_varint_field(1, session_id)
    out += eh.encode_varint_field(2, session_status)
    out += eh.encode_varint_field(3, session_total_count)
    out += eh.encode_varint_field(4, session_news_index)
    out += eh.encode_varint_field(5, news_id)
    if title:
        out += eh.encode_string_field(6, title)
    out += eh.encode_varint_field(7, report_time)
    if source:
        out += eh.encode_string_field(8, source)
    if content:
        out += eh.encode_string_field(9, content)
    return out


def build_news_fifo_count_request_v3(magic_random):
    """Reproduces DashboardProHelper.needSendNewsCountV3 / ProtoDashboardExt.sendNeedSendNewsCountV3:
    the V3 equivalent of build_news_fifo_count_request. Sends AppRequestDeviceNewsInfo{field_1=1}
    at DashboardDataPackage field 9, commandId=APP_REQUEST_NEWS_INFO(7). The device should respond
    with ResponseNewsFifoMsg at DashboardDataPackage field 8, commandId=DEV_RESPONSE_NEWS_INFO(8) --
    the same response field as the V2 variant (the commandId in the envelope distinguishes them).

    Confirmed from blutter disassembly of dashboard_pro_helper.dart line 1827 (2026-09-27):
      AppRequestDeviceNewsInfo::create
      turnOnDevice=(1)  [dedup setter, sets field 1 = 1]
      ProtoDashboardExt.sendNeedSendNewsCountV3(connection, msg)
        BleG2CmdProtoExt.sendDataPackage(cmdId=7, AppRequestDeviceNewsInfo, ...)
          → encodes into DashboardDataPackage field 9 (pattern: cmdId+2=field; 7+2=9)
      Response: ResponseNewsFifoMsg at field 10, commandId=8 (cmdId+2=field: 8+2=10).
        NOTE: the blutter source said "internal index 7" which Dart maps to field 8, but live
        testing (2026-09-27) confirmed the actual response data is at field 10, not field 8.
        V2 FIFO count response (commandId=6) IS at field 8; V3 response (commandId=8) is field 10.

    First live test on 2026-09-27: got commandId=8 response, data at field 10 confirmed.
    """
    request = eh.encode_varint_field(1, 1)  # AppRequestDeviceNewsInfo{field_1=1}
    out = eh.encode_varint_field(1, CMD_APP_REQUEST_NEWS_INFO)
    out += eh.encode_varint_field(2, magic_random)
    out += eh.encode_message_field(9, request)
    return out


def build_display_enable(magic_random, widget_types, package_id=1):
    """Send CMD_DASHBOARD_RECEIVE with display_setting listing the given widget types and empty
    content. This enables the specified widget slots in the native dashboard rotation without
    pushing any widget content. Use before or after a widget content push to tell the firmware
    which slots should be active. widget_types: list of WIDGET_* constants, e.g. [WIDGET_NEWS].
    Returns SUCCESS (flag=0) for all valid widget types confirmed so far.
    """
    display_setting = encode_dashboard_display_setting(widget_types)
    receive = encode_dashboard_receive_from_app(package_id, b'', display_setting)
    return encode_dashboard_data_package(CMD_DASHBOARD_RECEIVE, magic_random, receive)


def build_news_display_enable(magic_random, package_id=1):
    """Convenience wrapper: enable news widget slot only."""
    return build_display_enable(magic_random, [WIDGET_NEWS], package_id)


def build_quicklist_display_enable(magic_random, package_id=1):
    """Convenience wrapper: enable quicklist/tasks widget slot only."""
    return build_display_enable(magic_random, [WIDGET_QUICKLIST], package_id)


def build_dashboard_init(magic_random, package_id=1):
    """Base-settings packet (LIVE-CONFIRMED). Send once after connecting, before any content.

    Full dashboard initialization packet -- mirrors the FIRST CMD_DASHBOARD_RECEIVE the real
    EvenHub app sends after connecting (btsnoop 2026-09-28 confirmed).

    Key differences from our previous display_enable calls:
      - display_mode=4  (DashboardMainWidgetMode: 0 quick note, 1 health, 2 stocks, 3 calendar,
                         4 news, 5 map -- it picks the MAIN widget, it is not an on/off flag)
      - status_display_order=[1,2,3]  (3 status slots enabled)
      - widget_display_order=[1,3,2,2]  (CORRECTED 2026-09-28 btsnoop: NEWS, SCHEDULE, STOCK, STOCK)
      - half_day_format=1  (12-hour clock)
      - temperature_unit=1  (Celsius)

    Send this ONCE after connecting (before any content push) to activate the native dashboard.
    Subsequent content pushes do NOT need to include display_setting.
    """
    display_setting = encode_dashboard_display_setting(
        widget_display_order=[WIDGET_NEWS, WIDGET_SCHEDULE, WIDGET_STOCK, WIDGET_STOCK],
        status_display_order=[1, 2, 3],
        display_mode=4,
        half_day_format=1,
        temperature_unit=TEMP_UNIT_CELSIUS,
    )
    receive = encode_dashboard_receive_from_app(package_id, b'', display_setting)
    return encode_dashboard_data_package(CMD_DASHBOARD_RECEIVE, magic_random, receive)


def build_news_reset(magic_random):
    """AppResetClearAllDataMsg (cmdId=13, field 15) with turnOnDevice=1.
    Clears the device news queue so FIFO count resets to nonzero. Send this before pushing
    news if the FIFO count returned 0 (device queue full from previous broken sends).
    Mirrors sendEmptyNewsToGlassV3 in the real app.
    """
    reset_msg = eh.encode_varint_field(1, 1)  # turnOnDevice=1
    out = eh.encode_varint_field(1, CMD_APP_REQUEST_CLEAR_ALL_DATA)
    out += eh.encode_varint_field(2, magic_random)
    out += eh.encode_message_field(15, reset_msg)
    return out


def build_news_push_v3(magic_random, news_id, title, report_time, source='', content='',
                        session_id=0, session_status=0, session_total_count=1, session_news_index=0):
    """LIVE-CONFIRMED news push (one article per call). report_time must be MILLISECONDS since
    the epoch (seconds make the article look like 1970 and the glasses ignore it); news_id must
    be the slot index (0, 1, 2, ...), the same as session_news_index.

    Top-level payload using the REAL production news path: AppSendNewsData set directly as
    DashboardDataPackage field 11, commandId=APP_SEND_NEWS_DATA(9). Corrected session field
    defaults (2026-09-28 blutter full-loop analysis):
      session_id=0 (proto3 default, real app never sets sessionId)
      session_status=0 (NOT IS_LAST; real app sends 0 when fifo_count > 0)
      session_total_count=1 (pass the actual FIFO count from AppRequestDeviceNewsInfo)
      session_news_index=0 (0-based iteration index)
    Pass the actual fifo_count from the FIFO query as session_total_count."""
    news_data = encode_app_send_news_data(session_id, session_status, session_total_count,
                                           session_news_index, news_id, title, report_time, source, content)
    out = eh.encode_varint_field(1, CMD_APP_SEND_NEWS_DATA)
    out += eh.encode_varint_field(2, magic_random)
    out += eh.encode_message_field(11, news_data)
    return out


def build_weather_push(magic_random, temperature, package_id=None, **weather_kwargs):
    """Full top-level payload to push weather status to the native dashboard. IMPLEMENTED,
    UNTESTED LIVE. The bytes match the official app exactly (golden test in
    tests/test_g2_offline.py). `temperature` is whole-degree CELSIUS, sent as a protobuf
    float; pass unit=TEMP_UNIT_FAHRENHEIT only to change how the glasses display it. Other
    keyword arguments go to encode_weather_status. The app leaves package_id unset (None).

    Weather has no WidgetType of its own (it lives under statusComponents, not widgetComponents),
    so there's no equivalent widgetDisplayOrder entry to set -- unclear yet whether it needs any
    enablement step at all; untested live as of this writing.
    """
    weather = encode_weather_status(temperature, **weather_kwargs)
    status_component = encode_status_component_weather(weather)
    content = encode_dashboard_content_status(status_component)
    receive = encode_dashboard_receive_from_app(package_id, content)
    return encode_dashboard_data_package(CMD_DASHBOARD_RECEIVE, magic_random, receive)


def build_calendar_event_push(magic_random, schedule, total, index, authority=0, package_id=1,
                              set_display_order=False):
    """ONE calendar event per message, the way the official app sends them (docs/research/
    app_behaviour_details.md §1): rScheduleWidget{1 scheduleTotal=n, 2 scheduleNum=i (0-based),
    3 Schedule}. `schedule` is (title, location, time_str, end_ts); scheduleId is left unset like
    the app. For "no events" call with schedule=None, total=0 and authority=1 if a calendar is
    chosen (INFERRED: changes the glasses' empty-state text). DECODED."""
    sched_msgs = []
    if schedule is not None:
        title, location, time_str, end_ts = schedule
        out = b''
        if title:
            out += eh.encode_string_field(2, title)
        if location:
            out += eh.encode_string_field(3, location)
        if time_str:
            out += eh.encode_string_field(4, time_str)
        out += eh.encode_varint_field(5, end_ts)
        sched_msgs.append(out)
    widget = eh.encode_varint_field(1, total) + eh.encode_varint_field(2, index)
    for s in sched_msgs:
        widget += eh.encode_message_field(3, s)
    if total == 0:
        widget += eh.encode_varint_field(4, authority)
    content = encode_dashboard_content_widget(encode_widget_component_schedule(widget))
    display_setting = encode_dashboard_display_setting([WIDGET_SCHEDULE]) if set_display_order else None
    receive = encode_dashboard_receive_from_app(package_id, content, display_setting)
    return encode_dashboard_data_package(CMD_DASHBOARD_RECEIVE, magic_random, receive)


def build_calendar_push(magic_random, schedules, package_id=1, set_display_order=False):
    """Full top-level payload to push one or more calendar events to the native dashboard.
    LIVE-CONFIRMED. Returns protobuf bytes for SID 0x01 (cmd 2, Dashboard_Receive).

    schedules: list of (schedule_id, title, location, time_str, end_timestamp_unix_seconds)
    set_display_order: also include a DashboardDisplaySetting enabling the schedule widget --
        without this, content may have no visible slot to render into.
    """
    sched_msgs = [encode_schedule(*s) for s in schedules]
    schedule_widget = encode_schedule_widget(sched_msgs)
    widget_component = encode_widget_component_schedule(schedule_widget)
    content = encode_dashboard_content_widget(widget_component)
    display_setting = encode_dashboard_display_setting([WIDGET_SCHEDULE]) if set_display_order else None
    receive = encode_dashboard_receive_from_app(package_id, content, display_setting)
    return encode_dashboard_data_package(CMD_DASHBOARD_RECEIVE, magic_random, receive)


# ── Reading the glasses' answers (small conveniences) ────────────────────────────────────────

# DashboardRespondToApp.flag (field 3 → field 2 of an ack to a cmd 2 push).
ACK_SUCCESS, ACK_PARAMETER_ERROR, ACK_NEWS_VERSION_ERROR = 0, 1, 2
ACK_FLAG_NAMES = {ACK_SUCCESS: 'DASHBOARD_RECEIVED_SUCCESS', ACK_PARAMETER_ERROR: 'DASHBOARD_PARAMETER_ERROR',
                  ACK_NEWS_VERSION_ERROR: 'DASHBOARD_NEWS_VERSION_ERROR'}


def ack_flag(pb):
    """Result flag of the ack to a Dashboard_Receive push (init, calendar, weather, display
    setting): 0 = success, 1 = parameter error, 2 = news version error (ACK_FLAG_NAMES). The flag
    sits in field 3 (DashboardRespondToApp) → field 2; field 1 of that message is the packageId,
    not the result. An empty field 3 means 0 (proto3 leaves zeros out). Returns None when `pb` is
    None (no ack) or carries no field 3."""
    if pb is None:
        return None
    respond = eh.read_bytes_field(pb, 3)
    if respond is None:
        return None
    return eh.read_varint_field(respond, 2, 0)


def news_fifo_count(pb, default=5):
    """Slot count from the reply to build_news_fifo_count_request_v3 (cmd 8, field 10 → field 1).
    Returns `default` (5, what every live reply said) when the reply is missing or empty."""
    if pb is None:
        return default
    resp = eh.read_bytes_field(pb, 10)
    if resp is None:
        return default
    return eh.read_varint_field(resp, 1, default) or default


def news_status(pb):
    """newsStatus of the reply to build_news_push_v3 (cmd 10, field 12 → field 5): 0 = stored,
    anything else = not stored, send it again (app 2.3.1, docs/status.md). None when no reply."""
    if pb is None:
        return None
    resp = eh.read_bytes_field(pb, 12)
    return eh.read_varint_field(resp, 5, 0) if resp is not None else 0


def limit_utf8(text, max_bytes, ellipsis='...'):
    """Cut `text` so its UTF-8 form fits in `max_bytes`, the way the official app does for
    calendar and news strings: when it is too long, cut on a character boundary at
    max_bytes - len(ellipsis) bytes and append the ellipsis. Text that fits is returned as is.
    limit_utf8('héllo world', 8) → 'héll...' (5 + 3 bytes)."""
    text = text or ''
    raw = text.encode('utf-8')
    if len(raw) <= max_bytes:
        return text
    keep = max(0, max_bytes - len(ellipsis.encode('utf-8')))
    return raw[:keep].decode('utf-8', 'ignore') + ellipsis
