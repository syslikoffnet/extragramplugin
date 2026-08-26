"""
Local Snos — локальный «снос» чужого аккаунта в exteraGram / AyuGram.

Ничего не отправляется на сервер Telegram. Меняется только то, как
этот клиент рисует выбранного пользователя: имя, аватар, снежинка
удалённого/замороженного аккаунта и текст «Аккаунт заморожен».
"""

from __future__ import annotations

import json
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
    "Локальный снос чужих аккаунтов.\n\n"
    "Кнопка **hi** в меню профиля (⋮) и команда `.snos id/@user` "
    "делают человека похожим на замороженный аккаунт — **только у тебя**.\n\n"
    "Имя → «удаленный аккаунт», аватар пропадает, появляется снежинка, "
    "в био — «Аккаунт заморожен». Откат в настройках плагина или через `.unsnos`."
)
__author__ = "@extragramplugin"
__version__ = "1.0.1"
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

COMMAND_RE = re.compile(r"^\.(snos|unsnos)(?:\s+([\s\S]+))?$", re.IGNORECASE)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
TME_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z0-9_]+)",
    re.IGNORECASE,
)

# user.#83314fca flags
FLAG_USERNAME = 1 << 3
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
    try:
        from java.lang import Long

        return Long(int(value))
    except Exception:
        return int(value)


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
        self._applying = False
        self._db_stack: List[List[Any]] = []
        self._menu_ids: List[Any] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_plugin_load(self) -> None:
        self._reload_store()
        self._install_java_hooks()
        self._install_menus()
        try:
            self.add_on_send_message_hook()
        except Exception:
            _safe_log(f"add_on_send_message_hook failed:\n{_format_exc()}")
        self.log("Local Snos loaded")
        run_on_ui_thread(self._reapply_all, 400)
        run_on_ui_thread(self._reapply_all, 1800)

    def on_plugin_unload(self) -> None:
        # Turning the plugin off should immediately undo the visual spoof,
        # but the saved list stays so enabling it again restores the snos.
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
            Input(
                key=DISPLAY_NAME_KEY,
                text="Локальное имя",
                default=DEFAULT_DISPLAY_NAME,
                subtext="Так будет называться скрытый аккаунт",
                icon="msg_edit",
                on_change=lambda _value: self._reapply_all(),
            ),
            Input(
                key=FROZEN_BIO_KEY,
                text="Текст «заморозки»",
                default=DEFAULT_FROZEN_BIO,
                subtext="Подставляется в био / about",
                icon="msg_info",
                on_change=lambda _value: self._reapply_all(),
            ),
            Divider(text="Как пользоваться"),
            Text(
                text="Кнопка hi в профиле",
                subtext="Открой чужой профиль → ⋮ → hi. Повторное нажатие откатывает.",
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
            return rows

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
                user_full = self._get_user_full(account, user_id)
                if user_full is not None:
                    self._mutate_user_full(user_full)
            self._notify_ui(account)

    def _capture_original(self, account: int, user: Any) -> None:
        user_id = _as_int(getattr(user, "id", 0))
        key = str(user_id)
        existing = self._records.get(key, {})
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

        record = {
            "user_id": user_id,
            "account": account,
            "first_name": first,
            "last_name": last,
            "username": username,
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
        name = _as_str(getattr(user, "first_name", "") or "")
        return bool(getattr(user, "deleted", False)) and name == self._display_name()

    def _mutate_user(self, user: Any) -> None:
        if not _is_tl_user(user):
            return
        if self._applying:
            return
        self._applying = True
        try:
            display = self._display_name()
            self._set_field(user, "first_name", display)
            self._set_field(user, "last_name", "")
            self._set_field(user, "username", None)
            self._set_field(user, "deleted", True)
            self._set_field(user, "premium", False)
            self._set_field(user, "verified", False)
            self._set_field(user, "photo", self._empty_photo())
            self._set_field(user, "emoji_status", None)

            flags = _as_int(getattr(user, "flags", 0))
            flags |= FLAG_DELETED
            flags &= ~FLAG_USERNAME
            flags &= ~FLAG_PHOTO
            flags &= ~FLAG_USERNAMES
            self._set_field(user, "flags", flags)

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
        self._set_field(user_full, "about", self._frozen_bio())
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
        self._set_field(user, "deleted", bool(record.get("deleted", False)))
        if "premium" in record:
            self._set_field(user, "premium", bool(record.get("premium")))
        if "verified" in record:
            self._set_field(user, "verified", bool(record.get("verified")))
        if "flags" in record:
            self._set_field(user, "flags", _as_int(record.get("flags"), 0))
        if "flags2" in record:
            self._set_field(user, "flags2", _as_int(record.get("flags2"), 0))

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
        self._set_field(user_full, "about", record.get("about") or "")

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
            "org.telegram.messenger.UserObject",
            "getUserName",
            after=self._after_get_user_name,
        )
        self._hook_all(
            "org.telegram.messenger.UserObject",
            "getFirstName",
            after=self._after_get_first_name,
        )
        self._hook_all(
            "org.telegram.ui.Components.AvatarDrawable",
            "setInfo",
            after=self._after_avatar_set_info,
        )
        self._hook_all(
            "org.telegram.messenger.MessagesStorage",
            "putUsersAndChats",
            before=self._before_put_users_and_chats,
            after=self._after_put_users_and_chats,
        )
        # Actual SQLite write lives here. The public method often only
        # posts a runnable, so hooking just putUsersAndChats is not enough.
        self._hook_all(
            "org.telegram.messenger.MessagesStorage",
            "putUsersAndChatsInternal",
            before=self._before_put_users_and_chats,
            after=self._after_put_users_and_chats,
        )

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

    def _after_get_user_full(self, param: Any) -> None:
        if not self._snos_ids:
            return
        self._maybe_touch_user_full(param.getResult())

    def _after_get_user_name(self, param: Any) -> None:
        if not self._feature_enabled() or not self._snos_ids:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        user = args[0]
        if not _is_tl_user(user):
            return
        if _as_int(getattr(user, "id", 0)) in self._snos_ids:
            param.setResult(self._display_name())

    def _after_get_first_name(self, param: Any) -> None:
        if not self._feature_enabled() or not self._snos_ids:
            return
        args = getattr(param, "args", None)
        if not args:
            return
        user = args[0]
        if not _is_tl_user(user):
            return
        if _as_int(getattr(user, "id", 0)) in self._snos_ids:
            param.setResult(self._display_name())

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
            self._set_field(target, "drawDeleted", True)

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

        run_on_ui_thread(_do)

    def _post_notifications(self, account: int, user_id: Optional[int]) -> None:
        nc = self._notification_center(account)
        if nc is None:
            return
        mask = UPDATE_MASK_FALLBACK
        if NotificationCenter is not None:
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

        events: List[Any] = []
        if NotificationCenter is not None:
            events.append((getattr(NotificationCenter, "updateInterfaces", None), [mask]))
            events.append((getattr(NotificationCenter, "dialogsNeedReload", None), []))
            events.append((getattr(NotificationCenter, "contactsDidLoad", None), []))
            events.append((getattr(NotificationCenter, "reloadInterface", None), []))
            if user_id:
                events.append(
                    (
                        getattr(NotificationCenter, "userInfoDidLoad", None),
                        [_jlong(user_id), None],
                    )
                )

        for event_id, args in events:
            if event_id is None:
                continue
            self._post_event(nc, event_id, args)

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
                try:
                    method(event_id, args)
                    return
                except Exception:
                    continue

    def _refresh_fragment(self, fragment: Any) -> None:
        if fragment is None:
            return
        for name, args in (
            ("updateProfileData", (True,)),
            ("updateProfileData", ()),
            ("updateTitle", ()),
            ("updateSubtitle", ()),
        ):
            method = getattr(fragment, name, None)
            if callable(method):
                try:
                    method(*args)
                except Exception:
                    continue
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

    def _show_confirm(
        self,
        title: str,
        message: str,
        ok_text: str,
        on_ok: Callable[..., None],
    ) -> None:
        fragment = get_last_fragment() if get_last_fragment else None
        activity = None
        if fragment is not None:
            try:
                activity = fragment.getParentActivity()
            except Exception:
                activity = None
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
