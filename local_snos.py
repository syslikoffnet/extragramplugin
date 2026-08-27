"""
Local Snos — локальный «снос» чужого аккаунта и локальные NFT-подарки
в exteraGram / AyuGram.

Ничего не отправляется на сервер Telegram. Снос меняет только то, как
этот клиент рисует выбранного пользователя. Локальные подарки живут
только в памяти и настройках плагина: их нельзя продать, передать
или улучшить через настоящий API.
"""

from __future__ import annotations

import json
import random
import re
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Set

from android_utils import log, run_on_ui_thread
from base_plugin import (
    BasePlugin,
    HookResult,
    HookStrategy,
    MenuItemData,
    MenuItemType,
    MethodHook,
)
from hook_utils import find_class, get_private_field, set_private_field
from ui.alert import AlertDialogBuilder
from ui.bulletin import BulletinHelper
from ui.settings import Divider, Header, Input, Switch, Text

try:
    from client_utils import (
        get_last_fragment,
        get_messages_controller,
        get_notification_center,
        get_user_config,
        send_request,
    )
except Exception:  # pragma: no cover - defensive import for older SDKs
    get_last_fragment = None  # type: ignore
    get_messages_controller = None  # type: ignore
    get_notification_center = None  # type: ignore
    get_user_config = None  # type: ignore
    send_request = None  # type: ignore

try:
    from org.telegram.messenger import NotificationCenter, UserConfig
except Exception:  # pragma: no cover
    NotificationCenter = None  # type: ignore
    UserConfig = None  # type: ignore

try:
    from org.telegram.tgnet import TLRPC
except Exception:  # pragma: no cover
    TLRPC = None  # type: ignore


__id__ = "local_snos"
__name__ = "Local Snos"
__description__ = (
    "Локальный снос чужих аккаунтов и локальные NFT-подарки.\n\n"
    "Кнопка **hi** в меню профиля (⋮) и команда `.snos id/@user` "
    "делают человека похожим на замороженный аккаунт — **только у тебя**.\n\n"
    "Локально ставит user.deleted — клиент сам рисует HiddenName и "
    "аватар deleted (снежинка), как настоящий frozen. В своём профиле "
    "можно добавить локальные подарки. Настоящие подарки плагин не трогает."
)
__author__ = "@extragramplugin"
__version__ = "1.3.0"
__icon__ = "exteraPlugins/1"
__app_version__ = ">=11.0.0"
__sdk_version__ = ">=1.4.3.3"


DEFAULT_DISPLAY_NAME = "удаленный аккаунт"
DEFAULT_FROZEN_BIO = "Аккаунт заморожен"
SNOS_STORE_KEY = "snos_users"
ENABLED_KEY = "enabled"
DISPLAY_NAME_KEY = "display_name"
FROZEN_BIO_KEY = "frozen_bio"
COMMAND_KEY = "command_enabled"
GIFTS_ENABLED_KEY = "gifts_enabled"
GIFTS_STORE_KEY = "local_gifts"
GIFT_SEARCH_KEY = "gift_search"

COMMAND_RE = re.compile(r"^\.(snos|unsnos)(?:\s+([\s\S]+))?$", re.IGNORECASE)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
TME_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z0-9_]+)",
    re.IGNORECASE,
)

# user.#83314fca flags — same bits Telegram uses for deleted users
FLAG_FIRST_NAME = 1 << 1
FLAG_LAST_NAME = 1 << 2
FLAG_USERNAME = 1 << 3
FLAG_PHONE = 1 << 4
FLAG_PHOTO = 1 << 5
FLAG_DELETED = 1 << 13
FLAG_USERNAMES = 1 << 22

SERVICE_USER_IDS = {
    777000,
    333000,
    42777,
    708513,
    1271266957,
    2666000,
    489000,
    489001,
}

UPDATE_MASK_FALLBACK = 1 | 2 | 4 | 128 | 4096  # name, avatar, status, phone, user_phone

LOCAL_MSG_MIN = 1_900_000_000
LOCAL_MSG_MAX = 1_999_999_999
LOCAL_SAVED_BASE = -9_001_000_000_000_000
LOCAL_SLUG_PREFIX = "local-ls-"

SAVED_FLAG_FROM_ID = 1 << 1
SAVED_FLAG_MSG_ID = 1 << 3
SAVED_FLAG_UPGRADE_STARS = 1 << 6
SAVED_FLAG_CAN_UPGRADE = 1 << 10
SAVED_FLAG_SAVED_ID = 1 << 11
SAVED_FLAG_PINNED = 1 << 12
USERFULL_STARGIFTS_FLAG2 = 1 << 8  # userFull.flags2.stargifts_count

GIFT_MUTATION_HINTS = (
    "upgradeStarGift",
    "transferStarGift",
    "convertStarGift",
    "updateStarGiftPrice",
    "saveStarGift",
    "toggleStarGiftsPinnedToTop",
    "getStarGiftWithdrawalUrl",
    "dropStarGiftOriginalDetails",
    "updateStarGiftCollection",
    "deleteStarGiftCollection",
    "createStarGiftCollection",
    "reorderStarGiftCollections",
    "toggleChatStarGiftNotifications",
)
PAYMENT_HINTS = (
    "getPaymentForm",
    "sendStarsForm",
    "sendPaymentForm",
)
LIST_HINTS = (
    "getSavedStarGifts",
    "getSavedStarGift",
)
GIFT_SHEET_CLASSES = (
    "org.telegram.ui.Stars.StarGiftSheet",
    "org.telegram.ui.Gifts.StarGiftSheet",
    "org.telegram.ui.Components.Premium.StarGiftSheet",
    "org.telegram.ui.Stars.StarGiftUniqueSheet",
)
TL_SAVED_NAMES = (
    "org.telegram.tgnet.tl.TL_stars$TL_savedStarGift",
    "org.telegram.tgnet.tl.TL_stars$SavedStarGift",
    "org.telegram.tgnet.TLRPC$TL_savedStarGift",
    "org.telegram.tgnet.TLRPC$savedStarGift",
)
TL_UNIQUE_NAMES = (
    "org.telegram.tgnet.tl.TL_stars$TL_starGiftUnique",
    "org.telegram.tgnet.tl.TL_stars$starGiftUnique",
    "org.telegram.tgnet.TLRPC$TL_starGiftUnique",
)
TL_GET_GIFTS_NAMES = (
    "org.telegram.tgnet.tl.TL_stars$TL_starGifts_getStarGifts",
    "org.telegram.tgnet.tl.TL_stars$TL_payments_getStarGifts",
    "org.telegram.tgnet.tl.TL_stars$getStarGifts",
    "org.telegram.tgnet.TLRPC$TL_payments_getStarGifts",
    "org.telegram.tgnet.TLRPC$TL_stars_getStarGifts",
    "org.telegram.tgnet.tl.TL_stars$TL_getStarGifts",
)
TL_PREVIEW_NAMES = (
    "org.telegram.tgnet.tl.TL_stars$TL_payments_getStarGiftUpgradePreview",
    "org.telegram.tgnet.tl.TL_stars$TL_starGifts_getStarGiftUpgradePreview",
    "org.telegram.tgnet.TLRPC$TL_payments_getStarGiftUpgradePreview",
    "org.telegram.tgnet.tl.TL_stars$getStarGiftUpgradePreview",
)
TL_ATTRS_NAMES = (
    "org.telegram.tgnet.tl.TL_stars$TL_payments_getStarGiftUpgradeAttributes",
    "org.telegram.tgnet.tl.TL_stars$TL_starGifts_getStarGiftUpgradeAttributes",
    "org.telegram.tgnet.TLRPC$TL_payments_getStarGiftUpgradeAttributes",
)
GIFT_CATEGORIES = (
    ("Pepe / жабы", ("pepe", "toad", "frog", "жаб", "лягуш", "plush", "plushepe")),
    ("Духи", ("perfume", "дух", "fragrance", "scent", "cologne", "flacon")),
    ("Книги", ("book", "книг", "diary", "notebook", "journal")),
    ("Ручки", ("pen", "ручк", "pencil", "marker", "fountain")),
    ("Цветы", ("rose", "flower", "bouquet", "тюльпан", "роз", "цвет", "tulip", "lily")),
    ("Сердца", ("heart", "сердц", "love", "valentine")),
    ("Мишки / плюш", ("bear", "teddy", "мишк", "плюш", "bunny", "toy")),
    ("Торты / еда", ("cake", "candy", "cookie", "шоколад", "торт", "еда", "champagne", "wine")),
    ("Кольца / украшения", ("ring", "gem", "diamond", "кольц", "брилл", "necklace")),
)
TL_ATTR_MODEL_NAMES = (
    "org.telegram.tgnet.tl.TL_stars$TL_starGiftAttributeModel",
    "org.telegram.tgnet.tl.TL_stars$starGiftAttributeModel",
    "org.telegram.tgnet.TLRPC$TL_starGiftAttributeModel",
)
TL_ATTR_PATTERN_NAMES = (
    "org.telegram.tgnet.tl.TL_stars$TL_starGiftAttributePattern",
    "org.telegram.tgnet.tl.TL_stars$starGiftAttributePattern",
    "org.telegram.tgnet.TLRPC$TL_starGiftAttributePattern",
)
TL_ATTR_BACKDROP_NAMES = (
    "org.telegram.tgnet.tl.TL_stars$TL_starGiftAttributeBackdrop",
    "org.telegram.tgnet.tl.TL_stars$starGiftAttributeBackdrop",
    "org.telegram.tgnet.TLRPC$TL_starGiftAttributeBackdrop",
)
TL_PEER_USER_NAMES = (
    "org.telegram.tgnet.TLRPC$TL_peerUser",
    "org.telegram.tgnet.TLRPC$TL_peerUser",
)
TL_UPDATES_NAMES = (
    "org.telegram.tgnet.TLRPC$TL_updates",
    "org.telegram.tgnet.TLRPC$Updates",
)
TL_UPDATE_NEW_MSG_NAMES = (
    "org.telegram.tgnet.tl.TL_update$TL_updateNewMessage",
    "org.telegram.tgnet.TLRPC$TL_updateNewMessage",
)
TL_MESSAGE_SERVICE_NAMES = (
    "org.telegram.tgnet.TLRPC$TL_messageService",
    "org.telegram.tgnet.TLRPC$TL_message",
)
TL_ACTION_UNIQUE_NAMES = (
    "org.telegram.tgnet.TLRPC$TL_messageActionStarGiftUnique",
    "org.telegram.tgnet.tl.TL_stars$TL_messageActionStarGiftUnique",
)
TL_PAYMENT_FORM_NAMES = (
    "org.telegram.tgnet.TLRPC$TL_payments_paymentFormStarGift",
    "org.telegram.tgnet.tl.TL_stars$TL_paymentFormStarGift",
    "org.telegram.tgnet.tl.TL_stars$TL_payments_paymentFormStarGift",
    "org.telegram.tgnet.TLRPC$TL_payments_paymentFormStars",
    "org.telegram.tgnet.TLRPC$TL_payments_paymentForm",
)
TL_PAYMENT_RESULT_NAMES = (
    "org.telegram.tgnet.TLRPC$TL_payments_paymentResult",
    "org.telegram.tgnet.tl.TL_stars$TL_payments_paymentResult",
)
TL_INVOICE_NAMES = (
    "org.telegram.tgnet.TLRPC$TL_invoice",
)
TL_ERROR_NAMES = (
    "org.telegram.tgnet.TLRPC$TL_error",
)

UNIQUE_MODELS = (
    ("Aurora", 1.4),
    ("Ember", 2.1),
    ("Crystal", 0.8),
    ("Phantom", 0.5),
    ("Lotus", 3.6),
    ("Inferno", 1.1),
    ("Nebula", 0.9),
    ("Sakura", 2.4),
    ("Obsidian", 0.6),
    ("Ivory", 4.2),
)
UNIQUE_BACKDROPS = (
    ("Midnight", 4.8),
    ("Sunset", 3.2),
    ("Arctic", 2.7),
    ("Royal", 1.6),
    ("Jade", 5.1),
    ("Cosmic", 1.3),
    ("Amber", 3.9),
    ("Noir", 0.7),
)
UNIQUE_PATTERNS = (
    ("Spark", 6.4),
    ("Wave", 5.2),
    ("Hex", 2.8),
    ("Bloom", 3.5),
    ("Orbit", 1.9),
    ("Frost", 4.1),
    ("Pulse", 2.2),
)


def _safe_log(message: str) -> None:
    try:
        log(f"[local_snos] {message}")
    except Exception:
        pass


def _format_exc() -> str:
    try:
        return traceback.format_exc()
    except Exception:
        return "traceback unavailable"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _jlong(value: int) -> Any:
    text = str(int(value))
    try:
        from java.lang import Long

        return Long.valueOf(text)
    except Exception:
        try:
            from java.lang import Long

            return Long.decode(text)
        except Exception:
            return int(value)


def _jbool(value: bool) -> Any:
    try:
        from java.lang import Boolean

        return Boolean.TRUE if value else Boolean.FALSE
    except Exception:
        return bool(value)


def _jint(value: int) -> Any:
    # Pass a string so Chaquopy does not box a Python int as java.lang.Long.
    text = str(int(value))
    try:
        from java.lang import Integer

        return Integer.valueOf(text)
    except Exception:
        try:
            from java.lang import Integer

            return Integer.decode(text)
        except Exception:
            return int(value)


def _class_name(obj: Any) -> str:
    if obj is None:
        return ""
    try:
        return _as_str(obj.getClass().getName())
    except Exception:
        try:
            return obj.__class__.__name__
        except Exception:
            return ""


def _new_java_list() -> Any:
    try:
        from java.util import ArrayList

        return ArrayList()
    except Exception:
        return []


def _is_tl_user(obj: Any) -> bool:
    if obj is None:
        return False
    if TLRPC is not None:
        try:
            if isinstance(obj, TLRPC.User):
                return True
        except Exception:
            pass
    return hasattr(obj, "first_name") and hasattr(obj, "deleted") and hasattr(obj, "id")


def _is_user_full(obj: Any) -> bool:
    if obj is None:
        return False
    if TLRPC is not None:
        try:
            if isinstance(obj, TLRPC.UserFull):
                return True
        except Exception:
            pass
    return hasattr(obj, "about") and hasattr(obj, "id") and not hasattr(obj, "first_name")


def _java_list(value: Any) -> List[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except Exception:
        pass
    try:
        size = int(value.size())
        return [value.get(i) for i in range(size)]
    except Exception:
        return []


class _CallbackHook(MethodHook):
    """Xposed hook that never lets an exception escape into the client."""

    def __init__(
        self,
        before: Optional[Callable[[Any], None]] = None,
        after: Optional[Callable[[Any], None]] = None,
    ):
        self._before = before
        self._after = after

    def before_hooked_method(self, param: Any) -> None:
        if not self._before:
            return
        try:
            self._before(param)
        except Exception:
            _safe_log(f"before-hook crashed:\n{_format_exc()}")

    def after_hooked_method(self, param: Any) -> None:
        if not self._after:
            return
        try:
            self._after(param)
        except Exception:
            _safe_log(f"after-hook crashed:\n{_format_exc()}")


class LocalSnosPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__()
        self._snos_ids: Set[int] = set()
        self._records: Dict[str, Dict[str, Any]] = {}
        self._photo_refs: Dict[int, Any] = {}
        self._status_refs: Dict[int, Any] = {}
        self._usernames_refs: Dict[int, Any] = {}
        self._emoji_refs: Dict[int, Any] = {}
        self._phone_refs: Dict[int, Any] = {}
        self._applying = False
        self._db_stack: List[List[Any]] = []
        self._menu_ids: List[Any] = []
        self._gifts: List[Dict[str, Any]] = []
        self._catalog: List[Any] = []
        self._catalog_by_id: Dict[int, Any] = {}
        self._catalog_at = 0.0
        self._catalog_loading = False
        self._last_gifts_self = False
        self._last_gifts_filters: Dict[str, bool] = {}
        self._reapply_token = 0
        self._tl_cache: Dict[str, Any] = {}
        self._blocking_local = False
        self._unique_attr_refs: Dict[str, Any] = {}
        self._picker_gifts: List[Any] = []
        self._picker_heading = ""
        self._gift_count_bump = 0
        self._live_gift_sheets: List[Any] = []
        self._sheet_rec_ids: Dict[int, str] = {}
        self._suppress_gift_error = 0
        self._local_upgrade_tokens: Set[int] = set()
        self._serving_upgrade = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_plugin_load(self) -> None:
        self._reload_store()
        try:
            self._reload_gifts()
        except Exception:
            self._gifts = []
            _safe_log(f"reload gifts failed:\n{_format_exc()}")
        self._install_java_hooks()
        try:
            self._install_gift_java_hooks()
        except Exception:
            _safe_log(f"gift java hooks failed:\n{_format_exc()}")
        try:
            self._install_request_hooks()
        except Exception:
            _safe_log(f"request hooks failed:\n{_format_exc()}")
        self._install_menus()
        try:
            self.add_on_send_message_hook()
        except Exception:
            _safe_log(f"add_on_send_message_hook failed:\n{_format_exc()}")
        self.log("Local Snos 1.3.0 loaded")
        run_on_ui_thread(self._reapply_all, 400)
        run_on_ui_thread(self._reapply_all, 1800)
        run_on_ui_thread(self._prefetch_catalog, 700)

    def on_plugin_unload(self) -> None:
        try:
            self._restore_all_memory(persist=False)
        except Exception:
            _safe_log(f"unload restore failed:\n{_format_exc()}")
        self.log("Local Snos unloaded")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def create_settings(self) -> List[Any]:
        self._reload_store()
        self._reload_gifts()
        rows: List[Any] = [
            Header(text="Local Snos"),
            Switch(
                key=ENABLED_KEY,
                text="Включить локальный снос",
                default=True,
                subtext="Выключи, чтобы временно вернуть настоящие имена и аватарки",
                icon="msg_settings",
                on_change=self._on_enabled_change,
            ),
            Switch(
                key=COMMAND_KEY,
                text="Команда .snos в чатах",
                default=True,
                subtext="Команда не отправляется собеседнику",
                icon="msg_offline",
            ),
            Text(
                text="Как рисует Telegram",
                subtext="Плагин только ставит user.deleted. Имя (HiddenName) и снежинку в аватаре рисует сам клиент — как у настоящего frozen.",
                icon="msg_info",
            ),
            Divider(text="Как пользоваться"),
            Text(
                text="Кнопка hi в профиле",
                subtext="Чужой профиль → ⋮ → hi. Повторное нажатие откатывает. Есть Undo.",
                icon="msg_contacts",
            ),
            Text(
                text="Команды",
                subtext=".snos id/@user   ·   .unsnos id/@user   ·   .snos в личке без аргумента",
                icon="msg_offline",
            ),
            Divider(text="Локально скрытые аккаунты"),
        ]

        if not self._records:
            rows.append(
                Text(
                    text="Пока никого нет",
                    subtext="Скрытые аккаунты появятся здесь — нажми, чтобы восстановить",
                    icon="msg_info",
                )
            )
        else:
            for key in self._sorted_record_keys():
                record = self._records[key]
                user_id = _as_int(record.get("user_id") or key)
                original = record.get("display") or record.get("first_name") or str(user_id)
                username = record.get("username") or ""
                extra = f"ID {user_id}"
                if username:
                    extra += f"  ·  @{username}"
                extra += "  ·  нажми, чтобы вернуть"
                rows.append(
                    Text(
                        text=_as_str(original, str(user_id)),
                        subtext=extra,
                        icon="msg_delete",
                        red=True,
                        on_click=self._restore_click_handler(user_id),
                    )
                )
            rows.append(Divider())
            rows.append(
                Text(
                    text="Восстановить всех",
                    subtext=f"Вернуть {len(self._records)} аккаунт(ов) как было",
                    icon="msg_reset",
                    red=True,
                    on_click=lambda _view=None: self._confirm_restore_all(),
                )
            )

        rows.extend(self._gift_settings_rows())
        return rows

    def _on_enabled_change(self, enabled: bool) -> None:
        if enabled:
            self._reapply_all()
            self._toast_success("Локальный снос снова включён")
        else:
            self._restore_all_memory(persist=False)
            self._notify_ui(self._selected_account())
            self._toast_info("Визуальный снос выключен. Список сохранён.")

    def _restore_click_handler(self, user_id: int) -> Callable[..., None]:
        def _on_click(_view: Any = None) -> None:
            self._confirm_restore_one(user_id)

        return _on_click

    def _confirm_restore_one(self, user_id: int) -> None:
        record = self._records.get(str(user_id), {})
        title = record.get("display") or record.get("first_name") or str(user_id)

        def _ok(_builder: Any = None, _which: Any = None) -> None:
            self._restore_user(user_id, persist=True, notify=True)
            self._toast_success(f"Восстановлен: {title}")
            try:
                if _builder is not None:
                    _builder.dismiss()
            except Exception:
                pass

        self._show_confirm(
            "Вернуть аккаунт?",
            f"«{title}» снова станет обычным — только локально, у тебя в клиенте.",
            "Восстановить",
            _ok,
        )

    def _confirm_restore_all(self) -> None:
        count = len(self._records)
        if count <= 0:
            self._toast_info("Список и так пуст")
            return

        def _ok(_builder: Any = None, _which: Any = None) -> None:
            restored = self._restore_all_memory(persist=True)
            self._notify_ui(self._selected_account())
            self._toast_success(f"Восстановлено: {restored}")
            try:
                if _builder is not None:
                    _builder.dismiss()
            except Exception:
                pass

        self._show_confirm(
            "Восстановить всех?",
            f"Будет отменён локальный снос для {count} аккаунт(ов).",
            "Восстановить всех",
            _ok,
        )

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _install_menus(self) -> None:
        for menu_type, subtext in (
            (MenuItemType.PROFILE_ACTION_MENU, "Локальный снос / откат"),
            (MenuItemType.CHAT_ACTION_MENU, "Локальный снос этого человека"),
        ):
            item = self._build_menu_item(menu_type, subtext)
            if item is None:
                continue
            menu_id = self._add_menu_safe(item)
            if menu_id is not None:
                self._menu_ids.append(menu_id)
        gift_item = self._build_gift_menu_item()
        if gift_item is not None:
            menu_id = self._add_menu_safe(gift_item)
            if menu_id is not None:
                self._menu_ids.append(menu_id)

    def _build_menu_item(self, menu_type: Any, subtext: str) -> Optional[MenuItemData]:
        kwargs_chain = [
            dict(
                menu_type=menu_type,
                text="hi",
                subtext=subtext,
                icon="msg_block",
                on_click=self._on_hi_click,
                condition="user != null && !user.self",
                priority=80,
            ),
            dict(
                menu_type=menu_type,
                text="hi",
                subtext=subtext,
                icon="msg_block",
                on_click=self._on_hi_click,
                condition="user != null && !user.self",
            ),
            dict(
                menu_type=menu_type,
                text="hi",
                icon="msg_block",
                on_click=self._on_hi_click,
            ),
            dict(
                menu_type=menu_type,
                text="hi",
                on_click=self._on_hi_click,
            ),
        ]
        last_error = None
        for kwargs in kwargs_chain:
            try:
                return MenuItemData(**kwargs)
            except TypeError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
        _safe_log(f"MenuItemData incompatible: {last_error}")
        return None

    def _build_gift_menu_item(self) -> Optional[MenuItemData]:
        kwargs_chain = [
            dict(
                menu_type=MenuItemType.PROFILE_ACTION_MENU,
                text="Подарки",
                subtext="Локальные NFT на своём профиле",
                icon="msg_gift",
                on_click=self._on_gifts_menu_click,
                condition="user != null && user.self",
                priority=90,
            ),
            dict(
                menu_type=MenuItemType.PROFILE_ACTION_MENU,
                text="Подарки",
                subtext="Локальные NFT на своём профиле",
                icon="msg_fave",
                on_click=self._on_gifts_menu_click,
                condition="user != null && user.self",
            ),
            dict(
                menu_type=MenuItemType.PROFILE_ACTION_MENU,
                text="Подарки",
                on_click=self._on_gifts_menu_click,
                condition="user != null && user.self",
            ),
        ]
        last_error = None
        for kwargs in kwargs_chain:
            try:
                return MenuItemData(**kwargs)
            except TypeError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
        _safe_log(f"gift MenuItemData incompatible: {last_error}")
        return None

    def _add_menu_safe(self, item: MenuItemData) -> Any:
        try:
            return self.add_menu_item(item)
        except Exception:
            _safe_log(f"add_menu_item failed, retry without condition:\n{_format_exc()}")
            try:
                item.condition = None  # type: ignore[attr-defined]
                return self.add_menu_item(item)
            except Exception:
                _safe_log(f"add_menu_item retry failed:\n{_format_exc()}")
                return None

    def _on_hi_click(self, context: Dict[str, Any]) -> None:
        try:
            user = context.get("user")
            account = _as_int(context.get("account"), self._selected_account())
            fragment = context.get("fragment")
            if user is None:
                user_id = _as_int(context.get("userId") or context.get("user_id"))
                if user_id:
                    user = self._get_user(account, user_id)
            if user is None:
                self._toast_error("Не удалось получить пользователя")
                return
            self._toggle_user(account, user, fragment=fragment)
        except Exception:
            _safe_log(f"hi click failed:\n{_format_exc()}")
            self._toast_error("Ошибка локального сноса")

    def _toggle_user(self, account: int, user: Any, fragment: Any = None) -> None:
        if not self._feature_enabled():
            self._toast_error("Плагин выключен в настройках")
            return
        if not _is_tl_user(user):
            self._toast_error("Это не пользователь")
            return
        user_id = _as_int(getattr(user, "id", 0))
        if user_id <= 0:
            self._toast_error("Некорректный id")
            return
        if getattr(user, "self", False) or self._is_me(account, user_id):
            self._toast_error("Нельзя применить к своему аккаунту")
            return
        if user_id in SERVICE_USER_IDS:
            self._toast_error("Служебный аккаунт Telegram трогать нельзя")
            return

        if user_id in self._snos_ids:
            self._restore_user(user_id, persist=True, notify=True, account=account, fragment=fragment)
            self._toast_success("Локальный снос отменён")
            return

        if self._apply_snos(account, user, fragment=fragment):
            display = self._original_display(user_id) or str(user_id)

            def _undo() -> None:
                self._restore_user(user_id, persist=True, notify=True, account=account)

            try:
                BulletinHelper.show_undo(
                    f"{display} скрыт локально",
                    on_undo=_undo,
                    subtitle="Только в этом клиенте",
                )
            except Exception:
                self._toast_success("Аккаунт скрыт локально")

    # ------------------------------------------------------------------
    # Chat command  .snos / .unsnos
    # ------------------------------------------------------------------

    def on_send_message_hook(self, account: int, params: Any) -> HookResult:
        try:
            if not self.get_setting(COMMAND_KEY, True):
                return HookResult()
            message = getattr(params, "message", None)
            if message is None:
                return HookResult()
            try:
                raw = str(message).strip()
            except Exception:
                return HookResult()
            if not raw:
                return HookResult()
            match = COMMAND_RE.match(raw)
            if not match:
                return HookResult()

            action = match.group(1).lower()
            argument = (match.group(2) or "").strip()
            peer = self._peer_from_params(params)

            run_on_ui_thread(
                lambda: self._handle_command(account, action, argument, peer)
            )
            return HookResult(strategy=HookStrategy.CANCEL)
        except Exception:
            _safe_log(f"send hook failed:\n{_format_exc()}")
            return HookResult()

    def _handle_command(self, account: int, action: str, argument: str, peer: Optional[int]) -> None:
        if not self._feature_enabled() and action == "snos":
            self._toast_error("Плагин выключен в настройках")
            return

        target = self._normalize_target(argument)
        if not target:
            if peer and peer > 0:
                user = self._get_user(account, peer)
                if user is None:
                    self._toast_error("В этом чате нет пользователя")
                    return
                self._apply_command(account, action, user)
                return
            self._toast_info("Использование: .snos id/@username")
            return

        self._resolve_user(
            account,
            target,
            lambda user, error: self._on_resolved(account, action, user, error),
        )

    def _on_resolved(self, account: int, action: str, user: Any, error: Optional[str]) -> None:
        if error or user is None:
            self._toast_error(error or "Пользователь не найден")
            return
        self._apply_command(account, action, user)

    def _apply_command(self, account: int, action: str, user: Any) -> None:
        user_id = _as_int(getattr(user, "id", 0))
        if action == "unsnos":
            if user_id not in self._snos_ids:
                self._toast_info("Этот аккаунт и так не скрыт")
                return
            self._restore_user(user_id, persist=True, notify=True, account=account)
            self._toast_success("Локальный снос отменён")
            return
        if user_id in self._snos_ids:
            self._toast_info("Этот аккаунт уже скрыт локально")
            return
        self._toggle_user(account, user)

    def _normalize_target(self, raw: str) -> str:
        text = (raw or "").strip().strip('"').strip("'").strip()
        if not text:
            return ""
        text = text.split()[0]
        text = text.replace("tg://resolve?domain=", "")
        text = text.replace("tg://user?id=", "")
        match = TME_RE.search(text)
        if match:
            return match.group(1)
        if text.startswith("@"):
            return text[1:]
        return text

    def _resolve_user(self, account: int, target: str, callback: Callable[[Any, Optional[str]], None]) -> None:
        if target.lstrip("-").isdigit():
            user_id = _as_int(target)
            user = self._get_user(account, user_id)
            if user is not None:
                callback(user, None)
                return
            callback(
                None,
                "Пользователь не в кэше. Открой профиль или укажи @username.",
            )
            return

        username = target.lstrip("@")
        if not USERNAME_RE.match(username):
            callback(None, "Некорректный username")
            return

        local = self._find_user_by_username(account, username)
        if local is not None:
            callback(local, None)
            return

        if send_request is None or TLRPC is None:
            callback(None, "Не удалось резолвить username в этой сборке")
            return

        try:
            req = TLRPC.TL_contacts_resolveUsername()
            req.username = username
        except Exception:
            callback(None, "Не удалось создать запрос resolveUsername")
            return

        def _done(response: Any, error: Any) -> None:
            def _ui() -> None:
                if error is not None:
                    text = getattr(error, "text", None) or "Пользователь не найден"
                    callback(None, _as_str(text))
                    return
                user = self._user_from_resolved(response)
                if user is None:
                    callback(None, "Пользователь не найден")
                    return
                callback(user, None)

            run_on_ui_thread(_ui)

        self._send_req(req, _done, account)

    def _user_from_resolved(self, response: Any) -> Any:
        if response is None:
            return None
        users = getattr(response, "users", None)
        for user in _java_list(users):
            if _is_tl_user(user) and not getattr(user, "bot", False):
                return user
        for user in _java_list(users):
            if _is_tl_user(user):
                return user
        peer = getattr(response, "peer", None)
        user_id = _as_int(getattr(peer, "user_id", 0))
        if user_id:
            return self._get_user(self._selected_account(), user_id)
        return None

    def _find_user_by_username(self, account: int, username: str) -> Any:
        mc = self._messages_controller(account)
        if mc is None:
            return None
        username_l = username.lower()
        for method_name in ("getUserOrChat", "getUser"):
            method = getattr(mc, method_name, None)
            if not callable(method):
                continue
            try:
                obj = method(username)
                if _is_tl_user(obj):
                    return obj
            except Exception:
                continue
        try:
            mapping = get_private_field(mc, "objectsByUsernames")
            if mapping is not None:
                obj = mapping.get(username_l)
                if obj is None:
                    obj = mapping.get(username)
                if _is_tl_user(obj):
                    return obj
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Apply / restore
    # ------------------------------------------------------------------

    def _apply_snos(self, account: int, user: Any, fragment: Any = None) -> bool:
        if not _is_tl_user(user):
            return False
        user_id = _as_int(getattr(user, "id", 0))
        if user_id <= 0:
            return False

        self._capture_original(account, user)
        self._mutate_user(user)
        user_full = self._get_user_full(account, user_id)
        if user_full is not None:
            self._capture_user_full(user_id, user_full)
            self._mutate_user_full(user_full)

        self._snos_ids.add(user_id)
        self._persist_store()
        self._commit_user(account, user)
        self._notify_ui(account, user_id, fragment)
        return True

    def _restore_user(
        self,
        user_id: int,
        persist: bool = True,
        notify: bool = False,
        account: Optional[int] = None,
        fragment: Any = None,
    ) -> bool:
        account = self._selected_account() if account is None else int(account)
        user = self._get_user(account, user_id)
        if user is not None:
            self._restore_user_fields(user)
        user_full = self._get_user_full(account, user_id)
        if user_full is not None:
            self._restore_user_full_fields(user_full)

        self._snos_ids.discard(int(user_id))
        self._records.pop(str(user_id), None)
        self._photo_refs.pop(int(user_id), None)
        self._status_refs.pop(int(user_id), None)
        self._usernames_refs.pop(int(user_id), None)
        self._emoji_refs.pop(int(user_id), None)
        self._phone_refs.pop(int(user_id), None)
        if user is not None:
            self._commit_user(account, user)
        if persist:
            self._persist_store(reload_settings=True)
        if notify:
            self._notify_ui(account, user_id, fragment)
        return True

    def _restore_all_memory(self, persist: bool) -> int:
        ids = list(self._snos_ids)
        for user_id in ids:
            try:
                self._restore_user(user_id, persist=False, notify=False)
            except Exception:
                _safe_log(f"restore {user_id} failed:\n{_format_exc()}")
        if persist:
            self._records = {}
            self._snos_ids = set()
            self._persist_store(reload_settings=True)
        return len(ids)

    def _reapply_all(self) -> None:
        if not self._feature_enabled():
            return
        self._reload_store()
        if not self._snos_ids:
            return
        for account in self._active_accounts():
            mc = self._messages_controller(account)
            if mc is None:
                continue
            for user_id in list(self._snos_ids):
                user = self._get_user(account, user_id)
                if user is not None:
                    self._mutate_user(user)
                    self._commit_user(account, user)
                user_full = self._get_user_full(account, user_id)
                if user_full is not None:
                    self._mutate_user_full(user_full)
            self._notify_ui(account)

    def _schedule_reapply(self) -> None:
        self._reapply_token += 1
        token = self._reapply_token

        def _run() -> None:
            if token == self._reapply_token:
                self._reapply_all()

        run_on_ui_thread(_run, 420)

    def _capture_original(self, account: int, user: Any) -> None:
        user_id = _as_int(getattr(user, "id", 0))
        key = str(user_id)
        existing = self._records.get(key, {})
        if existing and user_id in self._snos_ids:
            return
        incoming_is_fake = self._looks_already_mutated(user)
        incoming_is_min = bool(getattr(user, "min", False))

        if incoming_is_fake and existing:
            return
        if incoming_is_min and existing:
            return

        first = _as_str(getattr(user, "first_name", "") or "")
        last = _as_str(getattr(user, "last_name", "") or "")
        username = _as_str(getattr(user, "username", "") or "")
        display = (first + " " + last).strip() or username or existing.get("display") or str(user_id)

        photo = getattr(user, "photo", None)
        if photo is not None and not self._is_empty_photo(photo):
            self._photo_refs[user_id] = photo

        status = getattr(user, "status", None)
        if status is not None:
            self._status_refs[user_id] = status

        usernames = getattr(user, "usernames", None)
        if usernames is not None:
            self._usernames_refs[user_id] = usernames

        emoji = getattr(user, "emoji_status", None)
        if emoji is not None:
            self._emoji_refs[user_id] = emoji

        phone = getattr(user, "phone", None)
        if phone:
            self._phone_refs[user_id] = phone

        record = {
            "user_id": user_id,
            "account": account,
            "first_name": first,
            "last_name": last,
            "username": username,
            "phone": _as_str(phone or existing.get("phone") or ""),
            "display": display,
            "deleted": bool(getattr(user, "deleted", False)),
            "premium": bool(getattr(user, "premium", False)),
            "verified": bool(getattr(user, "verified", False)),
            "flags": _as_int(getattr(user, "flags", 0)),
            "flags2": _as_int(getattr(user, "flags2", 0)),
            "photo": self._dump_photo(photo),
            "about": existing.get("about", ""),
            "saved_at": int(time.time()),
        }
        if existing.get("about"):
            record["about"] = existing["about"]
        self._records[key] = record

    def _capture_user_full(self, user_id: int, user_full: Any) -> None:
        key = str(user_id)
        record = self._records.get(key)
        if record is None:
            return
        about = _as_str(getattr(user_full, "about", "") or "")
        if about and about != self._frozen_bio() and not record.get("about"):
            record["about"] = about
            self._records[key] = record

    def _looks_already_mutated(self, user: Any) -> bool:
        if not bool(getattr(user, "deleted", False)):
            return False
        if _as_str(getattr(user, "username", "") or "").strip():
            return False
        return self._is_empty_photo(getattr(user, "photo", None))

    def _mutate_user(self, user: Any) -> None:
        if not _is_tl_user(user):
            return
        if self._applying:
            return
        self._applying = True
        try:
            # Telegram draws deleted users only when user.deleted is the
            # primitive boolean. UserObject.isDeleted / getUserName / hasPhoto
            # and AvatarDrawable.setInfo all read these fields.
            self._set_field(user, "first_name", "")
            self._set_field(user, "last_name", "")
            self._set_field(user, "username", None)
            self._set_field(user, "phone", None)
            self._set_bool(user, "deleted", True)
            self._set_bool(user, "premium", False)
            self._set_bool(user, "verified", False)
            self._set_bool(user, "min", False)
            self._set_field(user, "photo", self._empty_photo())
            self._set_field(user, "emoji_status", None)
            self._set_field(user, "color", None)
            self._set_field(user, "profile_color", None)

            flags = _as_int(getattr(user, "flags", 0))
            flags |= FLAG_DELETED
            flags &= ~FLAG_FIRST_NAME
            flags &= ~FLAG_LAST_NAME
            flags &= ~FLAG_USERNAME
            flags &= ~FLAG_PHONE
            flags &= ~FLAG_PHOTO
            flags &= ~FLAG_USERNAMES
            self._set_int(user, "flags", flags)

            usernames = getattr(user, "usernames", None)
            if usernames is not None:
                try:
                    usernames.clear()
                except Exception:
                    self._set_field(user, "usernames", None)

            empty_status = self._empty_status()
            if empty_status is not None:
                self._set_field(user, "status", empty_status)
        finally:
            self._applying = False

    def _mutate_user_full(self, user_full: Any) -> None:
        if user_full is None:
            return
        # Frozen text is NOT the bio. Deleted/frozen profiles hide "О себе".
        # Keep about empty so Telegram does not render a description row.
        self._set_field(user_full, "about", "")
        for field in ("profile_photo", "personal_photo", "fallback_photo"):
            if hasattr(user_full, field):
                self._set_field(user_full, field, None)

    def _restore_user_fields(self, user: Any) -> None:
        if not _is_tl_user(user):
            return
        user_id = _as_int(getattr(user, "id", 0))
        record = self._records.get(str(user_id), {})

        self._set_field(user, "first_name", record.get("first_name") or "")
        self._set_field(user, "last_name", record.get("last_name") or "")
        username = record.get("username") or None
        self._set_field(user, "username", username)
        self._set_bool(user, "deleted", bool(record.get("deleted", False)))
        if "premium" in record:
            self._set_bool(user, "premium", bool(record.get("premium")))
        if "verified" in record:
            self._set_bool(user, "verified", bool(record.get("verified")))
        if "flags" in record:
            self._set_int(user, "flags", _as_int(record.get("flags"), 0))
        if "flags2" in record:
            self._set_int(user, "flags2", _as_int(record.get("flags2"), 0))
        phone = self._phone_refs.get(user_id) or record.get("phone")
        if phone:
            self._set_field(user, "phone", phone)

        photo = self._photo_refs.get(user_id)
        if photo is None:
            photo = self._load_photo(record.get("photo"))
        if photo is not None:
            self._set_field(user, "photo", photo)

        status = self._status_refs.get(user_id)
        if status is not None:
            self._set_field(user, "status", status)
        usernames = self._usernames_refs.get(user_id)
        if usernames is not None:
            self._set_field(user, "usernames", usernames)
        emoji = self._emoji_refs.get(user_id)
        if emoji is not None:
            self._set_field(user, "emoji_status", emoji)

    def _restore_user_full_fields(self, user_full: Any) -> None:
        user_id = _as_int(getattr(user_full, "id", 0))
        record = self._records.get(str(user_id), {})
        about = _as_str(record.get("about") or "")
        if about == self._frozen_bio():
            about = ""
        self._set_field(user_full, "about", about)

    def _maybe_touch_user(self, user: Any, account: Optional[int] = None) -> None:
        if not self._feature_enabled():
            return
        if not _is_tl_user(user):
            return
        user_id = _as_int(getattr(user, "id", 0))
        if user_id not in self._snos_ids:
            return
        if self._looks_already_mutated(user):
            return
        if account is None:
            account = self._account_of_controller(None) or self._selected_account()
        if not bool(getattr(user, "min", False)):
            self._capture_original(account, user)
        self._mutate_user(user)

    def _maybe_touch_user_full(self, user_full: Any) -> None:
        if not self._feature_enabled() or user_full is None:
            return
        user_id = _as_int(getattr(user_full, "id", 0) or getattr(user_full, "user_id", 0))
        if user_id not in self._snos_ids:
            return
        self._capture_user_full(user_id, user_full)
        self._mutate_user_full(user_full)

    def _maybe_touch_many(self, users: Any) -> None:
        if not self._feature_enabled() or not self._snos_ids:
            return
        for user in _java_list(users):
            self._maybe_touch_user(user)

    # ------------------------------------------------------------------
    # Java hooks
    # ------------------------------------------------------------------

    def _install_java_hooks(self) -> None:
        self._hook_all(
            "org.telegram.messenger.MessagesController",
            "putUser",
            after=self._after_put_user,
        )
        self._hook_all(
            "org.telegram.messenger.MessagesController",
            "putUsers",
            after=self._after_put_users,
        )
        self._hook_all(
            "org.telegram.messenger.MessagesController",
            "getUser",
            after=self._after_get_user,
        )
        self._hook_all(
            "org.telegram.messenger.MessagesController",
            "putUserFull",
            after=self._after_put_user_full,
        )
        self._hook_all(
            "org.telegram.messenger.MessagesController",
            "getUserFull",
            after=self._after_get_user_full,
        )
        self._hook_all(
            "org.telegram.ui.Components.AvatarDrawable",
            "setInfo",
            after=self._after_avatar_set_info,
        )
        # Real methods Telegram uses to render deleted users.
        self._hook_all(
            "org.telegram.messenger.UserObject",
            "isDeleted",
            after=self._after_user_is_deleted,
        )
        self._hook_all(
            "org.telegram.messenger.UserObject",
            "getUserName",
            after=self._after_user_get_name,
        )
        self._hook_all(
            "org.telegram.messenger.UserObject",
            "hasPhoto",
            after=self._after_user_has_photo,
        )
        self._hook_all(
            "org.telegram.ui.Components.ChatAvatarContainer",
            "setUserAvatar",
            after=self._after_set_user_avatar,
        )
        self._hook_all(
            "org.telegram.messenger.MessagesStorage",
            "putUsersAndChats",
            before=self._before_put_users_and_chats,
            after=self._after_put_users_and_chats,
        )
        self._hook_all(
            "org.telegram.messenger.MessagesStorage",
            "putUsersAndChatsInternal",
            before=self._before_put_users_and_chats,
            after=self._after_put_users_and_chats,
        )
        self._install_frozen_ui_hooks()

    def _hook_all(
        self,
        class_name: str,
        method_name: str,
        before: Optional[Callable[[Any], None]] = None,
        after: Optional[Callable[[Any], None]] = None,
    ) -> None:
        cls = find_class(class_name)
        if cls is None:
            _safe_log(f"class not found: {class_name}")
            return
        try:
            hooked = self.hook_all_methods(cls, method_name, _CallbackHook(before, after))
            count = len(hooked) if hooked else 0
            self.log(f"hooked {class_name}.{method_name} x{count}")
        except Exception:
            _safe_log(f"failed to hook {class_name}.{method_name}:\n{_format_exc()}")

    def _after_put_user(self, param: Any) -> None:
        args = getattr(param, "args", None)
        if not args:
            return
        user = args[0]
        account = self._account_of_controller(getattr(param, "thisObject", None))
        self._maybe_touch_user(user, account)

    def _after_put_users(self, param: Any) -> None:
        args = getattr(param, "args", None)
        if not args:
            return
        self._maybe_touch_many(args[0])

    def _after_get_user(self, param: Any) -> None:
        if not self._snos_ids:
            return
        result = param.getResult()
        self._maybe_touch_user(result, self._account_of_controller(getattr(param, "thisObject", None)))

    def _after_put_user_full(self, param: Any) -> None:
        args = getattr(param, "args", None)
        if not args:
            return
        self._maybe_touch_user_full(args[0])
        self._maybe_bump_gifts_count(args[0])

    def _after_get_user_full(self, param: Any) -> None:
        result = param.getResult()
        if self._snos_ids:
            self._maybe_touch_user_full(result)
        # Do not mutate UserFull on every getUserFull — that runs at
        # startup and a bad boxed int crashes the whole client.

    def _after_avatar_set_info(self, param: Any) -> None:
        if not self._feature_enabled() or not self._snos_ids:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        target = getattr(param, "thisObject", None)
        if target is None:
            return
        user = args[0]
        user_id = None
        if _is_tl_user(user):
            user_id = _as_int(getattr(user, "id", 0))
        elif isinstance(user, (int, float)):
            user_id = int(user)
        else:
            try:
                from java.lang import Number

                if isinstance(user, Number):
                    user_id = int(user.longValue())
            except Exception:
                user_id = None
        if user_id in self._snos_ids:
            self._set_bool(target, "drawDeleted", True)

    def _before_put_users_and_chats(self, param: Any) -> None:
        if not self._snos_ids:
            self._db_stack.append([])
            return
        args = getattr(param, "args", None)
        users = args[0] if args else None
        touched: List[Any] = []
        for user in _java_list(users):
            if not _is_tl_user(user):
                continue
            if _as_int(getattr(user, "id", 0)) not in self._snos_ids:
                continue
            self._restore_user_fields(user)
            touched.append(user)
        self._db_stack.append(touched)

    def _after_put_users_and_chats(self, param: Any) -> None:
        touched = self._db_stack.pop() if self._db_stack else []
        if not self._feature_enabled():
            return
        for user in touched:
            self._mutate_user(user)

    def _snos_id_from_user(self, user: Any) -> int:
        if _is_tl_user(user):
            return _as_int(getattr(user, "id", 0))
        return 0

    def _after_user_is_deleted(self, param: Any) -> None:
        if not self._feature_enabled() or not self._snos_ids:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        uid = self._snos_id_from_user(args[0])
        if uid in self._snos_ids:
            try:
                param.setResult(_jbool(True))
            except Exception:
                try:
                    param.setResult(True)
                except Exception:
                    pass

    def _after_user_get_name(self, param: Any) -> None:
        if not self._feature_enabled() or not self._snos_ids:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        uid = self._snos_id_from_user(args[0])
        if uid not in self._snos_ids:
            return
        name = self._hidden_name()
        if not name:
            return
        try:
            param.setResult(name)
        except Exception:
            pass

    def _after_user_has_photo(self, param: Any) -> None:
        if not self._feature_enabled() or not self._snos_ids:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        uid = self._snos_id_from_user(args[0])
        if uid in self._snos_ids:
            try:
                param.setResult(_jbool(False))
            except Exception:
                try:
                    param.setResult(False)
                except Exception:
                    pass

    def _after_set_user_avatar(self, param: Any) -> None:
        if not self._feature_enabled() or not self._snos_ids:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        user = args[0]
        uid = self._snos_id_from_user(user)
        if uid not in self._snos_ids:
            return
        self._mutate_user(user)
        target = getattr(param, "thisObject", None)
        drawable = None
        try:
            drawable = get_private_field(target, "avatarDrawable")
        except Exception:
            drawable = getattr(target, "avatarDrawable", None) if target is not None else None
        if drawable is not None:
            self._set_bool(drawable, "drawDeleted", True)
            self._call_set_info(drawable, user)

    def _hidden_name(self) -> str:
        try:
            from org.telegram.messenger import LocaleController

            try:
                from org.telegram.messenger import R

                value = LocaleController.getString(R.string.HiddenName)
                if value:
                    return _as_str(value)
            except Exception:
                pass
            for args in (("HiddenName",), ("HiddenName", 0)):
                try:
                    value = LocaleController.getString(*args)
                    if value:
                        return _as_str(value)
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    def _commit_user(self, account: int, user: Any) -> None:
        if user is None:
            return
        mc = self._messages_controller(account)
        if mc is None:
            return
        method = getattr(mc, "putUser", None)
        if not callable(method):
            return
        for args in ((user, False), (user, False, True)):
            try:
                method(*args)
                return
            except Exception:
                continue

    def _install_frozen_ui_hooks(self) -> None:
        self._hook_all(
            "org.telegram.ui.ProfileActivity",
            "updateProfileData",
            after=self._after_update_profile_data,
        )
        self._hook_all(
            "org.telegram.ui.ChatActivity",
            "updateSubtitle",
            after=self._after_chat_update_subtitle,
        )

    def _after_update_profile_data(self, param: Any) -> None:
        self._nudge_native_deleted(getattr(param, "thisObject", None))

    def _after_chat_update_subtitle(self, param: Any) -> None:
        self._nudge_native_deleted(getattr(param, "thisObject", None))

    def _nudge_native_deleted(self, fragment: Any) -> None:
        """Ask Telegram to redraw via its own AvatarDrawable.setInfo / updateAvatar."""
        if fragment is None or not self._feature_enabled() or not self._snos_ids:
            return
        user_id = self._ui_user_id(fragment)
        if user_id not in self._snos_ids:
            return
        user = self._get_user(self._selected_account(), user_id)
        if user is not None:
            self._mutate_user(user)
            self._call_set_info(fragment, user)
        for name in ("avatarDrawable", "avatar", "avatarImage"):
            target = None
            try:
                target = get_private_field(fragment, name)
            except Exception:
                target = getattr(fragment, name, None)
            self._call_set_info(target, user)
        avatar = getattr(fragment, "avatarContainer", None)
        if avatar is None:
            try:
                avatar = get_private_field(fragment, "avatarContainer")
            except Exception:
                avatar = None
        if avatar is None:
            return
        self._call_set_info(avatar, user)
        for name in ("avatarDrawable", "avatarImageView"):
            child = None
            try:
                child = get_private_field(avatar, name)
            except Exception:
                child = getattr(avatar, name, None)
            self._call_set_info(child, user)
        for name in ("checkAndUpdateAvatar", "updateAvatar"):
            method = getattr(avatar, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    continue

    def _call_set_info(self, obj: Any, user: Any) -> None:
        if obj is None or user is None:
            return
        method = getattr(obj, "setInfo", None)
        if not callable(method):
            return
        try:
            method(user)
            return
        except Exception:
            pass
        try:
            method(int(self._selected_account()), user)
        except Exception:
            pass

    def _ui_user_id(self, obj: Any) -> int:
        if obj is None:
            return 0
        for name in ("userId", "user_id", "dialogId", "dialog_id"):
            value = None
            try:
                value = get_private_field(obj, name)
            except Exception:
                value = getattr(obj, name, None)
            uid = _as_int(value)
            if uid > 0:
                return uid
        for name in ("user", "currentUser", "chatUser"):
            user = None
            try:
                user = get_private_field(obj, name)
            except Exception:
                user = getattr(obj, name, None)
            if _is_tl_user(user):
                return _as_int(getattr(user, "id", 0))
        parent = None
        try:
            parent = get_private_field(obj, "parentFragment")
        except Exception:
            parent = getattr(obj, "parentFragment", None)
        if parent is not None and parent is not obj:
            return self._ui_user_id(parent)
        return 0

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _reload_store(self) -> None:
        raw = self.get_setting(SNOS_STORE_KEY, "{}")
        data: Dict[str, Any] = {}
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                _safe_log(f"bad snos store, resetting:\n{_format_exc()}")
                data = {}
        normalized: Dict[str, Dict[str, Any]] = {}
        ids: Set[int] = set()
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            user_id = _as_int(value.get("user_id") or key)
            if user_id <= 0:
                continue
            value = dict(value)
            value["user_id"] = user_id
            normalized[str(user_id)] = value
            ids.add(user_id)
        self._records = normalized
        self._snos_ids = ids

    def _persist_store(self, reload_settings: bool = False) -> None:
        try:
            payload = json.dumps(self._records, ensure_ascii=False)
            self.set_setting(SNOS_STORE_KEY, payload, reload_settings=reload_settings)
        except TypeError:
            try:
                self.set_setting(SNOS_STORE_KEY, json.dumps(self._records, ensure_ascii=False))
            except Exception:
                _safe_log(f"persist failed:\n{_format_exc()}")
        except Exception:
            _safe_log(f"persist failed:\n{_format_exc()}")

    def _sorted_record_keys(self) -> List[str]:
        def _sort_key(key: str) -> Any:
            record = self._records.get(key, {})
            return (-_as_int(record.get("saved_at")), key)

        return sorted(self._records.keys(), key=_sort_key)

    def _original_display(self, user_id: int) -> str:
        record = self._records.get(str(user_id), {})
        return _as_str(record.get("display") or record.get("first_name") or "")

    # ------------------------------------------------------------------
    # Local gifts — store / settings / UI
    # ------------------------------------------------------------------

    def _gifts_enabled(self) -> bool:
        return bool(self.get_setting(GIFTS_ENABLED_KEY, True))

    def _reload_gifts(self) -> None:
        raw = self.get_setting(GIFTS_STORE_KEY, "[]")
        data: List[Any] = []
        if isinstance(raw, list):
            data = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    data = parsed
            except Exception:
                _safe_log(f"bad gifts store, resetting:\n{_format_exc()}")
                data = []
        cleaned: List[Dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            rec = dict(item)
            rec["id"] = _as_str(rec.get("id") or self._new_gift_id())
            rec["msg_id"] = _as_int(rec.get("msg_id"))
            rec["saved_id"] = _as_int(rec.get("saved_id"))
            rec["gift_id"] = _as_int(rec.get("gift_id"))
            rec["upgraded"] = bool(rec.get("upgraded"))
            if not rec["msg_id"] or not (LOCAL_MSG_MIN <= rec["msg_id"] <= LOCAL_MSG_MAX):
                rec["msg_id"] = self._next_msg_id(cleaned)
            if rec["saved_id"] >= 0 or rec["saved_id"] > LOCAL_SAVED_BASE:
                rec["saved_id"] = LOCAL_SAVED_BASE - (len(cleaned) + 1)
            if not rec.get("slug"):
                rec["slug"] = LOCAL_SLUG_PREFIX + rec["id"]
            cleaned.append(rec)
        self._gifts = cleaned

    def _persist_gifts(self, reload_settings: bool = False) -> None:
        try:
            payload = json.dumps(self._gifts, ensure_ascii=False)
            self.set_setting(GIFTS_STORE_KEY, payload, reload_settings=reload_settings)
        except TypeError:
            try:
                self.set_setting(GIFTS_STORE_KEY, json.dumps(self._gifts, ensure_ascii=False))
            except Exception:
                _safe_log(f"persist gifts failed:\n{_format_exc()}")
        except Exception:
            _safe_log(f"persist gifts failed:\n{_format_exc()}")

    def _new_gift_id(self) -> str:
        return f"g{int(time.time() * 1000) % 1000000000}{random.randint(10, 99)}"

    def _next_msg_id(self, existing: Optional[List[Dict[str, Any]]] = None) -> int:
        used = {_as_int(item.get("msg_id")) for item in (existing if existing is not None else self._gifts)}
        value = LOCAL_MSG_MIN + 1
        while value in used and value <= LOCAL_MSG_MAX:
            value += 1
        return value

    def _gift_by_id(self, gift_id: str) -> Optional[Dict[str, Any]]:
        for rec in self._gifts:
            if rec.get("id") == gift_id:
                return rec
        return None

    def _settings_input(self, key: str, text: str, subtext: str) -> Any:
        for kwargs in (
            dict(key=key, text=text, default="", subtext=subtext, icon="msg_search"),
            dict(key=key, text=text, default="", subtext=subtext),
            dict(key=key, text=text, default=""),
        ):
            try:
                return Input(**kwargs)
            except TypeError:
                continue
            except Exception:
                continue
        return Text(text=text, subtext=subtext, icon="msg_search")

    def _gift_settings_rows(self) -> List[Any]:
        rows: List[Any] = [
            Divider(text="Локальные подарки"),
            Switch(
                key=GIFTS_ENABLED_KEY,
                text="Показывать на своём профиле",
                default=True,
                subtext="Только у тебя. Настоящие подарки плагин не трогает",
                icon="msg_gift",
            ),
            self._settings_input(
                GIFT_SEARCH_KEY,
                "Поиск подарка",
                "Название или эмодзи — потом нажми «Найти»",
            ),
            Text(
                text="Найти и добавить",
                subtext="Только обычные (неулучшенные) подарки из каталога",
                icon="msg_search",
                on_click=lambda _view=None: self._start_add_gift(search=True),
            ),
            Text(
                text="Все улучшаемые",
                subtext="Pepe, духи, жабы, книги, ручки — поиск внутри списка",
                icon="msg_add",
                on_click=lambda _view=None: self._start_add_gift(),
            ),
            Text(
                text="Безопасность",
                subtext="Локальные подарки нельзя продать, передать или улучшить за Stars. Реальные подарки не затрагиваются.",
                icon="msg_info",
            ),
        ]
        if not self._gifts:
            rows.append(
                Text(
                    text="Пока нет локальных подарков",
                    subtext="Свой профиль → ⋮ → Подарки  или кнопка выше",
                    icon="msg_info",
                )
            )
            return rows

        for rec in reversed(self._gifts):
            rows.append(
                Text(
                    text=self._gift_title(rec),
                    subtext=self._gift_subtitle(rec),
                    icon="msg_fave",
                    on_click=self._gift_click_handler(str(rec.get("id"))),
                )
            )
        rows.append(
            Text(
                text="Удалить все локальные",
                subtext=f"Убрать {len(self._gifts)} подарок(ов) только из этого клиента",
                icon="msg_delete",
                red=True,
                on_click=lambda _view=None: self._confirm_clear_gifts(),
            )
        )
        return rows

    def _gift_title(self, rec: Dict[str, Any]) -> str:
        emoji = _as_str(rec.get("emoji") or "").strip()
        name = _as_str(rec.get("title") or "Подарок").strip() or "Подарок"
        return f"{emoji} {name}".strip()

    def _gift_subtitle(self, rec: Dict[str, Any]) -> str:
        if rec.get("upgraded"):
            unique = rec.get("unique") or {}
            num = _as_int(unique.get("num"))
            model = _as_str(unique.get("model") or "—")
            return f"Коллекционный #{num}  ·  {model}  ·  нажми, чтобы открыть"
        return "Обычный  ·  можно улучшить локально  ·  нажми"

    def _gift_click_handler(self, gift_id: str) -> Callable[..., None]:
        def _on_click(_view: Any = None) -> None:
            rec = self._gift_by_id(gift_id)
            if rec is None:
                self._toast_info("Подарок уже удалён")
                return
            self._open_local_gift_dialog(rec)

        return _on_click

    def _on_gifts_menu_click(self, context: Dict[str, Any]) -> None:
        try:
            user = context.get("user")
            account = _as_int(context.get("account"), self._selected_account())
            user_id = _as_int(getattr(user, "id", 0) if user is not None else 0)
            if user_id and not self._is_me(account, user_id) and not getattr(user, "self", False):
                self._toast_info("Локальные подарки добавляются только на свой профиль")
                return
            self._open_gifts_manager()
        except Exception:
            _safe_log(f"gifts menu failed:\n{_format_exc()}")
            self._toast_error("Не удалось открыть подарки")

    def _open_gifts_manager(self) -> None:
        self._reload_gifts()
        items = ["Добавить улучшаемый подарок", "Найти по названию"]
        ids: List[Optional[str]] = [None, "__search__"]
        for rec in reversed(self._gifts):
            mark = "★ " if rec.get("upgraded") else ""
            items.append(f"{mark}{self._gift_title(rec)}")
            ids.append(str(rec.get("id")))
        if not self._gifts:
            items.append("Пока пусто — сначала добавь подарок")
            ids.append("__empty__")

        def _picked(builder: Any, which: int) -> None:
            try:
                builder.dismiss()
            except Exception:
                pass
            if which < 0 or which >= len(ids):
                return
            chosen = ids[which]
            if chosen is None:
                self._start_add_gift()
                return
            if chosen == "__search__":
                self._start_add_gift(search=True)
                return
            if chosen == "__empty__":
                return
            rec = self._gift_by_id(chosen)
            if rec is not None:
                self._open_local_gift_dialog(rec)

        self._show_items("Локальные подарки", items, _picked)

    def _start_add_gift(self, search: bool = False) -> None:
        if not self._gifts_enabled():
            self._toast_error("Локальные подарки выключены в настройках")
            return
        spinner = self._show_spinner("Каталог подарков", "Собираю неулучшенные подарки…")
        account = self._selected_account()

        def _after(ok: bool, error: Optional[str]) -> None:
            self._dismiss_quiet(spinner)
            if not ok:
                self._toast_error(error or "Не удалось загрузить каталог")
                return
            if search:
                self._prompt_gift_search()
                return
            self._show_catalog_categories()

        self._ensure_catalog(account, _after)

    def _base_catalog(self) -> List[Any]:
        result: List[Any] = []
        seen: Set[int] = set()
        for gift in list(self._catalog or []):
            if self._gift_is_unique_obj(gift):
                continue
            gid = _as_int(getattr(gift, "id", 0))
            if gid <= 0:
                continue
            if gid in seen:
                continue
            seen.add(gid)
            result.append(gift)

        def _sort_key(gift: Any) -> Any:
            upgradeable = 0 if self._gift_can_upgrade(gift) else 1
            limited = 0 if bool(getattr(gift, "limited", False)) or bool(getattr(gift, "sold_out", False)) else 1
            sold = 0 if bool(getattr(gift, "sold_out", False)) else 1
            title = self._gift_display_name(gift).lower()
            return (upgradeable, limited, sold, title)

        result.sort(key=_sort_key)
        return result

    def _upgradeable_catalog(self) -> List[Any]:
        return [g for g in self._base_catalog() if self._gift_can_upgrade(g)]

    def _show_catalog_categories(self) -> None:
        gifts = self._base_catalog()
        if not gifts:
            self._toast_error("Каталог пуст — открой в Telegram «Отправить подарок» и попробуй снова")
            return
        upgradeable = self._upgradeable_catalog() or gifts
        rows: List[str] = [
            f"🔎 Поиск по названию",
            f"Все улучшаемые  ·  {len(upgradeable)}",
        ]
        buckets: List[Any] = ["__search__", upgradeable]
        sold = [g for g in upgradeable if bool(getattr(g, "sold_out", False)) or bool(getattr(g, "limited", False))]
        if sold:
            rows.append(f"Лимитки / sold out  ·  {len(sold)}")
            buckets.append(sold)
        for title, keys in GIFT_CATEGORIES:
            matched = [g for g in upgradeable if self._gift_matches_keys(g, keys)]
            if not matched:
                continue
            rows.append(f"{title}  ·  {len(matched)}")
            buckets.append(matched)

        def _picked(builder: Any, which: int) -> None:
            self._dismiss_quiet(builder)
            if which < 0 or which >= len(buckets):
                return
            chosen = buckets[which]
            if chosen == "__search__":
                self._prompt_gift_search()
                return
            self._show_catalog_picker(chosen, rows[which])

        self._show_items("Неулучшенный подарок на профиль", rows, _picked)

    def _prompt_gift_search(self) -> None:
        preset = _as_str(self.get_setting(GIFT_SEARCH_KEY, "")).strip()

        def _run(query: str) -> None:
            q = _as_str(query or "").strip()
            try:
                self.set_setting(GIFT_SEARCH_KEY, q)
            except Exception:
                pass
            self._show_search_results(q)

        if preset:
            self._prompt_text("Поиск подарка", "Название или эмодзи", _run, preset)
            return
        self._prompt_text("Поиск подарка", "Название или эмодзи", _run, "")

    def _show_search_results(self, query: str) -> None:
        q = _as_str(query).strip().lower()
        pool = self._upgradeable_catalog() or self._base_catalog()
        if q:
            pool = [g for g in pool if q in self._catalog_label(g).lower() or q in self._gift_display_name(g).lower()]
        if not pool:
            self._toast_info("Ничего не нашёл. Попробуй другое название")
            run_on_ui_thread(self._prompt_gift_search, 250)
            return
        heading = f"Поиск: {query}" if query else "Все улучшаемые"
        self._show_catalog_picker(pool, heading)

    def _show_catalog_picker(self, gifts: Optional[List[Any]] = None, heading: str = "Выбери подарок") -> None:
        pool = list(gifts or self._upgradeable_catalog() or self._base_catalog())
        if not pool:
            self._toast_error("В этой категории пока пусто")
            return
        self._picker_gifts = pool
        self._picker_heading = heading
        labels = [self._catalog_label(gift) for gift in pool]
        title = heading.split("  ·  ")[0] if heading else "Выбери подарок"
        if len(labels) > 80:
            labels = labels[:80]
            labels.append("… ещё, воспользуйся поиском")

        def _picked(builder: Any, which: int) -> None:
            self._dismiss_quiet(builder)
            chosen = list(self._picker_gifts)
            if which == 80 or (0 <= which < len(labels) and labels[which].startswith("…")):
                self._prompt_gift_search()
                return
            if 0 <= which < len(chosen):
                self._add_local_gift(chosen[which], again=True, reopen=list(chosen), heading=heading)

        self._show_items(title, labels, _picked)

    def _gift_matches_keys(self, gift: Any, keys: Any) -> bool:
        hay = " ".join(
            [
                self._gift_display_name(gift),
                self._sticker_emoji(getattr(gift, "sticker", None)),
                _as_str(getattr(gift, "title", "") or ""),
            ]
        ).lower()
        return any(key in hay for key in keys)

    def _gift_display_name(self, gift: Any) -> str:
        title = _as_str(getattr(gift, "title", "") or "").strip()
        if title:
            return title
        emoji = self._sticker_emoji(getattr(gift, "sticker", None))
        if emoji and emoji != "🎁":
            return emoji
        return "Подарок"

    def _gift_can_upgrade(self, gift: Any) -> bool:
        if self._gift_is_unique_obj(gift):
            return False
        if _as_int(getattr(gift, "upgrade_stars", 0)) > 0:
            return True
        if _as_int(getattr(gift, "upgrade_variants", 0)) > 0:
            return True
        if bool(getattr(gift, "sold_out", False)) and _as_str(getattr(gift, "title", "") or "").strip():
            return True
        return bool(getattr(gift, "limited", False))

    def _catalog_label(self, gift: Any) -> str:
        emoji = self._sticker_emoji(getattr(gift, "sticker", None))
        title = self._gift_display_name(gift)
        bits = [f"{emoji} {title}".strip()]
        if self._gift_can_upgrade(gift):
            bits.append("можно улучшить")
        if bool(getattr(gift, "sold_out", False)):
            bits.append("sold out")
        stars = _as_int(getattr(gift, "stars", 0))
        if stars:
            bits.append(f"{stars}★")
        return "  ·  ".join(bits)

    def _sticker_emoji(self, sticker: Any) -> str:
        if sticker is None:
            return "🎁"
        for name in ("alt", "emoticon", "emoji"):
            value = _as_str(getattr(sticker, name, "") or "").strip()
            if value:
                return value
        return "🎁"

    def _add_local_gift(
        self,
        catalog_gift: Any,
        again: bool = False,
        reopen: Optional[List[Any]] = None,
        heading: str = "",
    ) -> None:
        gift_id = _as_int(getattr(catalog_gift, "id", 0))
        if gift_id <= 0:
            self._toast_error("У подарка нет id")
            return
        rec = {
            "id": self._new_gift_id(),
            "gift_id": gift_id,
            "msg_id": self._next_msg_id(),
            "saved_id": LOCAL_SAVED_BASE - (len(self._gifts) + 1),
            "title": self._gift_display_name(catalog_gift),
            "emoji": self._sticker_emoji(getattr(catalog_gift, "sticker", None)),
            "stars": _as_int(getattr(catalog_gift, "stars", 0)),
            "limited": bool(getattr(catalog_gift, "limited", False)),
            "can_upgrade": True,
            "upgraded": False,
            "unique": {},
            "added_at": int(time.time()),
        }
        rec["slug"] = LOCAL_SLUG_PREFIX + rec["id"]
        self._catalog_by_id[gift_id] = catalog_gift
        self._gifts.append(rec)
        try:
            self._persist_gifts(reload_settings=False)
        except Exception:
            _safe_log(f"persist after add failed:\n{_format_exc()}")
        try:
            self._refresh_gifts_ui()
        except Exception:
            _safe_log(f"refresh after add failed:\n{_format_exc()}")
        self._toast_success(f"{self._gift_title(rec)} на профиле. Можно добавить ещё")
        if again:
            if reopen:
                run_on_ui_thread(
                    lambda pool=list(reopen), h=heading or "Выбери подарок": self._show_catalog_picker(pool, h),
                    280,
                )
            else:
                run_on_ui_thread(self._show_catalog_categories, 280)

    def _open_local_gift_dialog(self, rec: Dict[str, Any]) -> None:
        unique = rec.get("unique") or {}
        if rec.get("upgraded"):
            body = (
                f"{self._gift_title(rec)}\n\n"
                f"Коллекционный подарок #{_as_int(unique.get('num'))}\n"
                f"Модель: {unique.get('model') or '—'}  ({unique.get('model_rarity') or '—'}%)\n"
                f"Фон: {unique.get('backdrop') or '—'}  ({unique.get('backdrop_rarity') or '—'}%)\n"
                f"Узор: {unique.get('pattern') or '—'}  ({unique.get('pattern_rarity') or '—'}%)\n\n"
                "Только локально. Продажа, перевод и вывод в блокчейн отключены."
            )
            self._show_confirm(
                "Локальный NFT",
                body,
                "Удалить",
                lambda b, _w: self._delete_local_gift(str(rec.get("id")), b),
            )
            return

        body = (
            f"{self._gift_title(rec)}\n\n"
            "Обычный подарок — можно улучшить до коллекционного.\n"
            "Улучшение бесплатное и только в этом клиенте.\n"
            "Настоящие подарки и Stars не затрагиваются."
        )

        def _upgrade(builder: Any, _which: Any = None) -> None:
            self._dismiss_quiet(builder)
            self._upgrade_local_gift(str(rec.get("id")))

        activity = self._activity()
        if activity is None:
            _upgrade(None)
            return
        try:
            builder = AlertDialogBuilder(activity)
            builder.set_title("Локальный подарок")
            builder.set_message(body)
            builder.set_positive_button("Улучшить", _upgrade)
            builder.set_neutral_button(
                "Удалить",
                lambda b, _w: self._delete_local_gift(str(rec.get("id")), b),
            )
            builder.set_negative_button("Закрыть", lambda b, _w: b.dismiss())
            try:
                builder.make_button_red(AlertDialogBuilder.BUTTON_NEUTRAL)
            except Exception:
                pass
            builder.show()
        except Exception:
            _safe_log(f"gift dialog failed:\n{_format_exc()}")
            self._upgrade_local_gift(str(rec.get("id")))

    def _delete_local_gift(self, gift_id: str, builder: Any = None) -> None:
        self._dismiss_quiet(builder)
        before = len(self._gifts)
        self._gifts = [rec for rec in self._gifts if rec.get("id") != gift_id]
        if len(self._gifts) == before:
            self._toast_info("Подарок уже удалён")
            return
        self._persist_gifts(reload_settings=True)
        self._refresh_gifts_ui()
        self._toast_success("Локальный подарок удалён")

    def _confirm_clear_gifts(self) -> None:
        if not self._gifts:
            self._toast_info("Удалять нечего")
            return

        def _ok(builder: Any = None, _which: Any = None) -> None:
            self._gifts = []
            self._persist_gifts(reload_settings=True)
            self._refresh_gifts_ui()
            self._dismiss_quiet(builder)
            self._toast_success("Локальные подарки удалены")

        self._show_confirm(
            "Удалить все локальные?",
            "Настоящие подарки останутся как были.",
            "Удалить",
            _ok,
        )

    def _upgrade_local_gift(self, gift_id: str) -> None:
        rec = self._gift_by_id(gift_id)
        if rec is None:
            self._toast_error("Подарок не найден")
            return
        if rec.get("upgraded"):
            self._toast_info("Этот подарок уже коллекционный")
            return
        unique = self._roll_unique(rec)
        rec["unique"] = unique
        rec["upgraded"] = True
        rec["upgraded_at"] = int(time.time())
        catalog = self._catalog_by_id.get(_as_int(rec.get("gift_id")))
        attrs = self._synthetic_unique_attrs(rec, catalog)
        if attrs is not None:
            self._unique_attr_refs[str(rec.get("id"))] = attrs
        self._persist_gifts()
        self._refresh_gifts_ui()
        self._play_upgrade_animation(rec, unique)

        def _apply_preview(meta: Dict[str, Any], preview_attrs: Any = None) -> None:
            live = self._gift_by_id(gift_id)
            if live is None or not live.get("upgraded"):
                return
            if meta:
                live["unique"] = meta
            if preview_attrs is not None:
                self._unique_attr_refs[str(live.get("id"))] = preview_attrs
            self._persist_gifts()
            self._refresh_gifts_ui()

        try:
            self._fetch_upgrade_preview(rec, _apply_preview)
        except Exception:
            _safe_log(f"upgrade preview failed:\n{_format_exc()}")

    def _roll_unique(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        seed = _as_str(rec.get("id")) + str(rec.get("gift_id"))
        rng = random.Random(seed)
        model, model_r = rng.choice(UNIQUE_MODELS)
        backdrop, back_r = rng.choice(UNIQUE_BACKDROPS)
        pattern, pat_r = rng.choice(UNIQUE_PATTERNS)
        num = rng.randint(17, 49999)
        return {
            "title": rec.get("title") or "Collectible",
            "slug": rec.get("slug") or (LOCAL_SLUG_PREFIX + rec["id"]),
            "num": num,
            "model": model,
            "model_rarity": model_r,
            "backdrop": backdrop,
            "backdrop_rarity": back_r,
            "pattern": pattern,
            "pattern_rarity": pat_r,
            "issued": max(num, 1000),
            "total": 50000,
        }

    def _play_upgrade_animation(self, rec: Dict[str, Any], unique: Dict[str, Any]) -> None:
        spinner = self._show_spinner("Улучшение подарка", "Собираем уникальные атрибуты…")
        steps = [
            (550, "Подбираем модель…"),
            (1200, f"Модель: {unique.get('model')}  ({unique.get('model_rarity')}%)"),
            (1850, f"Фон: {unique.get('backdrop')}  ({unique.get('backdrop_rarity')}%)"),
            (2500, f"Узор: {unique.get('pattern')}  ({unique.get('pattern_rarity')}%)"),
        ]

        def _set_msg(text: str) -> None:
            try:
                if spinner is not None:
                    spinner.set_message(text)
            except Exception:
                pass

        for delay, text in steps:
            run_on_ui_thread(lambda t=text: _set_msg(t), delay)

        def _done() -> None:
            self._dismiss_quiet(spinner)
            title = self._gift_title(rec)
            num = _as_int(unique.get("num"))
            self._toast_success(f"{title} стал коллекционным #{num}")

        run_on_ui_thread(_done, 3200)

    # ------------------------------------------------------------------
    # Local gifts — catalog / TL inject / safety
    # ------------------------------------------------------------------

    def _prefetch_catalog(self) -> None:
        if not self._gifts_enabled():
            return
        self._ensure_catalog(self._selected_account(), lambda _ok, _err: None)

    def _ingest_catalog(self, gifts: Any) -> int:
        added = 0
        for gift in _java_list(gifts):
            gid = _as_int(getattr(gift, "id", 0) or getattr(gift, "gift_id", 0))
            if gid <= 0:
                continue
            existing = self._catalog_by_id.get(gid)
            if existing is not None and self._gift_is_unique_obj(gift) and not self._gift_is_unique_obj(existing):
                continue
            if existing is None:
                self._catalog.append(gift)
                added += 1
            self._catalog_by_id[gid] = gift if not self._gift_is_unique_obj(gift) else existing or gift
        if self._catalog:
            self._catalog_at = time.time()
        return added

    def _catalog_from_controller(self, account: int) -> int:
        added = 0
        for class_name in (
            "org.telegram.messenger.StarsController",
            "org.telegram.ui.Stars.StarsController",
        ):
            cls = find_class(class_name)
            if cls is None:
                continue
            inst = None
            for method_name in ("getInstance", "Instance"):
                method = getattr(cls, method_name, None)
                if not callable(method):
                    continue
                try:
                    inst = method(int(account))
                    break
                except Exception:
                    try:
                        inst = method()
                        break
                    except Exception:
                        continue
            if inst is None:
                continue
            for field in (
                "gifts",
                "sortedGifts",
                "starGifts",
                "allGifts",
                "giftsList",
                "birthdayGifts",
                "auctionGifts",
            ):
                try:
                    value = get_private_field(inst, field)
                except Exception:
                    value = getattr(inst, field, None)
                if value is None:
                    continue
                added += self._ingest_catalog(value)
                nested = getattr(value, "gifts", None)
                if nested is not None and nested is not value:
                    added += self._ingest_catalog(nested)
            for method_name in ("loadStarGifts", "getStarGifts", "loadGifts"):
                method = getattr(inst, method_name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:
                        try:
                            method(False)
                        except Exception:
                            pass
        return added

    def _ensure_catalog(self, account: int, callback: Callable[[bool, Optional[str]], None]) -> None:
        answered = {"done": False}

        def _once(ok: bool, error: Optional[str]) -> None:
            if answered["done"]:
                return
            answered["done"] = True
            callback(ok, error)

        local = self._catalog_from_controller(account)
        ready = bool(self._base_catalog())
        if ready and (time.time() - self._catalog_at) < 600:
            _once(True, None)
            return
        if self._catalog_loading:
            run_on_ui_thread(lambda: self._ensure_catalog(account, callback), 400)
            return
        req = self._new_tl(TL_GET_GIFTS_NAMES)
        if req is None and TLRPC is not None:
            for name in (
                "TL_payments_getStarGifts",
                "TL_starGiftsGetStarGifts",
                "TL_stars_getStarGifts",
            ):
                ctor = getattr(TLRPC, name, None)
                if callable(ctor):
                    try:
                        req = ctor()
                        break
                    except Exception:
                        continue
        if req is None:
            if ready:
                _once(True, None)
                return
            _once(False, "Не удалось создать запрос каталога. Открой «Отправить подарок» в Telegram и повтори.")
            return
        try:
            if hasattr(req, "hash"):
                self._set_long(req, "hash", 0)
        except Exception:
            pass
        self._catalog_loading = True

        def _done(response: Any, error: Any) -> None:
            self._catalog_loading = False

            def _ui() -> None:
                if response is not None:
                    self._ingest_catalog(getattr(response, "gifts", None))
                self._catalog_from_controller(account)
                if self._base_catalog():
                    _once(True, None)
                    return
                if error is not None:
                    _once(False, _as_str(getattr(error, "text", None) or "Каталог недоступен"))
                    return
                _once(False, "Каталог пуст")

            run_on_ui_thread(_ui)

        self._send_req(req, _done, account)
        if ready:
            _once(True, None)

    def _install_request_hooks(self) -> None:
        names = list(GIFT_MUTATION_HINTS) + list(PAYMENT_HINTS) + list(LIST_HINTS)
        names.append("getStarGifts")
        names.append("upgradeStarGift")
        names.append("getStarGiftUpgradePreview")
        for name in names:
            self._add_req_hook(name)
        # Do not hook NotificationCenter: injecting during notifyUpdate
        # re-enters the list while the adapter is binding and crashes.

    def _add_req_hook(self, name: str) -> None:
        try:
            self.add_hook(name, match_substring=True)
            return
        except TypeError:
            pass
        except Exception:
            _safe_log(f"add_hook({name}) failed:\n{_format_exc()}")
            return
        try:
            self.add_hook(name)
        except Exception:
            _safe_log(f"add_hook retry {name} failed:\n{_format_exc()}")

    def pre_request_hook(self, request_name: str, account: int, request: Any) -> HookResult:
        try:
            if "getSavedStarGifts" in _as_str(request_name):
                self._last_gifts_self = self._request_is_self_gifts(account, request)
                self._last_gifts_filters = {
                    "exclude_unique": bool(getattr(request, "exclude_unique", False)),
                    "exclude_upgradable": bool(getattr(request, "exclude_upgradable", False)),
                    "exclude_unupgradable": bool(getattr(request, "exclude_unupgradable", False)),
                    "exclude_unlimited": bool(getattr(request, "exclude_unlimited", False)),
                }
                return HookResult()
            if self._name_has(request_name, GIFT_MUTATION_HINTS) or self._name_has(request_name, PAYMENT_HINTS):
                rec = self._local_record_from_obj(request)
                if rec is None:
                    return HookResult()
                name = _as_str(request_name).lower()
                if "preview" in name:
                    return HookResult()
                # Never let upgrade/payment for a local gift hit Telegram.
                # Native UI is driven by void openUpgrade/doUpgrade hooks.
                self._on_blocked_local_request(request_name, rec)
                return HookResult(strategy=HookStrategy.CANCEL)
        except Exception:
            _safe_log(f"pre_request failed:\n{_format_exc()}")
        return HookResult()

    def post_request_hook(self, request_name: str, account: int, response: Any, error: Any) -> HookResult:
        try:
            if error or response is None:
                return HookResult()
            if "getStarGifts" in _as_str(request_name) and "Saved" not in _as_str(request_name):
                self._ingest_catalog(getattr(response, "gifts", None))
                return HookResult()
            if not self._gifts_enabled() or not self._gifts:
                return HookResult()
            if "getSavedStarGifts" not in _as_str(request_name):
                return HookResult()
            if not self._last_gifts_self:
                return HookResult()
            if self._inject_local_gifts(response):
                return HookResult(strategy=HookStrategy.MODIFY, response=response)
        except Exception:
            _safe_log(f"post_request failed:\n{_format_exc()}")
        return HookResult()

    def _name_has(self, request_name: str, hints: Any) -> bool:
        name = _as_str(request_name)
        return any(hint in name for hint in hints)

    def _request_is_self_gifts(self, account: int, request: Any) -> bool:
        if request is None:
            return False
        if bool(getattr(request, "collection_id", 0)):
            return False
        offset = _as_str(getattr(request, "offset", "") or "")
        if offset and offset not in ("", "0"):
            return False
        peer = getattr(request, "peer", None)
        if peer is None:
            user_id = _as_int(getattr(request, "user_id", 0) or getattr(request, "id", 0))
            return bool(user_id) and self._is_me(account, user_id)
        name = _class_name(peer).lower()
        if "self" in name:
            return True
        user_id = _as_int(getattr(peer, "user_id", 0) or getattr(peer, "userId", 0))
        if user_id and self._is_me(account, user_id):
            return True
        return False

    def _inject_local_gifts(self, response: Any) -> bool:
        built = self._built_local_saved()
        if not built:
            return False
        current = _java_list(getattr(response, "gifts", None))
        merged = _new_java_list()
        try:
            for item in built:
                merged.add(item)
            for item in current:
                if self._local_record_from_obj(item) is not None:
                    continue
                merged.add(item)
            self._set_field(response, "gifts", merged)
        except Exception:
            _safe_log(f"inject list failed:\n{_format_exc()}")
            return False
        server = 0
        for item in current:
            if self._local_record_from_obj(item) is None:
                server += 1
        reported = _as_int(getattr(response, "count", server))
        if reported < server:
            reported = server
        # Strip previously injected extras from server-reported count.
        extra = len(built)
        self._set_int(response, "count", reported - (len(current) - server) + extra)
        return True

    def _built_local_saved(self) -> List[Any]:
        built: List[Any] = []
        for rec in reversed(self._gifts):
            obj = self._build_saved_gift(rec)
            if obj is not None:
                built.append(obj)
        return built

    def _build_saved_gift(self, rec: Dict[str, Any]) -> Any:
        saved = self._new_tl(TL_SAVED_NAMES)
        if saved is None:
            return None
        catalog = self._catalog_by_id.get(_as_int(rec.get("gift_id")))
        gift_obj = catalog
        if rec.get("upgraded"):
            unique_obj = self._build_unique_gift(rec, catalog)
            if unique_obj is not None:
                gift_obj = unique_obj
        if gift_obj is None:
            return None

        # User gifts use msg_id (flag 3). saved_id is for channel gifts and
        # confuses StarGiftSheet / upgrade into a no-op.
        flags = SAVED_FLAG_FROM_ID | SAVED_FLAG_MSG_ID | SAVED_FLAG_PINNED
        if not rec.get("upgraded"):
            flags |= SAVED_FLAG_CAN_UPGRADE | SAVED_FLAG_UPGRADE_STARS
            self._set_bool(saved, "can_upgrade", True)
            self._set_long(saved, "upgrade_stars", 25)
        else:
            self._set_bool(saved, "can_upgrade", False)

        self._set_int(saved, "flags", flags)
        self._set_bool(saved, "unsaved", False)
        self._set_bool(saved, "pinned_to_top", True)
        self._set_bool(saved, "name_hidden", False)
        self._set_int(saved, "date", _as_int(rec.get("added_at"), int(time.time())))
        self._set_int(saved, "msg_id", _as_int(rec.get("msg_id")))
        self._set_field(saved, "gift", gift_obj)
        peer = self._self_peer()
        if peer is not None:
            self._set_field(saved, "from_id", peer)
        return saved

    def _build_unique_gift(self, rec: Dict[str, Any], catalog: Any) -> Any:
        unique = self._new_tl(TL_UNIQUE_NAMES)
        if unique is None:
            return catalog
        meta = rec.get("unique") or {}
        unique_id = 9_000_000_000_000 + (_as_int(rec.get("msg_id")) % 1_000_000_000)
        self._set_long(unique, "id", unique_id)
        self._set_long(unique, "gift_id", _as_int(rec.get("gift_id")))
        self._set_field(unique, "title", _as_str(meta.get("title") or rec.get("title") or "Collectible"))
        self._set_field(unique, "slug", _as_str(meta.get("slug") or rec.get("slug")))
        self._set_int(unique, "num", _as_int(meta.get("num"), 1))
        self._set_int(unique, "availability_issued", _as_int(meta.get("issued"), 1))
        self._set_int(unique, "availability_total", _as_int(meta.get("total"), 50000))
        peer = self._self_peer()
        if peer is not None:
            self._set_field(unique, "owner_id", peer)
            flags = _as_int(getattr(unique, "flags", 0))
            flags |= 1
            self._set_int(unique, "flags", flags)
        attrs = self._unique_attr_refs.get(str(rec.get("id")))
        if attrs is None or not _java_list(attrs):
            attrs = self._synthetic_unique_attrs(rec, catalog)
            if attrs is not None:
                self._unique_attr_refs[str(rec.get("id"))] = attrs
        if attrs is None:
            attrs = _new_java_list()
        self._set_field(unique, "attributes", attrs)
        return unique

    def _self_peer(self) -> Any:
        account = self._selected_account()
        user_id = 0
        try:
            cfg = self._user_config(account)
            me = cfg.getCurrentUser() if cfg is not None else None
            user_id = _as_int(getattr(me, "id", 0))
        except Exception:
            user_id = 0
        if user_id <= 0:
            return None
        peer = self._new_tl(("org.telegram.tgnet.TLRPC$TL_peerUser",))
        if peer is None and TLRPC is not None:
            ctor = getattr(TLRPC, "TL_peerUser", None)
            if callable(ctor):
                try:
                    peer = ctor()
                except Exception:
                    peer = None
        if peer is None:
            return None
        self._set_long(peer, "user_id", user_id)
        return peer

    def _local_record_from_obj(self, obj: Any) -> Optional[Dict[str, Any]]:
        if obj is None or not self._gifts:
            return None
        seen: Set[int] = set()
        stack = [obj]
        depth = 0
        while stack and depth < 24:
            current = stack.pop()
            depth += 1
            ident = id(current)
            if ident in seen:
                continue
            seen.add(ident)
            rec = self._match_local_gift(current)
            if rec is not None:
                return rec
            for name in (
                "stargift",
                "starGift",
                "gift",
                "invoice",
                "saved_gift",
                "savedGift",
                "savedStarGift",
                "saved_star_gift",
                "messageObject",
                "inputInvoice",
                "inputGift",
                "input_gift",
            ):
                child = getattr(current, name, None)
                if child is not None and not isinstance(child, (str, int, float, bool)):
                    stack.append(child)
        return None

    def _match_local_gift(self, obj: Any) -> Optional[Dict[str, Any]]:
        msg_id = _as_int(getattr(obj, "msg_id", 0))
        saved_id = _as_int(getattr(obj, "saved_id", 0))
        if not msg_id:
            try:
                msg_id = _as_int(get_private_field(obj, "msg_id"))
            except Exception:
                pass
        if not saved_id:
            try:
                saved_id = _as_int(get_private_field(obj, "saved_id"))
            except Exception:
                pass
        slug = _as_str(getattr(obj, "slug", "") or "")
        for rec in self._gifts:
            if msg_id and msg_id == _as_int(rec.get("msg_id")):
                return rec
            if saved_id and saved_id == _as_int(rec.get("saved_id")):
                return rec
            rec_slug = _as_str(rec.get("slug") or "")
            if slug and rec_slug and slug == rec_slug:
                return rec
            unique = rec.get("unique") or {}
            if slug and slug == _as_str(unique.get("slug") or ""):
                return rec
        if slug.startswith(LOCAL_SLUG_PREFIX):
            return {"id": slug, "slug": slug}
        return None

    def _on_blocked_local_request(self, request_name: str, rec: Dict[str, Any]) -> None:
        name = _as_str(request_name).lower()
        if (
            "upgrade" in name
            or "paymentform" in name
            or "payment_form" in name
            or "starsform" in name
        ):
            return
        if self._blocking_local:
            return
        self._blocking_local = True

        def _ui() -> None:
            self._blocking_local = False
            self._toast_error("Локальный подарок нельзя продать, передать или вывести")

        run_on_ui_thread(_ui)

    def _install_gift_java_hooks(self) -> None:
        for class_name in GIFT_SHEET_CLASSES:
            self._hook_all(class_name, "show", before=self._before_gift_sheet_show)
            self._hook_ctors(class_name, after=self._after_gift_sheet_ctor)
            # void methods only — never sendRequest (int token = crash).
            self._hook_all(class_name, "openUpgrade", before=self._before_open_upgrade)
            self._hook_all(class_name, "doUpgrade", before=self._before_do_upgrade)

    def _hook_ctors(self, class_name: str, after: Callable[[Any], None]) -> None:
        cls = find_class(class_name)
        if cls is None:
            return
        hook = _CallbackHook(after=after)
        for method_name in ("hook_all_constructors", "hookAllConstructors"):
            method = getattr(self, method_name, None)
            if not callable(method):
                continue
            try:
                method(cls, hook)
                self.log(f"hooked ctors {class_name}")
                return
            except Exception:
                _safe_log(f"{method_name} {class_name} failed:\n{_format_exc()}")

    def _after_gift_sheet_ctor(self, param: Any) -> None:
        sheet = getattr(param, "thisObject", None)
        if sheet is None:
            return
        if sheet not in self._live_gift_sheets:
            self._live_gift_sheets.append(sheet)
            if len(self._live_gift_sheets) > 8:
                self._live_gift_sheets = self._live_gift_sheets[-8:]
        rec = self._local_from_sheet(sheet, getattr(param, "args", None))
        if rec is None:
            return
        self._mark_local_sheet(sheet, rec)
        self._prepare_local_sheet(sheet, rec)

    def _after_gift_sheet_set(self, param: Any) -> None:
        sheet = getattr(param, "thisObject", None)
        rec = self._local_from_sheet(sheet, getattr(param, "args", None))
        if rec is None:
            return
        try:
            setattr(sheet, "_local_snos_gift", rec.get("id"))
        except Exception:
            pass
        self._prepare_local_sheet(sheet, rec)

    def _prepare_local_sheet(self, sheet: Any, rec: Optional[Dict[str, Any]] = None) -> None:
        if sheet is None:
            return
        if rec is None:
            rec = self._local_from_sheet(sheet, None)
        if rec is None or rec.get("upgraded"):
            return
        saved = None
        for name in ("savedStarGift", "saved_gift", "gift"):
            try:
                saved = get_private_field(sheet, name)
            except Exception:
                saved = getattr(sheet, name, None)
            if saved is not None and hasattr(saved, "upgrade_stars"):
                break
        if saved is None:
            return
        # Prepaid path: StarGiftSheet skips getPaymentForm and later
        # calls payments.upgradeStarGift, which we fake as success.
        self._set_bool(saved, "can_upgrade", True)
        self._set_long(saved, "upgrade_stars", 25)
        flags = _as_int(getattr(saved, "flags", 0))
        flags |= SAVED_FLAG_CAN_UPGRADE | SAVED_FLAG_UPGRADE_STARS | SAVED_FLAG_MSG_ID
        flags &= ~SAVED_FLAG_SAVED_ID
        self._set_int(saved, "flags", flags)

    def _mark_local_sheet(self, sheet: Any, rec: Optional[Dict[str, Any]]) -> None:
        if sheet is None or rec is None:
            return
        try:
            self._sheet_rec_ids[id(sheet)] = _as_str(rec.get("id"))
        except Exception:
            pass
        try:
            setattr(sheet, "_local_snos_gift", rec.get("id"))
        except Exception:
            pass
        if sheet not in self._live_gift_sheets:
            self._live_gift_sheets.append(sheet)
            if len(self._live_gift_sheets) > 8:
                self._live_gift_sheets = self._live_gift_sheets[-8:]

    def _before_gift_sheet_show(self, param: Any) -> None:
        sheet = getattr(param, "thisObject", None)
        rec = self._sheet_local_rec(sheet, getattr(param, "args", None))
        if rec is not None:
            self._mark_local_sheet(sheet, rec)
            self._prepare_local_sheet(sheet, rec)
        return

    def _sheet_local_rec(self, sheet: Any, args: Any = None) -> Optional[Dict[str, Any]]:
        if sheet is not None:
            mapped = self._sheet_rec_ids.get(id(sheet))
            if mapped:
                rec = self._gift_by_id(_as_str(mapped))
                if rec is not None:
                    return rec
        rec = self._local_from_sheet(sheet, args)
        if rec is not None:
            return rec
        if sheet is not None:
            marker = getattr(sheet, "_local_snos_gift", None)
            if marker:
                rec = self._gift_by_id(_as_str(marker))
                if rec is not None:
                    return rec
        msg_id = 0
        if sheet is not None:
            for name in ("savedStarGift", "savedGift", "saved_gift"):
                try:
                    saved = get_private_field(sheet, name)
                except Exception:
                    saved = getattr(sheet, name, None)
                if saved is not None:
                    msg_id = _as_int(getattr(saved, "msg_id", 0))
                    if msg_id:
                        break
        if LOCAL_MSG_MIN <= msg_id <= LOCAL_MSG_MAX:
            for rec in self._gifts:
                if _as_int(rec.get("msg_id")) == msg_id:
                    return rec
        return None

    def _local_from_sheet(self, sheet: Any, args: Any) -> Optional[Dict[str, Any]]:
        if sheet is not None:
            rec = self._local_record_from_obj(sheet)
            if rec is not None:
                return rec
            for name in (
                "savedStarGift",
                "savedGift",
                "saved_gift",
                "gift",
                "slug",
                "messageId",
                "saved_id",
                "msg_id",
            ):
                try:
                    value = get_private_field(sheet, name)
                except Exception:
                    value = getattr(sheet, name, None)
                rec = self._local_record_from_obj(value) if value is not None else None
                if rec is None and value is not None and not isinstance(value, (str, int, float)):
                    rec = self._match_local_gift(value)
                if rec is None and isinstance(value, (int, float)):
                    rec = self._match_local_gift(type("T", (), {"msg_id": int(value), "saved_id": int(value)})())
                if rec is None and isinstance(value, str):
                    rec = self._match_local_gift(type("T", (), {"slug": value})())
                if rec is not None:
                    return rec
        for arg in list(args or []):
            rec = self._local_record_from_obj(arg)
            if rec is not None:
                return rec
            if arg is not None:
                rec = self._match_local_gift(arg)
                if rec is not None:
                    return rec
        return None

    def _skip_void(self, param: Any) -> None:
        try:
            param.setResult(None)
        except Exception:
            pass

    def _before_open_upgrade(self, param: Any) -> None:
        sheet = getattr(param, "thisObject", None)
        rec = self._sheet_local_rec(sheet, getattr(param, "args", None))
        if rec is None:
            return
        self._skip_void(param)
        self._mark_local_sheet(sheet, rec)
        self._prepare_local_sheet(sheet, rec)
        run_on_ui_thread(lambda s=sheet, r=rec: self._open_local_upgrade_page(s, r), 10)

    def _open_local_upgrade_page(self, sheet: Any, rec: Dict[str, Any]) -> None:
        if sheet is None or rec is None:
            return
        self._prepare_local_sheet(sheet, rec)

        def _go(samples: Any) -> None:
            if samples is not None:
                try:
                    set_private_field(sheet, "sample_attributes", samples)
                except Exception:
                    self._set_field(sheet, "sample_attributes", samples)
            self._prepare_local_sheet(sheet, rec)
            opened = False
            method = getattr(sheet, "openUpgradeAfter", None)
            if callable(method):
                try:
                    method()
                    opened = True
                except Exception:
                    _safe_log(f"openUpgradeAfter failed:\n{_format_exc()}")
            if not opened:
                result = self._java_call(sheet, "openUpgradeAfter")
                opened = result is not False
            if not opened:
                _safe_log("openUpgradeAfter missing, staying on info page")
            self._bind_confirm_button(sheet, rec)
            run_on_ui_thread(lambda s=sheet, r=rec: self._bind_confirm_button(s, r), 200)
            run_on_ui_thread(lambda s=sheet, r=rec: self._bind_confirm_button(s, r), 500)

        samples = self._sheet_sample_attributes()
        if samples:
            _go(samples)
            return
        if self._native_gift_preview(_as_int(rec.get("gift_id")), lambda preview: _go(getattr(preview, "sample_attributes", None) if preview is not None else None)):
            return
        self._fetch_preview_samples(rec, _go)

    def _native_gift_preview(self, gift_id: int, callback: Callable) -> bool:
        if gift_id <= 0:
            return False
        account = self._selected_account()
        sc = None
        for class_name in (
            "org.telegram.ui.Stars.StarsController",
            "org.telegram.messenger.StarsController",
        ):
            cls = find_class(class_name)
            if cls is None:
                continue
            method = getattr(cls, "getInstance", None)
            if not callable(method):
                continue
            try:
                sc = method(int(account))
            except Exception:
                try:
                    sc = method()
                except Exception:
                    sc = None
            if sc is not None:
                break
        if sc is None:
            return False
        preview = getattr(sc, "getStarGiftPreview", None)
        if not callable(preview):
            return False

        def _cb(result: Any = None) -> None:
            run_on_ui_thread(lambda: callback(result))

        for args in ((_jlong(gift_id), _cb), (gift_id, _cb), (_jlong(gift_id),)):
            try:
                preview(*args)
                return True
            except Exception:
                continue
        return False

    def _fetch_preview_samples(self, rec: Dict[str, Any], done: Callable[[Any], None]) -> None:
        req = self._new_tl(TL_PREVIEW_NAMES) or self._new_tl(TL_ATTRS_NAMES)
        if req is None:
            done(None)
            return
        gift_id = _as_int(rec.get("gift_id"))
        self._set_long(req, "gift_id", gift_id)

        def _done(response: Any, error: Any) -> None:
            def _ui() -> None:
                samples = None
                if response is not None:
                    samples = getattr(response, "sample_attributes", None) or getattr(response, "attributes", None)
                done(samples)

            run_on_ui_thread(_ui)

        self._send_req(req, _done, self._selected_account())

    def _before_do_upgrade(self, param: Any) -> None:
        sheet = getattr(param, "thisObject", None)
        rec = self._sheet_local_rec(sheet, getattr(param, "args", None))
        if rec is None:
            return
        self._skip_void(param)
        self._mark_local_sheet(sheet, rec)
        gift_id = _as_str(rec.get("id") or "")
        run_on_ui_thread(lambda s=sheet, gid=gift_id: self._complete_sheet_upgrade(s, gid), 10)

    def _sheet_button(self, sheet: Any) -> Any:
        if sheet is None:
            return None
        for name in ("button", "actionButton", "premiumButton"):
            try:
                value = get_private_field(sheet, name)
            except Exception:
                value = getattr(sheet, name, None)
            if value is not None:
                return value
        return None

    def _stop_sheet_loading(self, sheet: Any) -> None:
        button = self._sheet_button(sheet)
        if button is None:
            return
        for args in ((False,), (False, True), (False, False)):
            try:
                button.setLoading(*args)
                return
            except Exception:
                continue

    def _switch_sheet_info(self, sheet: Any) -> None:
        if sheet is None:
            return
        switch = getattr(sheet, "switchPage", None)
        if callable(switch):
            try:
                switch(_jint(0), True)
                return
            except Exception:
                try:
                    switch(0, True)
                    return
                except Exception:
                    pass
        self._java_call(sheet, "switchPage", _jint(0), True)

    def _as_click_listener(self, fn: Callable) -> Any:
        try:
            from android.view import View

            class _Click(View.OnClickListener):
                def onClick(self, view: Any) -> None:
                    try:
                        fn(view)
                    except Exception:
                        _safe_log(f"click listener failed:\n{_format_exc()}")

            return _Click()
        except Exception:
            return fn

    def _bind_confirm_button(self, sheet: Any, rec: Dict[str, Any]) -> None:
        button = self._sheet_button(sheet)
        if button is None or rec is None:
            return
        gift_id = _as_str(rec.get("id") or "")
        self._mark_local_sheet(sheet, rec)

        def _on_click(_view: Any = None) -> None:
            self._stop_sheet_loading(sheet)
            self._complete_sheet_upgrade(sheet, gift_id)

        listener = self._as_click_listener(_on_click)
        try:
            button.setOnClickListener(listener)
        except Exception:
            _safe_log(f"bind confirm failed:\n{_format_exc()}")

    def _complete_sheet_upgrade(self, sheet: Any, gift_id: str) -> None:
        self._stop_sheet_loading(sheet)
        rec = self._gift_by_id(gift_id)
        if rec is None:
            self._stop_sheet_loading(sheet)
            return
        if rec.get("upgraded"):
            self._stop_sheet_loading(sheet)
            self._switch_sheet_info(sheet)
            self._refresh_gifts_ui()
            return
        try:
            catalog = self._catalog_by_id.get(_as_int(rec.get("gift_id")))
            meta = rec.get("unique") or self._roll_unique(rec)
            attrs = None
            samples = self._sheet_sample_attributes()
            if samples:
                try:
                    meta, attrs = self._unique_from_preview(
                        rec, type("P", (), {"sample_attributes": samples})(), meta
                    )
                except Exception:
                    _safe_log(f"samples pick failed:\n{_format_exc()}")
            rec["unique"] = meta
            rec["upgraded"] = True
            rec["upgraded_at"] = int(time.time())
            if attrs is None:
                attrs = self._synthetic_unique_attrs(rec, catalog)
            if attrs is not None:
                self._unique_attr_refs[str(rec.get("id"))] = attrs
            self._persist_gifts()
            unique_obj = self._build_unique_gift(rec, catalog)
            applied = False
            updates = self._build_upgrade_updates(rec, unique_obj) if unique_obj is not None else None
            if sheet is not None and updates is not None:
                try:
                    applied = bool(self._apply_sheet_upgrade(sheet, rec, updates))
                except Exception:
                    applied = False
                    _safe_log(f"apply sheet upgrade failed:\n{_format_exc()}")
            if not applied and sheet is not None:
                self._force_sheet_unique(sheet, rec, unique_obj)
            self._switch_sheet_info(sheet)
            self._refresh_gifts_ui()
            title = self._gift_title(rec)
            num = _as_int((rec.get("unique") or {}).get("num"))
            self._toast_success(f"{title} стал коллекционным #{num}")
        except Exception:
            _safe_log(f"complete upgrade crashed:\n{_format_exc()}")
            try:
                self._upgrade_local_gift(gift_id)
            except Exception:
                pass
        finally:
            self._stop_sheet_loading(sheet)

    def _force_sheet_unique(self, sheet: Any, rec: Dict[str, Any], unique_obj: Any) -> None:
        if sheet is None or unique_obj is None:
            return
        saved = None
        for name in ("savedStarGift", "savedGift", "saved_gift"):
            try:
                saved = get_private_field(sheet, name)
            except Exception:
                saved = getattr(sheet, name, None)
            if saved is not None:
                break
        if saved is not None:
            self._set_field(saved, "gift", unique_obj)
            self._set_bool(saved, "can_upgrade", False)
            flags = _as_int(getattr(saved, "flags", 0))
            flags &= ~SAVED_FLAG_CAN_UPGRADE
            flags &= ~SAVED_FLAG_UPGRADE_STARS
            self._set_int(saved, "flags", flags)
        try:
            self._set_bool(sheet, "rolling", True)
        except Exception:
            pass
        gifts_list = None
        try:
            gifts_list = get_private_field(sheet, "giftsList")
        except Exception:
            gifts_list = getattr(sheet, "giftsList", None)
        setter = getattr(sheet, "set", None)
        if callable(setter) and saved is not None:
            for args in ((saved, gifts_list), (saved,)):
                try:
                    setter(*args)
                    break
                except Exception:
                    continue
        try:
            self._set_bool(sheet, "rolling", False)
        except Exception:
            pass

    def _apply_sheet_upgrade(self, sheet: Any, rec: Dict[str, Any], updates: Any) -> bool:
        if sheet is None or updates is None:
            return False
        input_gift = None
        method = getattr(sheet, "getInputStarGift", None)
        if callable(method):
            try:
                input_gift = method()
            except Exception:
                input_gift = None
        if input_gift is None:
            result = self._java_call(sheet, "getInputStarGift")
            if result is not False:
                input_gift = result
        done = self._as_runnable(lambda: None)
        method = getattr(sheet, "applyNewGiftFromUpdates", None)
        if callable(method):
            try:
                method(input_gift, updates, done)
                return True
            except Exception:
                _safe_log(f"applyNewGiftFromUpdates call failed:\n{_format_exc()}")
        invoked = self._java_call(sheet, "applyNewGiftFromUpdates", input_gift, updates, done)
        return invoked is not False

    def _as_runnable(self, fn: Callable[[], None]) -> Any:
        try:
            from java.lang import Runnable

            class _Run(Runnable):
                def run(self) -> None:
                    try:
                        fn()
                    except Exception:
                        _safe_log(f"runnable failed:\n{_format_exc()}")

            return _Run()
        except Exception:
            return fn

    def _java_call(self, obj: Any, method_name: str, *args: Any) -> Any:
        if obj is None:
            return False
        try:
            cls = obj.getClass()
        except Exception:
            return False
        while cls is not None:
            try:
                methods = cls.getDeclaredMethods()
            except Exception:
                methods = []
            for method in methods:
                try:
                    if _as_str(method.getName()) != method_name:
                        continue
                    method.setAccessible(True)
                    return method.invoke(obj, *args)
                except Exception:
                    continue
            try:
                cls = cls.getSuperclass()
            except Exception:
                break
        return False

    def _before_send_request(self, param: Any) -> None:
        if not self._gifts:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        request = args[0]
        name = _class_name(request).lower()
        if not name:
            return
        if (
            "stargift" not in name
            and "paymentform" not in name
            and "payment_form" not in name
            and "starsform" not in name
        ):
            return
        if "preview" in name or "getstargifts" in name or "getsaved" in name:
            return
        rec = self._local_record_from_obj(request)
        if rec is None:
            return
        gift_id = _as_str(rec.get("id") or "")
        live = self._gift_by_id(gift_id)
        is_upgrade = (
            "upgrade" in name
            or "paymentform" in name
            or "payment_form" in name
            or "starsform" in name
        )
        if not is_upgrade:
            return
        callback = self._request_callback(args)
        # Dead path: sendRequest is no longer hooked (int return = crash).
        return
        if "paymentform" in name or "payment_form" in name:
            form = self._fake_payment_form()
            if form is not None:
                self._invoke_request_callback(callback, form, None)
            else:
                self._nudge_open_upgrade()
            return
        if live is None:
            return
        wrap_payment = "starsform" in name or "sendstarsform" in name or "sendpaymentform" in name
        if live.get("upgraded"):
            unique_obj = self._build_unique_gift(
                live, self._catalog_by_id.get(_as_int(live.get("gift_id")))
            )
            payload = self._build_upgrade_payload(live, unique_obj, wrap_payment)
            if payload is not None:
                self._invoke_request_callback(callback, payload, None)
            return
        self._serve_local_upgrade(live, callback, wrap_payment=wrap_payment)

    def _request_callback(self, args: Any) -> Any:
        for arg in list(args or [])[1:]:
            if arg is None or isinstance(arg, (str, int, float, bool)):
                continue
            run = getattr(arg, "run", None)
            if callable(run):
                return arg
        return None

    def _invoke_request_callback(self, callback: Any, response: Any, error: Any) -> None:
        if callback is None:
            return

        def _run() -> None:
            try:
                callback.run(response, error)
                return
            except TypeError:
                pass
            except Exception:
                _safe_log(f"request callback failed:\n{_format_exc()}")
                return
            try:
                callback.run(response, error, 0)
            except Exception:
                _safe_log(f"request callback(ts) failed:\n{_format_exc()}")

        run_on_ui_thread(_run, 50)

    def _nudge_open_upgrade(self) -> None:
        def _ui() -> None:
            for sheet in list(reversed(self._live_gift_sheets)):
                rec = self._local_from_sheet(sheet, None)
                if rec is None:
                    continue
                self._prepare_local_sheet(sheet, rec)
                method = getattr(sheet, "openUpgradeAfter", None)
                if callable(method):
                    try:
                        method()
                        return
                    except Exception:
                        _safe_log(f"openUpgradeAfter failed:\n{_format_exc()}")

        run_on_ui_thread(_ui, 80)

    def _serve_local_upgrade(self, rec: Dict[str, Any], callback: Any, wrap_payment: bool = False) -> None:
        if self._serving_upgrade:
            return
        self._serving_upgrade = True
        gift_id = _as_str(rec.get("id") or "")

        def _finish(meta: Dict[str, Any], attrs: Any = None) -> None:
            self._serving_upgrade = False
            live = self._gift_by_id(gift_id)
            if live is None:
                return
            if meta:
                live["unique"] = meta
            live["upgraded"] = True
            live["upgraded_at"] = int(time.time())
            if attrs is not None:
                self._unique_attr_refs[str(live.get("id"))] = attrs
            catalog = self._catalog_by_id.get(_as_int(live.get("gift_id")))
            if attrs is None:
                attrs = self._synthetic_unique_attrs(live, catalog)
                if attrs is not None:
                    self._unique_attr_refs[str(live.get("id"))] = attrs
            self._persist_gifts()
            unique_obj = self._build_unique_gift(live, catalog)
            payload = self._build_upgrade_payload(live, unique_obj, wrap_payment)
            if payload is None:
                self._suppress_gift_error += 1
                self._upgrade_local_gift(gift_id)
                run_on_ui_thread(
                    lambda: setattr(self, "_suppress_gift_error", max(0, self._suppress_gift_error - 1)),
                    800,
                )
                return
            self._invoke_request_callback(callback, payload, None)
            run_on_ui_thread(self._refresh_gifts_ui, 1800)

        sheet_samples = self._sheet_sample_attributes()
        if sheet_samples:
            fallback = rec.get("unique") or self._roll_unique(rec)
            meta, attrs = self._unique_from_preview(rec, type("P", (), {"sample_attributes": sheet_samples})(), fallback)
            _finish(meta, attrs)
            return
        try:
            self._fetch_upgrade_preview(rec, _finish)
        except Exception:
            _safe_log(f"serve upgrade preview failed:\n{_format_exc()}")
            _finish(self._roll_unique(rec), None)

    def _fake_payment_form(self) -> Any:
        form = self._new_tl(TL_PAYMENT_FORM_NAMES)
        if form is None:
            return None
        invoice = self._new_tl(TL_INVOICE_NAMES)
        if invoice is not None:
            self._set_field(invoice, "currency", "XTR")
            prices = _new_java_list()
            self._set_field(invoice, "prices", prices)
            self._set_field(form, "invoice", invoice)
        form_id = 9_000_000_000_000 + random.randint(1, 999_999)
        if not self._set_field(form, "form_id", _jlong(form_id)):
            self._set_field(form, "form_id", form_id)
        self._set_field(form, "title", "Upgrade")
        self._set_field(form, "description", " ")
        self._set_field(form, "users", _new_java_list())
        self._set_field(form, "bot_id", 777000)
        if TLRPC is not None:
            try:
                if not isinstance(form, TLRPC.PaymentForm):
                    return None
            except Exception:
                pass
        return form

    def _sheet_sample_attributes(self) -> Any:
        for sheet in list(reversed(self._live_gift_sheets)):
            for name in ("sample_attributes", "sampleAttributes"):
                try:
                    value = get_private_field(sheet, name)
                except Exception:
                    value = getattr(sheet, name, None)
                if value is not None and _java_list(value):
                    return value
        return None

    def _build_upgrade_payload(self, rec: Dict[str, Any], unique_obj: Any, wrap_payment: bool) -> Any:
        updates = self._build_upgrade_updates(rec, unique_obj)
        if updates is None or not wrap_payment:
            return updates
        result = self._new_tl(TL_PAYMENT_RESULT_NAMES)
        if result is None:
            return updates
        self._set_field(result, "updates", updates)
        return result

    def _build_upgrade_updates(self, rec: Dict[str, Any], unique_obj: Any) -> Any:
        if unique_obj is None:
            return None
        updates = self._new_tl(TL_UPDATES_NAMES)
        if updates is None and TLRPC is not None:
            ctor = getattr(TLRPC, "TL_updates", None)
            if callable(ctor):
                try:
                    updates = ctor()
                except Exception:
                    updates = None
        if updates is None:
            return None
        action = self._new_tl(TL_ACTION_UNIQUE_NAMES)
        if action is None and TLRPC is not None:
            ctor = getattr(TLRPC, "TL_messageActionStarGiftUnique", None)
            if callable(ctor):
                try:
                    action = ctor()
                except Exception:
                    action = None
        if action is None:
            return None
        self._set_field(action, "gift", unique_obj)
        self._set_field(action, "saved", True)
        self._set_field(action, "refunded", False)
        far = int(time.time()) + 10 * 365 * 24 * 3600
        self._set_field(action, "can_export_at", far)
        self._set_field(action, "can_transfer_at", far)
        self._set_field(action, "can_resell_at", far)
        flags = _as_int(getattr(action, "flags", 0))
        flags |= 4  # saved
        self._set_int(action, "flags", flags)
        peer = self._self_peer()
        if peer is not None:
            self._set_field(action, "from_id", peer)
            self._set_field(action, "peer", peer)
        message = self._new_tl(TL_MESSAGE_SERVICE_NAMES)
        if message is None:
            return None
        msg_id = _as_int(rec.get("msg_id"))
        self._set_int(message, "id", msg_id)
        self._set_int(message, "date", int(time.time()))
        self._set_field(message, "action", action)
        if peer is not None:
            self._set_field(message, "from_id", peer)
            self._set_field(message, "peer_id", peer)
        self._set_field(message, "out", True)
        self._set_int(message, "local_id", msg_id)
        upd = self._new_tl(TL_UPDATE_NEW_MSG_NAMES)
        if upd is None:
            return None
        self._set_field(upd, "message", message)
        self._set_int(upd, "pts", 0)
        self._set_int(upd, "pts_count", 0)
        bucket = _new_java_list()
        try:
            bucket.add(upd)
        except Exception:
            return None
        self._set_field(updates, "updates", bucket)
        self._set_field(updates, "users", _new_java_list())
        self._set_field(updates, "chats", _new_java_list())
        self._set_int(updates, "date", int(time.time()))
        self._set_int(updates, "seq", 0)
        try:
            setattr(updates, "_local_snos_upgrade", True)
        except Exception:
            pass
        ident = id(updates)
        self._local_upgrade_tokens.add(ident)
        if len(self._local_upgrade_tokens) > 32:
            self._local_upgrade_tokens = set(list(self._local_upgrade_tokens)[-16:])
        return updates

    def _before_process_updates(self, param: Any) -> None:
        args = getattr(param, "args", None)
        if not args:
            return
        updates = args[0]
        if updates is None:
            return
        if getattr(updates, "_local_snos_upgrade", False) or id(updates) in self._local_upgrade_tokens:
            try:
                param.setResult(None)
            except Exception:
                pass
            return
        rec = self._local_record_from_obj(updates)
        if rec is not None:
            try:
                param.setResult(None)
            except Exception:
                pass

    def _before_error_bulletin(self, param: Any) -> None:
        if self._suppress_gift_error > 0:
            try:
                param.setResult(None)
            except Exception:
                pass

    def _before_post_notification(self, param: Any) -> None:
        if not self._gifts_enabled() or not self._gifts:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        event = args[0]
        star = None
        if NotificationCenter is not None:
            star = getattr(NotificationCenter, "starUserGiftsLoaded", None)
        if star is None or _as_int(event) != _as_int(star):
            return
        gifts_list = None
        dialog_id = 0
        if len(args) >= 3:
            dialog_id = _as_int(args[1])
            gifts_list = args[2]
        elif len(args) == 2:
            gifts_list = args[1]
        if gifts_list is None:
            return
        account = self._selected_account()
        if dialog_id not in (0,) and not self._is_me(account, dialog_id):
            return
        self._inject_into_gifts_list(gifts_list)

    def _maybe_bump_gifts_count(self, user_full: Any) -> None:
        if user_full is None or not self._gifts_enabled():
            extra = 0
        else:
            extra = len(self._gifts)
        if user_full is None:
            return
        user_id = _as_int(getattr(user_full, "id", 0) or getattr(user_full, "user_id", 0))
        if user_id and not self._is_me(self._selected_account(), user_id):
            return
        current = _as_int(getattr(user_full, "stargifts_count", 0))
        prev = _as_int(getattr(self, "_gift_count_bump", 0))
        base = current - prev
        if base < 0:
            base = current
        new = base + extra
        if new == current and prev == extra:
            return
        self._set_int(user_full, "stargifts_count", new)
        flags2 = _as_int(getattr(user_full, "flags2", 0))
        flags2 |= USERFULL_STARGIFTS_FLAG2
        self._set_int(user_full, "flags2", flags2)
        self._gift_count_bump = extra

    def _inject_into_gifts_list(self, gifts_list: Any) -> None:
        if gifts_list is None or getattr(self, "_injecting", False):
            return
        self._injecting = True
        try:
            self._inject_into_gifts_list_inner(gifts_list)
        except Exception:
            _safe_log(f"inject gifts crashed:\n{_format_exc()}")
        finally:
            self._injecting = False

    def _inject_into_gifts_list_inner(self, gifts_list: Any) -> None:
        built = self._built_local_saved()
        raw = getattr(gifts_list, "gifts", None)
        if raw is None:
            return
        current = _java_list(raw)
        locals_now = 0
        kept: List[Any] = []
        for item in current:
            if self._local_record_from_obj(item) is not None:
                locals_now += 1
            else:
                kept.append(item)
        try:
            raw.clear()
        except Exception:
            try:
                while raw.size() > 0:
                    raw.remove(0)
            except Exception:
                return
        try:
            for item in built:
                raw.add(item)
            for item in kept:
                raw.add(item)
        except Exception:
            _safe_log(f"gifts list inject failed:\n{_format_exc()}")
            return
        total = _as_int(getattr(gifts_list, "totalCount", len(kept)))
        server_total = max(0, total - locals_now)
        self._set_int(gifts_list, "totalCount", server_total + len(built))

    def _refresh_gifts_ui(self) -> None:
        account = self._selected_account()
        try:
            me = 0
            cfg = self._user_config(account)
            if cfg is not None:
                me = _as_int(getattr(cfg.getCurrentUser(), "id", 0))
            user_full = self._get_user_full(account, me) if me else None
            self._maybe_bump_gifts_count(user_full)
        except Exception:
            _safe_log(f"bump gifts count failed:\n{_format_exc()}")
        try:
            for gifts_list in self._iter_profile_gifts_lists(account):
                self._inject_into_gifts_list(gifts_list)
                notify = getattr(gifts_list, "notifyUpdate", None)
                if callable(notify):
                    try:
                        notify()
                    except Exception:
                        pass
        except Exception:
            _safe_log(f"refresh gifts lists failed:\n{_format_exc()}")
        # Do not post updateInterfaces here. Gift add must not go through
        # the freeze notify path (Integer/Long crashes, fragment refresh).

    def _iter_profile_gifts_lists(self, account: int) -> List[Any]:
        found: List[Any] = []
        sc = None
        for class_name in (
            "org.telegram.ui.Stars.StarsController",
            "org.telegram.messenger.StarsController",
        ):
            cls = find_class(class_name)
            if cls is None:
                continue
            method = getattr(cls, "getInstance", None)
            if not callable(method):
                continue
            try:
                sc = method(int(account))
            except Exception:
                try:
                    sc = method()
                except Exception:
                    sc = None
            if sc is not None:
                break
        if sc is None:
            return found
        me = 0
        try:
            cfg = self._user_config(account)
            if cfg is not None:
                me = _as_int(getattr(cfg.getCurrentUser(), "id", 0))
        except Exception:
            me = 0
        getter = getattr(sc, "getProfileGiftsList", None)
        if callable(getter):
            for key in (me, 0, _jlong(me) if me else 0):
                if not key and key != 0:
                    continue
                for args in ((key,), (key, False), (key, True)):
                    try:
                        lst = getter(*args)
                    except Exception:
                        continue
                    if lst is not None and lst not in found:
                        found.append(lst)
        mapping = None
        try:
            mapping = get_private_field(sc, "giftLists")
        except Exception:
            mapping = getattr(sc, "giftLists", None)
        if mapping is not None:
            try:
                size = int(mapping.size())
                for i in range(size):
                    lst = mapping.valueAt(i)
                    if lst is not None and lst not in found:
                        found.append(lst)
            except Exception:
                pass
        return found

    def _gift_is_unique_obj(self, gift: Any) -> bool:

        if gift is None:
            return False
        name = _class_name(gift).lower()
        if "unique" in name:
            return True
        slug = _as_str(getattr(gift, "slug", None) or "").strip()
        gift_id = _as_int(getattr(gift, "gift_id", 0))
        # Unique collectibles have a real slug plus the base gift_id.
        # Regular Java starGift objects often still *declare* slug/num/attributes.
        if slug and gift_id > 0:
            return True
        return False

    def _fetch_upgrade_preview(self, rec: Dict[str, Any], done: Callable) -> None:
        gift_id = _as_int(rec.get("gift_id"))
        fallback = self._roll_unique(rec)
        if gift_id <= 0:
            done(fallback, None)
            return
        req = self._new_tl(TL_PREVIEW_NAMES) or self._new_tl(TL_ATTRS_NAMES)
        if req is None:
            done(fallback, None)
            return
        try:
            self._set_long(req, "gift_id", gift_id)
        except Exception:
            try:
                req.gift_id = _jlong(gift_id)
            except Exception:
                done(fallback, None)
                return

        def _done(response: Any, error: Any) -> None:
            def _ui() -> None:
                if error or response is None:
                    done(fallback, None)
                    return
                unique, attrs = self._unique_from_preview(rec, response, fallback)
                done(unique, attrs)

            run_on_ui_thread(_ui)

        self._send_req(req, _done, self._selected_account())

    def _unique_from_preview(self, rec: Dict[str, Any], response: Any, fallback: Dict[str, Any]) -> Any:
        samples: List[Any] = []
        for name in ("sample_attributes", "attributes", "models", "gifts"):
            values = _java_list(getattr(response, name, None))
            if values:
                samples.extend(values)
        models: List[Any] = []
        patterns: List[Any] = []
        backdrops: List[Any] = []
        for item in samples:
            nested = _java_list(getattr(item, "attributes", None))
            pool = nested or [item]
            for attr in pool:
                aname = _class_name(attr).lower()
                if "model" in aname:
                    models.append(attr)
                elif "pattern" in aname:
                    patterns.append(attr)
                elif "backdrop" in aname:
                    backdrops.append(attr)
        rng = random.Random(_as_str(rec.get("id")) + str(rec.get("gift_id")))
        picked: List[Any] = []
        meta = dict(fallback)
        if models:
            model = rng.choice(models)
            picked.append(model)
            meta["model"] = _as_str(getattr(model, "name", None) or meta.get("model"))
            perm = _as_int(getattr(model, "rarity_permille", 0))
            if perm:
                meta["model_rarity"] = round(perm / 10.0, 1)
        if backdrops:
            backdrop = rng.choice(backdrops)
            picked.append(backdrop)
            meta["backdrop"] = _as_str(getattr(backdrop, "name", None) or meta.get("backdrop"))
            perm = _as_int(getattr(backdrop, "rarity_permille", 0))
            if perm:
                meta["backdrop_rarity"] = round(perm / 10.0, 1)
        if patterns:
            pattern = rng.choice(patterns)
            picked.append(pattern)
            meta["pattern"] = _as_str(getattr(pattern, "name", None) or meta.get("pattern"))
            perm = _as_int(getattr(pattern, "rarity_permille", 0))
            if perm:
                meta["pattern_rarity"] = round(perm / 10.0, 1)
        attrs = _new_java_list()
        try:
            for attr in picked:
                attrs.add(attr)
        except Exception:
            attrs = picked
        return meta, (attrs if picked else None)

    def _synthetic_unique_attrs(self, rec: Dict[str, Any], catalog: Any) -> Any:
        meta = rec.get("unique") or {}
        attrs = _new_java_list()
        added = 0
        model = self._new_tl(TL_ATTR_MODEL_NAMES)
        if model is not None:
            self._set_field(model, "name", _as_str(meta.get("model") or "Model"))
            rarity = int(float(meta.get("model_rarity") or 1.0) * 10)
            self._set_int(model, "rarity_permille", rarity)
            sticker = getattr(catalog, "sticker", None) if catalog is not None else None
            if sticker is not None:
                self._set_field(model, "document", sticker)
            try:
                attrs.add(model)
                added += 1
            except Exception:
                pass
        pattern = self._new_tl(TL_ATTR_PATTERN_NAMES)
        if pattern is not None:
            self._set_field(pattern, "name", _as_str(meta.get("pattern") or "Pattern"))
            rarity = int(float(meta.get("pattern_rarity") or 2.0) * 10)
            self._set_int(pattern, "rarity_permille", rarity)
            sticker = getattr(catalog, "sticker", None) if catalog is not None else None
            if sticker is not None:
                self._set_field(pattern, "document", sticker)
            try:
                attrs.add(pattern)
                added += 1
            except Exception:
                pass
        backdrop = self._new_tl(TL_ATTR_BACKDROP_NAMES)
        if backdrop is not None:
            self._set_field(backdrop, "name", _as_str(meta.get("backdrop") or "Backdrop"))
            rarity = int(float(meta.get("backdrop_rarity") or 3.0) * 10)
            self._set_int(backdrop, "rarity_permille", rarity)
            self._set_int(backdrop, "center_color", 0x3B1F6B)
            self._set_int(backdrop, "edge_color", 0x12081F)
            self._set_int(backdrop, "pattern_color", 0xF4C6FF)
            self._set_int(backdrop, "text_color", 0xFFFFFF)
            try:
                attrs.add(backdrop)
                added += 1
            except Exception:
                pass
        return attrs if added else None

    def _new_tl(self, names: Any) -> Any:
        for name in names:
            if name in self._tl_cache:
                cls = self._tl_cache[name]
            else:
                cls = find_class(name)
                self._tl_cache[name] = cls
            if cls is None:
                continue
            try:
                return cls()
            except Exception:
                try:
                    return cls.newInstance()
                except Exception:
                    continue
        return None

    # ------------------------------------------------------------------
    # Telegram helpers
    # ------------------------------------------------------------------

    def _feature_enabled(self) -> bool:
        return bool(self.get_setting(ENABLED_KEY, True))

    def _display_name(self) -> str:
        value = _as_str(self.get_setting(DISPLAY_NAME_KEY, DEFAULT_DISPLAY_NAME)).strip()
        return value or DEFAULT_DISPLAY_NAME

    def _frozen_bio(self) -> str:
        value = _as_str(self.get_setting(FROZEN_BIO_KEY, DEFAULT_FROZEN_BIO)).strip()
        return value or DEFAULT_FROZEN_BIO

    def _selected_account(self) -> int:
        try:
            from client_utils import get_selected_account

            return int(get_selected_account())
        except Exception:
            pass
        if UserConfig is not None:
            try:
                return int(UserConfig.selectedAccount)
            except Exception:
                pass
        return 0

    def _active_accounts(self) -> List[int]:
        accounts: List[int] = []
        max_count = 5
        if UserConfig is not None:
            try:
                max_count = int(getattr(UserConfig, "MAX_ACCOUNT_COUNT", 5) or 5)
            except Exception:
                max_count = 5
        for index in range(max_count):
            try:
                if UserConfig is not None:
                    cfg = UserConfig.getInstance(index)
                    if cfg is not None and cfg.isClientActivated():
                        accounts.append(index)
                        continue
            except Exception:
                pass
        if not accounts:
            accounts.append(self._selected_account())
        return accounts

    def _is_me(self, account: int, user_id: int) -> bool:
        try:
            cfg = self._user_config(account)
            if cfg is None:
                return False
            me = cfg.getCurrentUser()
            return me is not None and _as_int(getattr(me, "id", 0)) == int(user_id)
        except Exception:
            return False

    def _user_config(self, account: int) -> Any:
        if get_user_config is not None:
            try:
                return get_user_config(account)
            except TypeError:
                return get_user_config()
            except Exception:
                pass
        if UserConfig is not None:
            try:
                return UserConfig.getInstance(account)
            except Exception:
                pass
        return None

    def _messages_controller(self, account: Optional[int] = None) -> Any:
        if get_messages_controller is None:
            return None
        try:
            if account is not None:
                return get_messages_controller(account)
        except TypeError:
            pass
        except Exception:
            _safe_log(f"get_messages_controller({account}) failed:\n{_format_exc()}")
        try:
            return get_messages_controller()
        except Exception:
            return None

    def _notification_center(self, account: Optional[int] = None) -> Any:
        if get_notification_center is None:
            return None
        try:
            if account is not None:
                return get_notification_center(account)
        except TypeError:
            pass
        except Exception:
            pass
        try:
            return get_notification_center()
        except Exception:
            return None

    def _account_of_controller(self, controller: Any) -> int:
        if controller is not None:
            try:
                value = get_private_field(controller, "currentAccount")
                if value is not None:
                    return int(value)
            except Exception:
                pass
        return self._selected_account()

    def _get_user(self, account: int, user_id: int) -> Any:
        mc = self._messages_controller(account)
        if mc is None:
            return None
        for key in (int(user_id), _jlong(user_id)):
            try:
                user = mc.getUser(key)
                if user is not None:
                    return user
            except Exception:
                continue
        try:
            users = get_private_field(mc, "users")
            if users is not None:
                user = users.get(_jlong(user_id))
                if user is None:
                    user = users.get(int(user_id))
                return user
        except Exception:
            pass
        return None

    def _get_user_full(self, account: int, user_id: int) -> Any:
        mc = self._messages_controller(account)
        if mc is None:
            return None
        for key in (int(user_id), _jlong(user_id)):
            try:
                info = mc.getUserFull(key)
                if info is not None:
                    return info
            except Exception:
                continue
        try:
            mapping = get_private_field(mc, "userFulls")
            if mapping is not None:
                info = mapping.get(int(user_id))
                if info is None:
                    info = mapping.get(_jlong(user_id))
                return info
        except Exception:
            pass
        return None

    def _send_req(self, request: Any, callback: Callable, account: int) -> None:
        if send_request is None:
            callback(None, "send_request unavailable")
            return
        try:
            send_request(request, callback, account=account)
            return
        except TypeError:
            pass
        except Exception:
            _safe_log(f"send_request(account=) failed:\n{_format_exc()}")
        try:
            send_request(request, callback)
        except Exception:
            _safe_log(f"send_request failed:\n{_format_exc()}")
            callback(None, "request failed")

    def _peer_from_params(self, params: Any) -> Optional[int]:
        for name in ("peer", "dialogId", "dialog_id", "did"):
            if not hasattr(params, name):
                continue
            try:
                value = getattr(params, name)
                if value is None:
                    continue
                return int(value)
            except Exception:
                continue
        return None

    def _empty_photo(self) -> Any:
        if TLRPC is None:
            return None
        try:
            return TLRPC.TL_userProfilePhotoEmpty()
        except Exception:
            return None

    def _empty_status(self) -> Any:
        if TLRPC is None:
            return None
        try:
            return TLRPC.TL_userStatusEmpty()
        except Exception:
            return None

    def _is_empty_photo(self, photo: Any) -> bool:
        if photo is None:
            return True
        try:
            name = photo.getClass().getName()
            if "Empty" in _as_str(name):
                return True
        except Exception:
            pass
        if TLRPC is not None:
            try:
                if isinstance(photo, TLRPC.TL_userProfilePhotoEmpty):
                    return True
            except Exception:
                pass
        return False

    def _dump_photo(self, photo: Any) -> Optional[Dict[str, Any]]:
        if photo is None or self._is_empty_photo(photo):
            return None
        data: Dict[str, Any] = {"empty": False}
        for field in ("photo_id", "dc_id", "flags"):
            if hasattr(photo, field):
                data[field] = _as_int(getattr(photo, field, 0))
        for field in ("has_video", "personal"):
            if hasattr(photo, field):
                data[field] = bool(getattr(photo, field, False))
        return data

    def _load_photo(self, data: Any) -> Any:
        if not data or not isinstance(data, dict) or data.get("empty"):
            return self._empty_photo()
        if TLRPC is None:
            return None
        try:
            photo = TLRPC.TL_userProfilePhoto()
        except Exception:
            return None
        if "photo_id" in data:
            self._set_field(photo, "photo_id", _as_int(data.get("photo_id")))
        if "dc_id" in data:
            self._set_field(photo, "dc_id", _as_int(data.get("dc_id")))
        if "flags" in data:
            self._set_field(photo, "flags", _as_int(data.get("flags")))
        if "has_video" in data:
            self._set_field(photo, "has_video", bool(data.get("has_video")))
        if "personal" in data:
            self._set_field(photo, "personal", bool(data.get("personal")))
        return photo

    def _set_field(self, obj: Any, name: str, value: Any) -> bool:
        if obj is None:
            return False
        try:
            setattr(obj, name, value)
            return True
        except Exception:
            pass
        try:
            return bool(set_private_field(obj, name, value))
        except Exception:
            return False

    def _find_java_field(self, obj: Any, name: str) -> Any:
        try:
            cls = obj.getClass()
        except Exception:
            return None
        while cls is not None:
            try:
                field = cls.getDeclaredField(name)
                field.setAccessible(True)
                return field
            except Exception:
                try:
                    cls = cls.getSuperclass()
                except Exception:
                    return None
        return None

    def _set_int(self, obj: Any, name: str, value: int) -> bool:
        field = self._find_java_field(obj, name) if obj is not None else None
        if field is not None:
            try:
                tname = _as_str(field.getType().getName())
                ivalue = int(value)
                if tname == "int":
                    field.setInt(obj, ivalue)
                    return True
                if tname == "long":
                    field.setLong(obj, ivalue)
                    return True
                if tname == "java.lang.Integer":
                    field.set(obj, _jint(ivalue))
                    return True
                if tname == "java.lang.Long":
                    field.set(obj, _jlong(ivalue))
                    return True
            except Exception:
                pass
        return self._set_field(obj, name, int(value))

    def _set_long(self, obj: Any, name: str, value: int) -> bool:
        field = self._find_java_field(obj, name) if obj is not None else None
        if field is not None:
            try:
                tname = _as_str(field.getType().getName())
                ivalue = int(value)
                if tname == "long":
                    field.setLong(obj, ivalue)
                    return True
                if tname == "int":
                    field.setInt(obj, ivalue)
                    return True
                if tname == "java.lang.Long":
                    field.set(obj, _jlong(ivalue))
                    return True
                if tname == "java.lang.Integer":
                    field.set(obj, _jint(ivalue))
                    return True
            except Exception:
                pass
        return self._set_field(obj, name, _jlong(int(value)))

    def _set_bool(self, obj: Any, name: str, value: bool) -> bool:
        field = self._find_java_field(obj, name) if obj is not None else None
        if field is not None:
            try:
                tname = _as_str(field.getType().getName())
                if tname == "boolean":
                    field.setBoolean(obj, bool(value))
                    return True
                if tname == "java.lang.Boolean":
                    field.set(obj, bool(value))
                    return True
            except Exception:
                pass
        return self._set_field(obj, name, bool(value))

    def _notify_ui(self, account: int, user_id: Optional[int] = None, fragment: Any = None) -> None:
        def _do() -> None:
            try:
                self._post_notifications(account, user_id)
            except Exception:
                _safe_log(f"notify failed:\n{_format_exc()}")
            try:
                self._refresh_fragment(fragment or (get_last_fragment() if get_last_fragment else None))
            except Exception:
                _safe_log(f"fragment refresh failed:\n{_format_exc()}")

        run_on_ui_thread(_do, 120)

    def _post_notifications(self, account: int, user_id: Optional[int]) -> None:
        nc = self._notification_center(account)
        if nc is None or NotificationCenter is None:
            return

        mask = UPDATE_MASK_FALLBACK
        value = 0
        for name in (
            "UPDATE_MASK_NAME",
            "UPDATE_MASK_AVATAR",
            "UPDATE_MASK_STATUS",
            "UPDATE_MASK_PHONE",
            "UPDATE_MASK_USER_PHONE",
        ):
            part = getattr(NotificationCenter, name, None)
            if isinstance(part, int):
                value |= part
        if value:
            mask = value

        event_id = getattr(NotificationCenter, "updateInterfaces", None)
        if event_id is None:
            return

        # Must be java.lang.Integer. A Python int becomes Long and
        # DialogsActivity.didReceivedNotification crashes with
        # ClassCastException: Long cannot be cast to Integer.
        self._post_event(nc, event_id, [_jint(mask)])

    def _post_event(self, nc: Any, event_id: Any, args: List[Any]) -> None:
        for method_name in ("postNotificationNameOnUIThread", "postNotificationName"):
            method = getattr(nc, method_name, None)
            if not callable(method):
                continue
            try:
                if args:
                    method(event_id, *args)
                else:
                    method(event_id)
                return
            except Exception:
                _safe_log(f"{method_name} failed:\n{_format_exc()}")
                continue

    def _refresh_fragment(self, fragment: Any) -> None:
        if fragment is None:
            return
        for name, args in (
            ("updateProfileData", (True,)),
            ("updateProfileData", ()),
            ("updateTitle", ()),
            ("updateSubtitle", ()),
            ("updateOnlineDisplay", ()),
        ):
            method = getattr(fragment, name, None)
            if callable(method):
                try:
                    method(*args)
                except Exception:
                    continue
        try:
            self._nudge_native_deleted(fragment)
        except Exception:
            _safe_log(f"native deleted redraw failed:\n{_format_exc()}")
        avatar = getattr(fragment, "avatarContainer", None)
        if avatar is None:
            avatar = get_private_field(fragment, "avatarContainer")
        if avatar is None:
            return
        for name in ("checkAndUpdateAvatar", "updateAvatar"):
            method = getattr(avatar, name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    continue

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _activity(self) -> Any:
        fragment = get_last_fragment() if get_last_fragment else None
        if fragment is None:
            return None
        try:
            return fragment.getParentActivity()
        except Exception:
            return None

    def _dismiss_quiet(self, builder: Any) -> None:
        if builder is None:
            return
        try:
            builder.dismiss()
        except Exception:
            pass

    def _show_spinner(self, title: str, message: str) -> Any:
        activity = self._activity()
        if activity is None:
            return None
        try:
            style = getattr(AlertDialogBuilder, "ALERT_TYPE_SPINNER", 2)
            builder = AlertDialogBuilder(activity, style)
            builder.set_title(title)
            builder.set_message(message)
            builder.show()
            try:
                builder.set_cancelable(False)
            except Exception:
                pass
            return builder
        except Exception:
            _safe_log(f"spinner failed:\n{_format_exc()}")
            return None

    def _prompt_text(
        self,
        title: str,
        hint: str,
        on_done: Callable[[str], None],
        preset: str = "",
    ) -> None:
        activity = self._activity()
        if activity is None:
            on_done(preset)
            return
        try:
            from android.widget import EditText, FrameLayout
            from android.app import AlertDialog
            from android.content import DialogInterface

            density = float(activity.getResources().getDisplayMetrics().density)
            pad = int(density * 20)
            wrap = FrameLayout(activity)
            wrap.setPadding(pad, int(density * 8), pad, 0)
            edit = EditText(activity)
            edit.setHint(hint)
            edit.setSingleLine(True)
            if preset:
                edit.setText(preset)
                try:
                    edit.setSelection(len(preset))
                except Exception:
                    pass
            wrap.addView(edit)

            class _Ok(DialogInterface.OnClickListener):
                def onClick(self, dialog: Any, which: int) -> None:
                    try:
                        value = _as_str(edit.getText())
                    except Exception:
                        value = preset
                    on_done(value)

            class _Cancel(DialogInterface.OnClickListener):
                def onClick(self, dialog: Any, which: int) -> None:
                    return

            builder = AlertDialog.Builder(activity)
            builder.setTitle(title)
            builder.setView(wrap)
            builder.setPositiveButton("Найти", _Ok())
            builder.setNegativeButton("Отмена", _Cancel())
            builder.show()
            return
        except Exception:
            _safe_log(f"prompt text failed:\n{_format_exc()}")
        if preset:
            on_done(preset)
            return
        self._toast_info("Введи название в настройках плагина → Поиск подарка")

    def _show_items(self, title: str, items: List[str], on_pick: Callable[..., None]) -> None:
        activity = self._activity()
        if activity is None:
            self._toast_error("Нет активного окна")
            return
        try:
            builder = AlertDialogBuilder(activity)
            builder.set_title(title)
            builder.set_items(items, on_pick)
            builder.set_negative_button("Отмена", lambda b, _w: b.dismiss())
            builder.show()
        except Exception:
            _safe_log(f"items dialog failed:\n{_format_exc()}")
            self._toast_error("Не удалось открыть список")

    def _show_confirm(
        self,
        title: str,
        message: str,
        ok_text: str,
        on_ok: Callable[..., None],
    ) -> None:
        activity = self._activity()
        if activity is None:
            on_ok(None, None)
            return
        try:
            builder = AlertDialogBuilder(activity)
            builder.set_title(title)
            builder.set_message(message)
            builder.set_positive_button(ok_text, on_ok)
            builder.set_negative_button("Отмена", lambda b, _w: b.dismiss())
            try:
                builder.make_button_red(AlertDialogBuilder.BUTTON_POSITIVE)
            except Exception:
                pass
            builder.show()
        except Exception:
            _safe_log(f"dialog failed:\n{_format_exc()}")
            on_ok(None, None)

    def _toast_info(self, text: str) -> None:
        try:
            BulletinHelper.show_info(text)
        except Exception:
            self.log(text)

    def _toast_success(self, text: str) -> None:
        try:
            BulletinHelper.show_success(text)
        except Exception:
            self._toast_info(text)

    def _toast_error(self, text: str) -> None:
        try:
            BulletinHelper.show_error(text)
        except Exception:
            self._toast_info(text)
