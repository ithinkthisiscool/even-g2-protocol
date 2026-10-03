"""On-glass menu / app launcher (SID 0x03).

Plain English
-------------
The glasses have a launcher menu listing the apps you can open (Dashboard, Even AI, Translate...).
The phone decides what is in it: it sends the WHOLE list every time (there is no "add one item"
command), and the glasses replace their menu with it. The official app requires 5 to 10 items.

Use build_builtin_menu_push(magic, app_ids) -- LIVE-CONFIRMED. Built-in apps carry ONLY their app
id (itemType 0); the glasses draw the name and icon themselves. Valid ids are BUILTIN_MENU_APPS
below. Sending a name or icon number on a built-in item, or an id that is not a menu app (0, 3, 9,
10, 12, 224...), is why earlier attempts failed. Custom names only work for installed EvenHub
plugins (itemType 1 + the plugin's own app id) -- untested here.

    pb = build_builtin_menu_push(session.next_magic(), [1, 4, 5, 6, 7])
    frames = evenhub.frame_pb(pb, SID_MENU, evenhub.FLAG_REQUEST, session.next_seq())

build_menu_push / encode_menu_item are the general (all four fields) encoders, for plugin items.

Schema source: extracted the same way as g2/dashboard.py (blutter decompile of the real app's
Dart code, every field number read directly from BuilderInfo registration calls in
menu.pb.dart / menu.pbenum.dart).

SID = 0x03 (UI_FOREGROUND_MEUN_ID, from the app's own service_id_def.pbenum.dart -- the complete
real SID table, see esp32_ai/docs/research/unknowns_findings.md section 3 and esp32_ai/docs/GLASSES.md).

Schema:
  meun_main_msg_ctx (top-level envelope, oneof):
    1  Cmd          enum Menu_Cmd_List (0=APP_SEND_MENU_INFO, 1=OS_RESPONSE_MENU_INFO)
    2  MagicRandom  int32
    3  sendData     -> MenuInfoSend   (app -> device: push the menu item list)
    4  resData      -> ResponseMenuInfo (device -> app response)

  MenuInfoSend:
    1  itemTotalNum  int32
    2  item          -> Menu_Item_Ctx (repeated, packed message)

  Menu_Item_Ctx (one menu entry):
    1  itemType   int32  (unclear semantics -- untested)
    2  iconNum    int32  (likely selects a built-in icon graphic index -- untested)
    3  itemName   string
    4  itemAppId  int32  (likely the SID/app identifier launched on selection -- untested)

  ResponseMenuInfo:
    1  respValue  int32

Status: built-in items (itemType=0 + itemAppId only) are LIVE-CONFIRMED (2026-09-28).
itemType=1 plugin items with a name/icon are known from the decompile but untested.
"""
import sys
import os

from . import evenhub as eh

SID_MENU = 0x03

CMD_APP_SEND_MENU_INFO = 0
CMD_OS_RESPONSE_MENU_INFO = 1


# Built-in menu apps (blutter: UnitService._syneMenusToGlass). Built-ins are itemType=0 and carry
# only itemAppId -- the glasses draw name+icon. Custom names need itemType=1 + an installed
# EvenHub plugin's appId. The app enforces 5..10 enabled items and resends the full list to add.
BUILTIN_MENU_APPS = {1: 'Dashboard', 4: 'Notifications', 5: 'Translate', 6: 'Teleprompt',
                     7: 'Even AI', 8: 'Navigate', 11: 'Conversate', 48: 'Terminal',
                     266: 'Silent mode'}
MENU_MIN_ITEMS, MENU_MAX_ITEMS = 5, 10
ITEM_TYPE_BUILTIN, ITEM_TYPE_PLUGIN = 0, 1


def encode_builtin_menu_item(app_id):
    """Menu_Item_Ctx for a built-in app: {1: itemType 0, 4: app_id}. No name, no icon."""
    return eh.encode_varint_field(1, ITEM_TYPE_BUILTIN) + eh.encode_varint_field(4, app_id)


def build_builtin_menu_push(magic_random, app_ids):
    """LIVE-CONFIRMED. Full menu replacement made of built-in apps, in the given order.
    app_ids: 5..10 ids from BUILTIN_MENU_APPS, e.g. [1, 4, 5, 6, 7]. Returns protobuf bytes for
    SID 0x03: {1: cmd 0, 2: magic, 3: {1: count, 2: item...}}."""
    send_data = eh.encode_varint_field(1, len(app_ids))
    for app_id in app_ids:
        send_data += eh.encode_message_field(2, encode_builtin_menu_item(app_id))
    return (eh.encode_varint_field(1, CMD_APP_SEND_MENU_INFO) + eh.encode_varint_field(2, magic_random) +
            eh.encode_message_field(3, send_data))


def encode_menu_item(item_type, icon_num, item_name, item_app_id):
    """General Menu_Item_Ctx with all four fields (name omitted when empty). For plugin items
    (itemType 1); do NOT use it for built-ins -- use encode_builtin_menu_item. Untested."""
    out = eh.encode_varint_field(1, item_type)
    out += eh.encode_varint_field(2, icon_num)
    if item_name:
        out += eh.encode_string_field(3, item_name)
    out += eh.encode_varint_field(4, item_app_id)
    return out


def encode_menu_info_send(items):
    """items: list of (item_type, icon_num, item_name, item_app_id)."""
    out = eh.encode_varint_field(1, len(items))
    for it in items:
        out += eh.encode_message_field(2, encode_menu_item(*it))
    return out


def build_menu_push(magic_random, items):
    """Top-level meun_main_msg_ctx payload pushing a full menu item list to the glasses.
    items: list of (item_type, icon_num, item_name, item_app_id). General form, untested for
    plugin items; for built-ins prefer build_builtin_menu_push."""
    send_data = encode_menu_info_send(items)
    out = eh.encode_varint_field(1, CMD_APP_SEND_MENU_INFO)
    out += eh.encode_varint_field(2, magic_random)
    out += eh.encode_message_field(3, send_data)
    return out
