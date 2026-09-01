import asyncio
import json
import logging
import mimetypes
import os
import re
import threading
import time
import warnings
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

_CB_LIMIT = 64
_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_MAX_RETRIES = 3
_LEVEL_TAG = {logging.DEBUG: "Debug", logging.INFO: "Info", logging.WARNING: "Warning", logging.ERROR: "Error"}


class _Log:

    def __init__(self):
        self._logger = logging.getLogger("gramly")

    def setup(self, debug: bool) -> None:
        self._logger.setLevel(logging.DEBUG if debug else logging.INFO)
        if not self._logger.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
            self._logger.addHandler(h)
        self._logger.propagate = False

    def _emit(self, level: int, action: str, exc_info=None, **ctx):
        tag = _LEVEL_TAG.get(level, "?")
        parts = " ".join(f"{k}={v}" for k, v in ctx.items() if v is not None)
        line = f"{tag} {action}  {parts}".rstrip() if parts else f"{tag} {action}"
        self._logger.log(level, line, exc_info=exc_info)

    def debug(self, action: str, **ctx):
        self._emit(logging.DEBUG, action, **ctx)

    def info(self, action: str, **ctx):
        self._emit(logging.INFO, action, **ctx)

    def warning(self, action: str, **ctx):
        self._emit(logging.WARNING, action, **ctx)

    def error(self, action: str, exc_info=None, **ctx):
        self._emit(logging.ERROR, action, exc_info=exc_info, **ctx)


_log = _Log()

DEFAULT_PERMISSIONS: dict = {
    "can_send_messages": True,
    "can_send_audios": True,
    "can_send_documents": True,
    "can_send_photos": True,
    "can_send_videos": True,
    "can_send_video_notes": True,
    "can_send_voice_notes": True,
    "can_send_polls": True,
    "can_send_other_messages": True,
    "can_add_web_page_previews": True,
    "can_change_info": True,
    "can_invite_users": True,
    "can_pin_messages": True,
    "can_manage_topics": True,
}

HANDLER_UPDATES: dict = {
    "_messageHandlers": ["message"],
    "_editedHandlers": ["edited_message", "edited_business_message", "edited_channel_post"],
    "_postHandlers": ["channel_post"],
    "_callbackHandlers": ["callback_query"],
    "_inlineHandlers": ["inline_query"],
    "_mediaHandlers": ["message"],
    "_anyHandlers": ["message"],
    "_myStatusHandlers": ["my_chat_member"],
    "_memberHandlers": ["chat_member"],
    "_joinHandlers": ["chat_join_request"],
    "_reactionHandlers": ["message_reaction"],
    "_pollHandlers": ["poll_answer"],
    "_boostHandlers": ["chat_boost", "removed_chat_boost"],
    "_checkoutHandlers": ["pre_checkout_query"],
    "_paymentHandlers": ["message"],
    "_webappHandlers": ["message"],
    "_paidMediaHandlers": ["purchased_paid_media"],
    "_guestHandlers": ["guest_message"],
    "_managedHandlers": ["managed_bot"],
    "_generationStoppedHandlers": ["stopped_message_generation"],
    "_bizMsgHandlers": ["business_message"],
    "_bizEditedHandlers": ["edited_business_message"],
    "_bizDeletedHandlers": ["deleted_business_messages"],
    "_bizConnectionHandlers": ["business_connection"],
    "_commandBlocks": ["message", "callback_query"],
}
__version__ = "1.3.3"
__bot_api_version__ = "10.3"


__all__ = [
    "Gramly", "Rich",
    "btn", "row", "kbd", "userRequest", "chatRequest",
    "CallbackData", "Message", "CallbackQuery", "InlineQuery", "Payment", "PreCheckout",
    "JoinRequest", "GuestQuery", "BusinessMessage", "BusinessConnection",
    "TimerHandle", "CommandBlock", "TelegramError",
    "Obj", "User", "Chat", "SuccessfulPayment",
    "setupLogging", "chatId", "userId",
    "DEFAULT_PERMISSIONS",
    "__version__", "__bot_api_version__",
]


def setupLogging(debug: bool) -> None:
    _log.setup(debug)


def chatId(target) -> int:
    if isinstance(target, bool):
        raise TypeError(f"chat_id must be int, got bool: {target!r}")
    if isinstance(target, int):
        return target
    if isinstance(target, dict):
        if "chat" in target:
            c = target["chat"]
            return chatId(c)
        if "message" in target:
            return chatId(target["message"])
        if "id" not in target:
            raise TypeError(f"dict has no 'id' key: {list(target)[:5]!r}")
        v = target["id"]
        if not isinstance(v, int):
            raise TypeError(f"dict 'id' is not int: {v!r}")
        return v
    uci = getattr(target, "user_chat_id", None)
    if uci:
        return int(uci)
    if hasattr(target, "chat_id"):
        v = target.chat_id
        if isinstance(v, int):
            return v
        if isinstance(v, dict):
            return int(v["id"])
        if v is None:
            raise TypeError("chat_id is None")
        try:
            return int(v)
        except (TypeError, ValueError) as e:
            raise TypeError(f"chat_id is not int-like: {v!r}") from e
    if hasattr(target, "chat"):
        c = target.chat
        return c["id"] if isinstance(c, dict) else c.id
    if hasattr(target, "id"):
        v = target.id
        if isinstance(v, int):
            return v
        raise TypeError(f"object id is not int: {v!r} (type={type(target).__name__})")
    raise TypeError(
        f"Cannot extract chat_id from {type(target).__name__}; "
        "expected int, dict, or object with chat_id/chat/id attribute"
    )


def userId(target) -> Optional[int]:
    if isinstance(target, int):
        return target
    if isinstance(target, dict):
        if target.get("from"):
            f = target["from"]
            return f.get("id") if isinstance(f, dict) else getattr(f, "id", None)
        if target.get("user"):
            u = target["user"]
            return u.get("id") if isinstance(u, dict) else getattr(u, "id", None)
        v = target.get("id")
        return v if isinstance(v, int) else None
    if hasattr(target, "user_id"):
        return target.user_id
    if hasattr(target, "from_user") and target.from_user:
        f = target.from_user
        return f["id"] if isinstance(f, dict) else f.id
    if hasattr(target, "id"):
        return target.id
    return None


def toList(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def isNotModified(e: Exception) -> bool:
    return "message is not modified" in str(e).lower()


_MEDIA_META: dict = {
    "photo": ("image/jpeg", "jpg"),
    "video": ("video/mp4", "mp4"),
    "audio": ("audio/mpeg", "mp3"),
    "document": ("application/octet-stream", "bin"),
}

_EXT_TYPE: dict = {
    "jpg": "photo", "jpeg": "photo", "png": "photo",
    "gif": "photo", "webp": "photo", "bmp": "photo",
    "mp4": "video", "mov": "video", "avi": "video",
    "mkv": "video", "webm": "video",
    "mp3": "audio", "ogg": "audio", "m4a": "audio",
    "flac": "audio", "wav": "audio", "opus": "audio",
}

_EXT_MIME: dict = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "mp4": "video/mp4", "mov": "video/quicktime", "avi": "video/x-msvideo",
    "mkv": "video/x-matroska", "webm": "video/webm",
    "mp3": "audio/mpeg", "ogg": "audio/ogg", "m4a": "audio/mp4",
    "flac": "audio/flac", "wav": "audio/wav", "opus": "audio/ogg",
    "pdf": "application/pdf", "zip": "application/zip",
}


def mimeType(ext: str) -> str:
    clean = (ext or "").lower().lstrip(".")
    if clean in _EXT_MIME:
        return _EXT_MIME[clean]
    guessed, _ = mimetypes.guess_type(f"file.{clean}" if clean else "")
    return guessed or "application/octet-stream"


def _typeFromPath(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_TYPE.get(ext, "document")


def _typeFromBytes(data: bytes) -> str:
    if len(data) < 4:
        return "document"
    h = data[:12]
    if h[:3] == b"\xff\xd8\xff":
        return "photo"
    if h[:4] == b"\x89PNG":
        return "photo"
    if h[:4] == b"GIF8":
        return "photo"
    if h[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "photo"
    if h[:4] in (b"ftyp", b"\x00\x00\x00\x18", b"\x00\x00\x00\x1c"):
        return "video"
    if data[4:8] == b"ftyp":
        return "video"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "video"
    if h[:3] == b"ID3" or (h[:2] == b"\xff\xfb"):
        return "audio"
    if h[:4] == b"OggS":
        return "audio"
    if h[:4] == b"fLaC":
        return "audio"
    return "document"


class LRUCache(OrderedDict):
    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize

    def __getitem__(self, key):
        self.move_to_end(key)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self._maxsize:
            self.popitem(last=False)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class Obj:
    __slots__ = ("_d",)

    def __init__(self, d: dict):
        self._d = d

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            v = self._d[name]
        except KeyError:
            raise AttributeError(name)
        if isinstance(v, dict):
            return Obj(v)
        if isinstance(v, list):
            return [Obj(i) if isinstance(i, dict) else i for i in v]
        return v

    def __setattr__(self, name, value):
        if name == "_d":
            super().__setattr__(name, value)
        else:
            self._d[name] = value

    def __getitem__(self, key):
        return self._d[key]

    def __contains__(self, key):
        return key in self._d

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def items(self):
        return self._d.items()

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def __repr__(self):
        return repr(self._d)


def wrap(v):
    if isinstance(v, dict):
        return Obj({k: wrap(v2) for k, v2 in v.items()})
    if isinstance(v, list):
        return [wrap(i) for i in v]
    return v


def _to_raw(v):
    if isinstance(v, Obj):
        return {k: _to_raw(vv) for k, vv in v._d.items()}
    if isinstance(v, list):
        return [_to_raw(i) for i in v]
    return v


@dataclass
class User:
    id: int
    is_bot: bool
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    language_code: Optional[str] = None
    is_premium: Optional[bool] = None
    added_to_attachment_menu: Optional[bool] = None
    can_join_groups: Optional[bool] = None
    can_read_all_group_messages: Optional[bool] = None
    supports_inline_queries: Optional[bool] = None
    supports_guest_queries: Optional[bool] = None
    can_connect_to_business: Optional[bool] = None
    has_main_web_app: Optional[bool] = None
    can_manage_bots: Optional[bool] = None

    @classmethod
    def fromDict(cls, d):
        if d is None:
            return None
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Chat:
    id: int
    type: str
    title: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_forum: Optional[bool] = None
    is_direct_messages: Optional[bool] = None

    @classmethod
    def fromDict(cls, d):
        if d is None:
            return None
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class SuccessfulPayment:
    currency: str
    total_amount: int
    invoice_payload: str
    telegram_payment_charge_id: str
    provider_payment_charge_id: Optional[str] = None
    is_recurring: Optional[bool] = None
    is_first_recurring: Optional[bool] = None
    subscription_expiration_date: Optional[int] = None

    @classmethod
    def fromDict(cls, d):
        if d is None:
            return None
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class _UserRequest(dict):
    __slots__ = ()


class _ChatRequest(dict):
    __slots__ = ()


def userRequest(
    requestId: int,
    *,
    isBot: bool = None,
    isPremium: bool = None,
    maxQuantity: int = None,
    requestName: bool = None,
    requestUsername: bool = None,
    requestPhoto: bool = None,
) -> _UserRequest:
    d = _UserRequest({"request_id": requestId})
    fields = (
        ("user_is_bot", isBot),
        ("user_is_premium", isPremium),
        ("max_quantity", maxQuantity),
        ("request_name", requestName),
        ("request_username", requestUsername),
        ("request_photo", requestPhoto),
    )
    d.update((key, value) for key, value in fields if value is not None)
    return d


def chatRequest(
    requestId: int,
    *,
    isChannel: bool = None,
    isForum: bool = None,
    hasUsername: bool = None,
    isCreated: bool = None,
    botIsMember: bool = None,
    requestTitle: bool = None,
    requestUsername: bool = None,
    requestPhoto: bool = None,
    userAdminRights: dict = None,
    botAdminRights: dict = None,
) -> _ChatRequest:
    d = _ChatRequest({"request_id": requestId})
    fields = (
        ("chat_is_channel", isChannel),
        ("chat_is_forum", isForum),
        ("chat_has_username", hasUsername),
        ("chat_is_created", isCreated),
        ("bot_is_member", botIsMember),
        ("request_title", requestTitle),
        ("request_username", requestUsername),
        ("request_photo", requestPhoto),
        ("user_administrator_rights", userAdminRights),
        ("bot_administrator_rights", botAdminRights),
    )
    d.update((key, value) for key, value in fields if value is not None)
    return d


def btn(text: str, action=None, *,
    url: str = None,
    miniApp: str = None,
    loginUrl: dict = None,
    switchInline: str = None,
    switchInlineCurrent: str = None,
    switchInlineChosen: dict = None,
    copyText: str = None,
    pay: bool = False,

    requestPoll: str = None,
    requestContact: bool = False,
    requestLocation: bool = False,

    style: str = None,
    emoji: str = None,
    disabled=None,
) -> dict:
    kwarg_choices = (
        url, miniApp, loginUrl, switchInline,
        switchInlineCurrent, switchInlineChosen, copyText, pay or None,
        requestPoll, requestContact or None, requestLocation or None,
    )
    total = (1 if action is not None else 0) + sum(x is not None for x in kwarg_choices)

    if total == 0 and not disabled:
        raise ValueError(
            f"btn('{text}'): no action - pass callback_data string, "
            "userRequest(...), chatRequest(...), or a keyword action: "
            "url / miniApp / loginUrl / switchInline / switchInlineCurrent / "
            "switchInlineChosen / copyText / pay / "
            "requestPoll / requestContact / requestLocation / disabled"
        )
    if total > 1:
        raise ValueError(f"btn('{text}'): exactly one action allowed, got {total}")
    if action is not None and not isinstance(action, (str, _UserRequest, _ChatRequest)):
        raise TypeError(
            f"btn('{text}'): action must be a str, userRequest(...), or chatRequest(...); "
            f"got {type(action).__name__!r}"
        )
    if isinstance(action, str) and len(action.encode()) > _CB_LIMIT:
        raise ValueError(f"callback_data too long ({len(action.encode())} bytes): {action!r}")
    if isinstance(action, str) and not action:
        raise ValueError(f"btn('{text}'): callback_data must not be empty")
    if style is not None and style not in ("danger", "primary", "success"):
        raise ValueError(f"style must be 'danger', 'primary', or 'success', got {style!r}")
    if requestPoll is not None and requestPoll not in ("regular", "quiz"):
        raise ValueError(f"requestPoll must be 'regular' or 'quiz', got {requestPoll!r}")
    return {
        "text": text,
        "data": action if isinstance(action, str) else None,
        "url": url,
        "mini_app": miniApp,
        "login_url": loginUrl,
        "switch_inline": switchInline,
        "switch_inline_current": switchInlineCurrent,
        "switch_inline_chosen": switchInlineChosen,
        "copy_text": copyText,
        "pay": pay,
        "request_user": dict(action) if isinstance(action, _UserRequest) else None,
        "request_chat": dict(action) if isinstance(action, _ChatRequest) else None,
        "request_poll": requestPoll,
        "request_contact": requestContact or None,
        "request_location": requestLocation or None,
        "style": style,
        "emoji": emoji,
        "disabled": disabled,
    }


def row(*buttons) -> list:
    return list(buttons)


def kbd(*rows) -> list:
    return list(rows)


def _wireInlineButton(item: dict) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    data = item.get("data")
    if data is not None and len(data.encode()) > _CB_LIMIT:
        raise ValueError(f"callback_data too long ({len(data.encode())} bytes): {data!r}")
    if data is not None and not data:
        raise ValueError("callback_data cannot be empty")
    b = {"text": item["text"]}
    if item.get("disabled"):
        b["disabled"] = {}
    elif data is not None:
        b["callback_data"] = data
    if item.get("url"):
        b["url"] = item["url"]
    if item.get("mini_app"):
        b["web_app"] = {"url": item["mini_app"]}
    if item.get("login_url"):
        b["login_url"] = item["login_url"]
    if item.get("switch_inline") is not None:
        b["switch_inline_query"] = item["switch_inline"]
    if item.get("switch_inline_current") is not None:
        b["switch_inline_query_current_chat"] = item["switch_inline_current"]
    if item.get("switch_inline_chosen") is not None:
        b["switch_inline_query_chosen_chat"] = item["switch_inline_chosen"]
    if item.get("copy_text") is not None:
        b["copy_text"] = {"text": item["copy_text"]}
    if item.get("pay"):
        b["pay"] = True
    if item.get("style"):
        b["style"] = item["style"]
    if item.get("emoji"):
        b["icon_custom_emoji_id"] = item["emoji"]
    return b


def buildInlineKeyboard(rows, forceReply: bool = None) -> dict:
    if not rows:
        markup = {"inline_keyboard": []}
        if forceReply:
            markup["force_reply"] = True
        return markup
    if isinstance(rows, dict):
        rows = [[rows]]
    elif isinstance(rows, list) and rows and all(isinstance(x, dict) for x in rows):
        rows = [rows]
    elif not isinstance(rows, list):
        rows = [[rows]]
    keyboard = []
    for r in rows:
        if not isinstance(r, list):
            r = [r]
        rowBtns = []
        for item in r:
            b = _wireInlineButton(item)
            if b is not None:
                rowBtns.append(b)
        if rowBtns:
            keyboard.append(rowBtns)
    markup = {"inline_keyboard": keyboard}
    if forceReply:
        markup["force_reply"] = True
    return markup


def buildReplyKeyboard(rows, forceReply: bool = None) -> dict:
    if rows is False:
        return {"remove_keyboard": True}
    if isinstance(rows, (str, dict)):
        rows = [[rows]]
    elif isinstance(rows, list) and rows and all(isinstance(x, (str, dict)) for x in rows):
        rows = [rows]
    keyboard = []
    for r in rows:
        if isinstance(r, (str, dict)):
            r = [r]
        rowBtns = []
        for b in r:
            if isinstance(b, str):
                rowBtns.append({"text": b})
            elif isinstance(b, dict):
                btnObj = {"text": b["text"]}
                if b.get("style"):
                    btnObj["style"] = b["style"]
                if b.get("emoji"):
                    btnObj["icon_custom_emoji_id"] = b["emoji"]
                if b.get("request_contact"):
                    btnObj["request_contact"] = True
                if b.get("request_location"):
                    btnObj["request_location"] = True
                if b.get("request_poll"):
                    btnObj["request_poll"] = {"type": b["request_poll"]}
                if b.get("request_user"):
                    btnObj["request_users"] = b["request_user"]
                if b.get("request_chat"):
                    btnObj["request_chat"] = b["request_chat"]
                if b.get("mini_app"):
                    btnObj["web_app"] = {"url": b["mini_app"]}
                rowBtns.append(btnObj)
        if rowBtns:
            keyboard.append(rowBtns)
    markup = {"keyboard": keyboard, "resize_keyboard": True}
    if forceReply:
        markup["force_reply"] = True
    return markup


def _compileRuns(parts) -> list:
    if parts is None:
        return []
    if isinstance(parts, str) or (isinstance(parts, dict) and "type" in parts):
        parts = (parts,)
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            out.append(p)
        elif p is not None:
            out.append(str(p))
    return out


def _asBlocks(parts) -> list:
    if isinstance(parts, dict) and "blocks" in parts:
        return parts["blocks"]
    items = parts if isinstance(parts, (list, tuple)) else (parts,)
    out = []
    for it in items:
        if isinstance(it, dict) and "type" in it:
            out.append(it)
        else:
            out.append({"type": "paragraph", "text": _compileRuns(it)})
    return out


def _resolveMediaSource(item, index: int = 0) -> dict:
    if hasattr(item, "read"):
        item = (item.read(), getattr(item, "name", None))
    if isinstance(item, tuple) and len(item) == 2:
        data, hint = item
        if isinstance(data, (bytes, bytearray)):
            mtype = _typeFromPath(hint) if isinstance(hint, str) and hint else _typeFromBytes(bytes(data))
            ct, ext = _MEDIA_META.get(mtype, _MEDIA_META["document"])
            fname = hint if (isinstance(hint, str) and "." in hint) else f"file{index}.{ext}"
            return {"media": "", "_bytes": bytes(data), "_filename": fname, "_content_type": ct}
        item = data
    if isinstance(item, (bytes, bytearray)):
        mtype = _typeFromBytes(bytes(item))
        ct, ext = _MEDIA_META.get(mtype, _MEDIA_META["document"])
        return {"media": "", "_bytes": bytes(item), "_filename": f"file{index}.{ext}", "_content_type": ct}
    s = str(item)
    if ("/" in s or "\\" in s or s.startswith(".")) and os.path.isfile(s):
        try:
            with open(s, "rb") as f:
                data = f.read()
            fname = os.path.basename(s)
            mtype = _typeFromPath(fname)
            ct, _ext = _MEDIA_META.get(mtype, _MEDIA_META["document"])
            return {"media": "", "_bytes": data, "_filename": fname, "_content_type": ct}
        except Exception as e:
            _log.warning("richMedia", file=s, err=e)
    return {"media": s}


def _resolveInputMedia(item, mediaType: str = None, index: int = 0, documentFallback: str = "photo", extensionlessOnly: bool = False, **fields) -> dict:
    resolved = _resolveMediaSource(item, index)
    if mediaType is None:
        if "_bytes" in resolved:
            mediaType = _typeFromBytes(resolved["_bytes"]) if not resolved.get("_filename") \
                else _typeFromPath(resolved["_filename"])
        else:
            hasExtension = "." in str(item).rsplit("/", 1)[-1]
            mediaType = _typeFromPath(str(item))
            if mediaType == "document" and documentFallback and not (extensionlessOnly and hasExtension):
                mediaType = documentFallback
    entry = {"type": mediaType, "media": resolved["media"]}
    if "_bytes" in resolved:
        entry["_bytes"], entry["_filename"], entry["_content_type"] = (
            resolved["_bytes"], resolved["_filename"], resolved["_content_type"])
    entry.update({k: v for k, v in fields.items() if v is not None})
    return entry


def _compileMediaMap(media: dict) -> list:
    if not media:
        return []
    out = []
    for name, item in media.items():
        wrapped = item if isinstance(item, dict) and "type" in item else _resolveInputMedia(item)
        out.append({"id": name, "media": wrapped})
    return out


def _collectRichAttachments(obj):
    files = {}

    def walk(node):
        if isinstance(node, dict):
            if "_bytes" in node:
                key = f"file{len(files)}"
                files[key] = (
                    node.get("_filename", f"{key}.bin"),
                    node["_bytes"],
                    node.get("_content_type", "application/octet-stream"),
                )
                cleaned = {k: v for k, v in node.items() if not k.startswith("_")}
                cleaned["media"] = f"attach://{key}"
                return cleaned
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(obj), files


_RUN_STYLES = ("bold", "italic", "underline", "strike", "spoiler", "mono", "mark", "sub", "sup")
_RUN_STYLE_WIRE = {
    "bold": "bold", "italic": "italic", "underline": "underline", "strike": "strikethrough",
    "spoiler": "spoiler", "mono": "code", "mark": "marked", "sub": "subscript", "sup": "superscript",
}


def run(text: str, style: str = None, *,
    link: str = None,
    mention: int = None,
    username: str = None,
    hashtag: str = None,
    cashtag: str = None,
    botCommand: str = None,
    email: str = None,
    phone: str = None,
    bankCard: str = None,
    date: int = None,
    dateFormat: str = None,
    math: bool = False,
) -> dict:
    kwarg_choices = (link, mention, username, hashtag, cashtag, botCommand, email, phone, bankCard, date, math or None)
    total = (1 if style is not None else 0) + sum(x is not None for x in kwarg_choices)

    if total == 0:
        return text
    if total > 1:
        raise ValueError(
            f"run({text!r}): exactly one of style/link/mention/username/hashtag/"
            f"cashtag/botCommand/email/phone/bankCard/date/math allowed, got {total}"
        )
    if style is not None:
        if style not in _RUN_STYLES:
            raise ValueError(f"run({text!r}): style must be one of {_RUN_STYLES}, got {style!r}")
        return {"type": _RUN_STYLE_WIRE[style], "text": [text]}
    if link is not None:
        return {"type": "url", "text": [text], "url": link}
    if mention is not None:
        return {"type": "text_mention", "text": [text], "user": {"id": mention}}
    if username is not None:
        return {"type": "mention", "text": [text], "username": username.lstrip("@")}
    if hashtag is not None:
        return {"type": "hashtag", "text": [text], "hashtag": hashtag if hashtag.startswith("#") else f"#{hashtag}"}
    if cashtag is not None:
        return {"type": "cashtag", "text": [text], "cashtag": cashtag if cashtag.startswith("$") else f"${cashtag}"}
    if botCommand is not None:
        return {"type": "bot_command", "text": [text], "bot_command": botCommand if botCommand.startswith("/") else f"/{botCommand}"}
    if email is not None:
        return {"type": "email_address", "text": [text], "email_address": email}
    if phone is not None:
        return {"type": "phone_number", "text": [text], "phone_number": phone}
    if bankCard is not None:
        return {"type": "bank_card_number", "text": [text], "bank_card_number": bankCard}
    if date is not None:
        return {"type": "date_time", "unix_time": date, "date_time_format": dateFormat}
    if math:
        return {"type": "mathematical_expression", "text": [text]}


def emojiRun(customEmojiId: str, alt: str = "🙂") -> dict:
    return {"type": "custom_emoji", "custom_emoji_id": customEmojiId, "alternative_text": alt}


def buttonRun(button: dict) -> dict:
    wired = _wireInlineButton(button)
    if wired is None:
        raise TypeError(f"buttonRun(): expected a btn(...) object, got {type(button).__name__!r}")
    return {"type": "button", "button": wired}


def anchor(name: str) -> dict:
    return {"type": "anchor", "name": name}


def anchorLink(text: str, name: str) -> dict:
    return {"type": "anchor_link", "text": [text], "anchor_name": name}


def ref(name: str) -> dict:
    return {"type": "reference", "name": name}


def refLink(text: str, name: str) -> dict:
    return {"type": "reference_link", "text": [text], "reference_name": name}


def heading(*parts, size: int = 1) -> dict:
    return {"type": "heading", "text": _compileRuns(parts), "size": max(1, min(size, 6))}


def paragraph(*parts) -> dict:
    return {"type": "paragraph", "text": _compileRuns(parts)}


def footer(*parts) -> dict:
    return {"type": "footer", "text": _compileRuns(parts)}


def quote(*parts, credit=None) -> dict:
    d = {"type": "blockquote", "blocks": _asBlocks(parts)}
    if credit:
        d["credit"] = _compileRuns(credit)
    return d


def expandableQuote(*parts, credit=None, expanded: bool = False) -> dict:
    d = {"type": "expandable_blockquote", "blocks": _asBlocks(parts)}
    if credit:
        d["credit"] = _compileRuns(credit)
    if expanded:
        d["is_expanded"] = True
    return d


def pullQuote(*parts, credit=None) -> dict:
    d = {"type": "pullquote", "text": _compileRuns(parts)}
    if credit:
        d["credit"] = _compileRuns(credit)
    return d


def item(*parts, checkbox: bool = None, checked: bool = None,
         value: int = None, labelType: str = None, children: list = None) -> dict:
    blocks = list(_asBlocks(parts))
    if children:
        blocks.append({"type": "list", "items": [
            c if isinstance(c, dict) and "blocks" in c else item(c) for c in children
        ]})
    d = {"blocks": blocks}
    if checkbox or checked:
        d["has_checkbox"] = True
    if checked:
        d["is_checked"] = True
    if value is not None:
        d["value"] = value
    if labelType is not None:
        d["type"] = labelType
    return d


def bulletList(*items) -> dict:
    compiled = [it if isinstance(it, dict) and "blocks" in it else item(it) for it in items]
    return {"type": "list", "items": compiled}


def codeBlock(text: str, language: str = None) -> dict:
    d = {"type": "pre", "text": _compileRuns(text)}
    if language:
        d["language"] = language
    return d


def mathBlock(expression: str) -> dict:
    return {"type": "mathematical_expression", "expression": expression}


def divider() -> dict:
    return {"type": "divider"}


def buttonsBlock(*buttons, align: str = None) -> dict:
    flat = []
    def _collect(item):
        if isinstance(item, list):
            for sub in item:
                _collect(sub)
        elif item is not None:
            flat.append(item)
    for b in buttons:
        _collect(b)
    wired = [b for b in (_wireInlineButton(item) for item in flat) if b is not None]
    d = {"type": "buttons", "buttons": wired}
    if align is not None:
        if align not in ("left", "center", "right"):
            raise ValueError(f"buttonsBlock(): align must be left/center/right, got {align!r}")
        d["align"] = align
    return d


def thinking(*parts) -> dict:
    return {"type": "thinking", "text": _compileRuns(parts)}


def details(summary, *parts, open: bool = False) -> dict:
    summaryParts = summary if isinstance(summary, (list, tuple)) else (summary,)
    d = {"type": "details", "summary": _compileRuns(summaryParts), "blocks": _asBlocks(parts)}
    if open:
        d["is_open"] = True
    return d


def table(rows: list, headerRow: bool = True, bordered: bool = None,
          striped: bool = None, compact: bool = None, caption=None) -> dict:
    def cell(c, isHeaderRow: bool):
        if isinstance(c, dict):
            d = {"align": c.get("align", "left"), "valign": c.get("valign", "middle")}
            if "text" in c and c["text"] is not None:
                d["text"] = _compileRuns(c["text"])
            if c.get("header", isHeaderRow):
                d["is_header"] = True
            if c.get("colspan"):
                d["colspan"] = c["colspan"]
            if c.get("rowspan"):
                d["rowspan"] = c["rowspan"]
            return d
        parts = c if isinstance(c, (list, tuple)) else (c,)
        d = {"text": _compileRuns(parts), "align": "left", "valign": "middle"}
        if isHeaderRow:
            d["is_header"] = True
        return d

    cells = [[cell(c, headerRow and i == 0) for c in r] for i, r in enumerate(rows)]
    d = {"type": "table", "cells": cells}
    if bordered is not None:
        d["is_bordered"] = bordered
    if striped is not None:
        d["is_striped"] = striped
    if compact is not None:
        d["is_compact"] = compact
    if caption:
        d["caption"] = _compileRuns(caption)
    return d


def _caption(caption, credit=None) -> Optional[dict]:
    if caption is None and credit is None:
        return None
    d = {"text": _compileRuns(caption) if caption else []}
    if credit is not None:
        d["credit"] = _compileRuns(credit)
    return d


def mapBlock(latitude: float, longitude: float, zoom: int = 15,
             width: int = 400, height: int = 300, caption=None, credit=None) -> dict:
    d = {
        "type": "map",
        "location": {"latitude": latitude, "longitude": longitude},
        "zoom": max(0, min(zoom, 24)), "width": width, "height": height,
    }
    cap = _caption(caption, credit)
    if cap:
        d["caption"] = cap
    return d


def _mediaBlockOf(wireType: str, blockKey: str, media, caption=None, credit=None,
                   index: int = 0, spoiler: bool = None, **mediaFields) -> dict:
    resolved = _resolveInputMedia(media, wireType, index, has_spoiler=spoiler or None, **mediaFields)
    d = {"type": wireType, blockKey: resolved}
    cap = _caption(caption, credit)
    if cap:
        d["caption"] = cap
    return d


def photo(media, caption=None, credit=None, spoiler: bool = False) -> dict:
    return _mediaBlockOf("photo", "photo", media, caption, credit, spoiler=spoiler)


def video(media, caption=None, credit=None, spoiler: bool = False, **kwargs) -> dict:
    return _mediaBlockOf("video", "video", media, caption, credit, spoiler=spoiler, **kwargs)


def animation(media, caption=None, credit=None, spoiler: bool = False, **kwargs) -> dict:
    return _mediaBlockOf("animation", "animation", media, caption, credit, spoiler=spoiler, **kwargs)


def audio(media, caption=None, credit=None, title: str = None, performer: str = None) -> dict:
    return _mediaBlockOf("audio", "audio", media, caption, credit, title=title, performer=performer)


def voiceNote(media, caption=None, credit=None, duration: int = None) -> dict:
    return _mediaBlockOf("voice_note", "voice_note", media, caption, credit, duration=duration)


def document(media, caption=None, credit=None, fileName: str = None) -> dict:
    return _mediaBlockOf("document", "document", media, caption, credit, file_name=fileName)


def _mediaGroup(kind: str, items: list, caption=None, credit=None) -> dict:
    blocks = []
    for i, it in enumerate(items):
        if isinstance(it, dict) and "type" in it:
            blocks.append(it)
        else:
            blocks.append(_mediaBlockOf("photo", "photo", it, index=i))
    d = {"type": kind, "blocks": blocks}
    cap = _caption(caption, credit)
    if cap:
        d["caption"] = cap
    return d


def collage(items: list, caption=None, credit=None) -> dict:
    return _mediaGroup("collage", items, caption, credit)


def slideshow(items: list, caption=None, credit=None) -> dict:
    return _mediaGroup("slideshow", items, caption, credit)


def _hasThinking(blocks: list) -> bool:
    return any(b.get("type") == "thinking" for b in blocks)


def rich(*blocks, forDraft: bool = False) -> dict:
    compiled = [dict(b) for b in blocks]
    if not forDraft and _hasThinking(compiled):
        raise TelegramError(
            "rich(): a thinking(...) block was found, but it's only accepted by "
            "bot.messageDraft(...) — pass rich(..., forDraft=True) if this is going "
            "to a draft, or drop thinking(...) to send it normally."
        )
    return {"blocks": compiled}


def richMarkdown(text: str, media: dict = None, rtl: bool = None, skipEntityDetection: bool = None) -> dict:
    out = {"markdown": text}
    if media:
        out["media"] = _compileMediaMap(media)
    if rtl is not None:
        out["is_rtl"] = rtl
    if skipEntityDetection is not None:
        out["skip_entity_detection"] = skipEntityDetection
    return out


def richHtml(text: str, media: dict = None, rtl: bool = None, skipEntityDetection: bool = None) -> dict:
    out = {"html": text}
    if media:
        out["media"] = _compileMediaMap(media)
    if rtl is not None:
        out["is_rtl"] = rtl
    if skipEntityDetection is not None:
        out["skip_entity_detection"] = skipEntityDetection
    return out


_SLOT_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _slotNames(node, order: list) -> None:
    if isinstance(node, str):
        for m in _SLOT_PATTERN.finditer(node):
            name = m.group(1)
            if name not in order:
                order.append(name)
    elif isinstance(node, dict):
        for v in node.values():
            _slotNames(v, order)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _slotNames(v, order)


def _fillSlots(node, data: dict):
    if isinstance(node, str):
        def _sub(m: "re.Match") -> str:
            name = m.group(1)
            if name not in data:
                raise KeyError(f"missing value for slot '{{{name}}}'")
            return str(data[name])
        return _SLOT_PATTERN.sub(_sub, node)
    if isinstance(node, dict):
        return {k: _fillSlots(v, data) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_fillSlots(v, data) for v in node]
    return node


class TelegramError(Exception):
    def __init__(self, description: str, errorCode: int = 0, retryAfter: int = None):
        super().__init__(description)
        self.description = description
        self.error_code = errorCode
        self.retry_after = retryAfter

    def __str__(self) -> str:
        return f"{self.error_code}: {self.description}" if self.error_code else self.description


class RichTemplate:
    __slots__ = ("_blocks", "_forDraft", "_slotOrder")

    def __init__(self, blocks: tuple, forDraft: bool):
        self._blocks = blocks
        self._forDraft = forDraft
        order: list = []
        for b in blocks:
            _slotNames(b, order)
        self._slotOrder = tuple(order)

    def __call__(self, *args, **kwargs) -> dict:
        if len(args) > len(self._slotOrder):
            raise TypeError(
                f"template takes {len(self._slotOrder)} positional slot(s) "
                f"{self._slotOrder}, got {len(args)}"
            )
        data = dict(zip(self._slotOrder, args))
        for name, value in kwargs.items():
            if name in data:
                raise TypeError(f"slot '{name}' passed both positionally and by name")
            data[name] = value
        filled = [_fillSlots(b, data) for b in self._blocks]
        return rich(*filled, forDraft=self._forDraft)

    @property
    def slots(self) -> tuple:
        return self._slotOrder

    def __repr__(self) -> str:
        return f"RichTemplate(slots={self._slotOrder!r})"


class Rich:
    __slots__ = ()

    def __new__(cls, *blocks, forDraft=False):
        order: list = []
        for b in blocks:
            _slotNames(b, order)
        if order:
            return RichTemplate(blocks, forDraft)
        return rich(*blocks, forDraft=forDraft)

    Build = staticmethod(rich)
    Heading = staticmethod(heading)
    Paragraph = staticmethod(paragraph)
    Footer = staticmethod(footer)
    Quote = staticmethod(quote)
    ExpandableQuote = staticmethod(expandableQuote)
    PullQuote = staticmethod(pullQuote)
    Item = staticmethod(item)
    BulletList = staticmethod(bulletList)
    CodeBlock = staticmethod(codeBlock)
    MathBlock = staticmethod(mathBlock)
    Anchor = staticmethod(anchor)
    Divider = staticmethod(divider)
    Thinking = staticmethod(thinking)
    Details = staticmethod(details)
    Table = staticmethod(table)
    Map = staticmethod(mapBlock)
    Run = staticmethod(run)
    EmojiRun = staticmethod(emojiRun)
    ButtonRun = staticmethod(buttonRun)
    Buttons = staticmethod(buttonsBlock)
    AnchorLink = staticmethod(anchorLink)
    Ref = staticmethod(ref)
    RefLink = staticmethod(refLink)
    Markdown = staticmethod(richMarkdown)
    Html = staticmethod(richHtml)
    Photo = staticmethod(photo)
    Video = staticmethod(video)
    Animation = staticmethod(animation)
    Audio = staticmethod(audio)
    Voice = staticmethod(voiceNote)
    Document = staticmethod(document)
    Collage = staticmethod(collage)
    Slideshow = staticmethod(slideshow)


class CallbackData:
    __slots__ = ("raw", "parts")

    def __init__(self, data: str):
        self.raw = data
        self.parts = data.split(":")

    @property
    def owner(self) -> Optional[int]:
        try:
            return int(self.parts[0])
        except (IndexError, ValueError):
            return None

    @property
    def action(self) -> str:
        return self.parts[1] if len(self.parts) > 1 else self.parts[0]

    def get(self, index: int, cast=str, default=None):
        try:
            return cast(self.parts[index])
        except (IndexError, ValueError, TypeError):
            return default

    def extra(self, index: int = 0, cast=str, default=None):
        return self.get(2 + index, cast, default)

    def __getitem__(self, i):
        return self.parts[i]

    def __len__(self) -> int:
        return len(self.parts)

    def __repr__(self) -> str:
        return f"CallbackData({self.raw!r})"


class ArgsMixin:
    def arg(self, index: int, cast=str, default=None):
        try:
            return cast(self.args[index])
        except (IndexError, ValueError, TypeError):
            return default

    def has(self, n: int = 1) -> bool:
        return len(self.args) >= n

    def argInt(self, index: int, default: int = None) -> Optional[int]:
        return self.arg(index, cast=int, default=default)

    def argFloat(self, index: int, default: float = None) -> Optional[float]:
        return self.arg(index, cast=float, default=default)

    def argsJoined(self, sep: str = " ") -> str:
        return sep.join(self.args)


class RawAttrMixin:
    __slots__ = ()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._raw:
            raise AttributeError(name)
        v = self._raw[name]
        return wrap(v) if isinstance(v, (dict, list)) else v


class Message(ArgsMixin, RawAttrMixin):
    __slots__ = ("_raw", "args", "match", "from_user", "chat", "text", "message_id")

    def __init__(self, raw: dict, args: list, match=None):
        self._raw = raw
        self.args = args
        self.match = match
        self.from_user = User.fromDict(raw.get("from"))
        self.chat = Chat.fromDict(raw.get("chat")) or Chat(id=0, type="unknown")
        self.text = raw.get("text") or raw.get("caption")
        self.message_id = raw.get("message_id")

    @property
    def user_id(self) -> Optional[int]:
        return self.from_user.id if self.from_user else None

    @property
    def userId(self) -> Optional[int]:
        return self.user_id

    @property
    def chat_id(self) -> int:
        return self.chat.id

    @property
    def chatId(self) -> int:
        return self.chat_id

    @property
    def chat_type(self) -> Optional[str]:
        return self.chat.type

    @property
    def chatType(self) -> Optional[str]:
        return self.chat_type

    @property
    def messageId(self) -> Optional[int]:
        return self.message_id

    @property
    def ephemeralId(self) -> Optional[int]:
        return self._raw.get("ephemeral_message_id")

    @property
    def isEphemeral(self) -> bool:
        return self.ephemeralId is not None

    @property
    def receiverUser(self):
        return User.fromDict(self._raw.get("receiver_user"))

    @property
    def isPrivate(self) -> bool:
        return self.chat_type == "private"

    @property
    def isGroup(self) -> bool:
        return self.chat_type in ("group", "supergroup")

    @property
    def isChannel(self) -> bool:
        return self.chat_type == "channel"

    @property
    def forwardFrom(self):
        return User.fromDict(self._raw.get("forward_from"))

    @property
    def replyTo(self):
        r = self._raw.get("reply_to_message")
        return Message(r, []) if r else None

    @property
    def payment(self):
        p = self._raw.get("successful_payment")
        if p:
            return Payment(SuccessfulPayment.fromDict(p))
        return None

    @property
    def webAppData(self):
        return wrap(self._raw.get("web_app_data"))

    @property
    def userShared(self):
        return wrap(self._raw.get("users_shared") or self._raw.get("user_shared"))

    @property
    def chatShared(self):
        return wrap(self._raw.get("chat_shared"))

    @property
    def checklist(self):
        return wrap(self._raw.get("checklist"))

    @property
    def checklistTasksAdded(self):
        return wrap(self._raw.get("checklist_tasks_added"))

    @property
    def livePhoto(self):
        return wrap(self._raw.get("live_photo"))

    @property
    def communityChatAdded(self):
        return wrap(self._raw.get("community_chat_added"))

    @property
    def communityChatJoined(self):
        return wrap(self._raw.get("community_chat_joined"))

    @property
    def communityChatRemoved(self):
        return wrap(self._raw.get("community_chat_removed"))

    @property
    def isGuest(self) -> bool:
        return self._raw.get("guest_query_id") is not None

    @property
    def guestQueryId(self) -> Optional[str]:
        return self._raw.get("guest_query_id")

    @property
    def guestCallerUser(self):
        return User.fromDict(self._raw.get("guest_bot_caller_user"))

    @property
    def guestCallerChat(self):
        return Chat.fromDict(self._raw.get("guest_bot_caller_chat"))

    def __repr__(self) -> str:
        return f"Message({self.message_id}, {self.text!r})"


class CallbackQuery(ArgsMixin, RawAttrMixin):
    __slots__ = ("_raw", "cb", "args", "data", "message", "from_user", "chat", "id", "_answered", "bc_id")

    def __init__(self, raw: dict, cb: CallbackData, args: list = None):
        self._raw = raw
        self.cb = cb
        self.args = args if args is not None else cb.parts[2:]
        self.data = raw.get("data")
        self.message = wrap(raw.get("message", {}))
        self.from_user = User.fromDict(raw.get("from"))
        msg = raw.get("message") or {}
        self.chat = Chat.fromDict(msg.get("chat")) if msg.get("chat") else None
        self.id = raw.get("id")
        self.bc_id = msg.get("business_connection_id")
        self._answered = False

    @property
    def businessConnectionId(self) -> Optional[str]:
        return self.bc_id

    @property
    def bcId(self) -> Optional[str]:
        return self.bc_id

    @property
    def user_id(self) -> Optional[int]:
        return self.from_user.id if self.from_user else None

    @property
    def userId(self) -> Optional[int]:
        return self.user_id

    @property
    def chat_id(self) -> Optional[int]:
        return self.chat.id if self.chat else None

    @property
    def chatId(self) -> Optional[int]:
        return self.chat_id

    @property
    def message_id(self) -> int:
        return getattr(self.message, "message_id", None)

    @property
    def messageId(self) -> Optional[int]:
        return self.message_id

    @property
    def ephemeralId(self) -> Optional[int]:
        return getattr(self.message, "ephemeral_message_id", None)

    @property
    def isEphemeral(self) -> bool:
        return self.ephemeralId is not None

    @property
    def isInline(self) -> bool:
        return self._raw.get("inline_message_id") is not None

    @property
    def inlineMessageId(self) -> Optional[str]:
        return self._raw.get("inline_message_id")

    def __repr__(self) -> str:
        return f"CallbackQuery({self.id!r}, {self.data!r})"


class Payment:
    __slots__ = ("_raw",)

    def __init__(self, raw: SuccessfulPayment):
        self._raw = raw

    @property
    def currency(self) -> str:
        return self._raw.currency

    @property
    def totalAmount(self) -> int:
        return self._raw.total_amount

    @property
    def stars(self) -> Optional[int]:
        return self._raw.total_amount if self._raw.currency == "XTR" else None

    @property
    def payload(self) -> str:
        return self._raw.invoice_payload

    @property
    def chargeId(self) -> str:
        return self._raw.telegram_payment_charge_id

    @property
    def providerChargeId(self) -> str:
        return self._raw.provider_payment_charge_id

    @property
    def isRecurring(self) -> bool:
        return bool(self._raw.is_recurring)

    @property
    def isFirstRecurring(self) -> bool:
        return bool(self._raw.is_first_recurring)

    @property
    def subscriptionExpiration(self) -> Optional[int]:
        return self._raw.subscription_expiration_date

    def __repr__(self) -> str:
        return f"Payment({self.currency} {self.totalAmount} payload={self.payload!r})"


class PreCheckout:
    __slots__ = ("_raw", "_gramly")

    def __init__(self, raw: dict, gramly):
        self._raw = raw
        self._gramly = gramly

    @property
    def id(self) -> str:
        return self._raw.get("id")

    @property
    def fromUser(self):
        return User.fromDict(self._raw.get("from"))

    @property
    def user_id(self) -> Optional[int]:
        u = self.fromUser
        return u.id if u else None

    @property
    def userId(self) -> Optional[int]:
        return self.user_id

    @property
    def currency(self) -> str:
        return self._raw.get("currency")

    @property
    def totalAmount(self) -> int:
        return self._raw.get("total_amount")

    @property
    def payload(self) -> str:
        return self._raw.get("invoice_payload")

    def accept(self):
        return self._gramly._api_call("answerPreCheckoutQuery", pre_checkout_query_id=self.id, ok=True)

    def reject(self, reason: str):
        return self._gramly._api_call("answerPreCheckoutQuery", pre_checkout_query_id=self.id, ok=False, error_message=reason)

    def __repr__(self) -> str:
        return f"PreCheckout({self.currency} {self.totalAmount} payload={self.payload!r})"


class JoinRequest(RawAttrMixin):
    __slots__ = ("_raw",)

    def __init__(self, raw: dict):
        self._raw = raw

    @property
    def user(self):
        return User.fromDict(self._raw.get("from"))

    @property
    def fromUser(self):
        return self.user

    @property
    def user_id(self) -> Optional[int]:
        u = self.user
        return u.id if u else None

    @property
    def userId(self) -> Optional[int]:
        return self.user_id

    @property
    def chat(self):
        return Chat.fromDict(self._raw.get("chat"))

    @property
    def chat_id(self) -> int:
        return self.chat.id

    @property
    def chatId(self) -> int:
        return self.chat_id

    @property
    def userChatId(self) -> Optional[int]:
        v = self._raw.get("user_chat_id")
        return int(v) if v else None

    @property
    def queryId(self) -> Optional[str]:
        return self._raw.get("query_id")

    @property
    def inviteLink(self):
        return wrap(self._raw.get("invite_link"))

    @property
    def date(self) -> Optional[int]:
        return self._raw.get("date")

    def __repr__(self) -> str:
        return f"JoinRequest(user={self.user_id}, chat={self.chat_id})"


class BusinessConnection(RawAttrMixin):
    __slots__ = ("_raw",)

    def __init__(self, raw: dict):
        self._raw = raw

    @property
    def id(self) -> str:
        return self._raw.get("id")

    @property
    def user(self):
        return User.fromDict(self._raw.get("user"))

    @property
    def userChatId(self) -> Optional[int]:
        return self._raw.get("user_chat_id")

    @property
    def date(self) -> int:
        return self._raw.get("date")

    @property
    def isEnabled(self) -> bool:
        return bool(self._raw.get("is_enabled"))

    @property
    def canReply(self) -> bool:
        r = wrap(self._raw.get("rights"))
        return bool(r and r.can_reply)

    @property
    def rights(self):
        return wrap(self._raw.get("rights"))

    def __repr__(self) -> str:
        return f"BusinessConnection(id={self.id!r}, enabled={self.isEnabled})"


class BusinessMessage(ArgsMixin, RawAttrMixin):
    __slots__ = (
        "_raw", "_gramly", "args", "match",
        "bc_id", "chat_id", "message_id", "text", "from_user", "chat",
    )

    def __init__(self, raw: dict, gramly, args: list = None, match=None):
        self._raw = raw
        self._gramly = gramly
        self.args = args or []
        self.match = match
        self.bc_id = raw.get("business_connection_id")
        self.text = raw.get("text") or raw.get("caption")
        self.chat = Chat.fromDict(raw.get("chat")) or Chat(id=0, type="unknown")
        self.chat_id = self.chat.id
        self.message_id = raw.get("message_id")
        self.from_user = User.fromDict(raw.get("from"))

    @property
    def businessConnectionId(self) -> Optional[str]:
        return self.bc_id

    @property
    def bcId(self) -> Optional[str]:
        return self.bc_id

    @property
    def messageId(self) -> Optional[int]:
        return self.message_id

    @property
    def chatId(self) -> int:
        return self.chat_id

    @property
    def user_id(self) -> Optional[int]:
        return self.from_user.id if self.from_user else None

    @property
    def userId(self) -> Optional[int]:
        return self.user_id

    @property
    def isOwner(self) -> bool:
        conn = self._gramly.businessConnection(self.bc_id)
        return bool(conn and conn.user and self.from_user and self.from_user.id == conn.user.id)

    def reply(self, text: str, inline=None, keyboard=None, **kwargs):
        return self._gramly.businessSend(self.chat_id, text, self.bc_id, inline=inline, keyboard=keyboard, **kwargs)

    def typing(self, action: str = "typing"):
        return self._gramly.businessAction(self.chat_id, self.bc_id, action)

    def read(self):
        return self._gramly.businessRead(self.bc_id, self.chat_id, self.message_id)

    def delete(self, messageIds: list = None):
        ids = messageIds if messageIds is not None else [self.message_id]
        return self._gramly.businessDelete(self.bc_id, self.chat_id, ids)

    def pin(self, notify: bool = False):
        return self._gramly.pin(self, notify=notify)

    def unpin(self):
        return self._gramly.unpin(self.chat_id, self.message_id, bcId=self.bc_id)

    def __repr__(self) -> str:
        return f"BusinessMessage(chat={self.chat_id}, bc_id={self.bc_id!r}, text={self.text!r})"


class GuestQuery:
    __slots__ = ("_raw", "_gramly")

    def __init__(self, raw: dict, gramly):
        self._raw = raw
        self._gramly = gramly

    @property
    def id(self) -> str:
        return self._raw.get("guest_query_id")

    @property
    def fromUser(self):
        return User.fromDict(self._raw.get("guest_bot_caller_user"))

    @property
    def fromChat(self):
        return Chat.fromDict(self._raw.get("guest_bot_caller_chat"))

    @property
    def user_id(self) -> Optional[int]:
        u = self.fromUser
        return u.id if u else None

    @property
    def userId(self) -> Optional[int]:
        return self.user_id

    @property
    def text(self) -> Optional[str]:
        return self._raw.get("text")

    def answer(self, text: str, parseMode: str = None, **kwargs):
        return self._gramly._api_call("answerGuestQuery", guest_query_id=self.id, message={"text": text, "parse_mode": parseMode or self._gramly.parse_mode, **kwargs})

    def __repr__(self) -> str:
        return f"GuestQuery(id={self.id!r})"


class InlineQuery:
    __slots__ = ("_raw", "_gramly", "id", "text", "from_user", "offset", "query")

    def __init__(self, raw: dict, gramly):
        self._raw = raw
        self._gramly = gramly
        self.id = raw.get("id")
        self.query = raw.get("query", "")
        self.text = self.query.strip()
        self.from_user = User.fromDict(raw.get("from"))
        self.offset = raw.get("offset", "")

    @property
    def user_id(self) -> Optional[int]:
        return self.from_user.id if self.from_user else None

    @property
    def userId(self) -> Optional[int]:
        return self.user_id

    def article(self, title: str, text: str, description: str = None, thumbUrl: str = None, resultId: str = None, **kwargs) -> dict:
        return {
            "type": "article", "id": resultId or title, "title": title,
            "description": description, "thumbnail_url": thumbUrl,
            "input_message_content": {"message_text": text, "parse_mode": self._gramly.parse_mode},
            **kwargs,
        }

    def answer(self, results: list, cacheTime: int = 30, personal: bool = True):
        return self._gramly.answerInlineQuery(self, results, cacheTime=cacheTime, isPersonal=personal)

    def __repr__(self) -> str:
        return f"InlineQuery({self.id!r}, {self.text!r})"


def _in_async_task() -> bool:
    try:
        return asyncio.current_task() is not None
    except RuntimeError:
        return False


class AsyncAPIClient:
    def __init__(self, token: str, connectTimeout: int = 10, readTimeout: int = 30):
        self._token = token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=connectTimeout, read=readTimeout, write=30, pool=5),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    def _url(self, method: str) -> str:
        return _API_BASE.format(token=self._token, method=method)

    def _parse(self, raw: bytes):
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise TelegramError("non-JSON response", 0)
        if not result.get("ok"):
            p = result.get("parameters") or {}
            raise TelegramError(
                result.get("description", "Unknown error"),
                result.get("error_code", 0),
                retryAfter=p.get("retry_after"),
            )
        return wrap(result.get("result"))

    def _safeParse(self, response) -> TelegramError:
        try:
            self._parse(response.content)
            return TelegramError("unknown error", response.status_code)
        except TelegramError as e:
            return e if e.error_code else TelegramError(e.description, response.status_code)

    async def _with_retry(self, op):
        last_exc: Exception = RuntimeError("retry: no attempts made")
        for _ in range(_MAX_RETRIES):
            try:
                return await op()
            except TelegramError as e:
                last_exc = e
                if e.error_code == 429 and e.retry_after:
                    await asyncio.sleep(e.retry_after)
                    continue
                raise
            except httpx.HTTPStatusError as e:
                last_exc = self._safeParse(e.response)
                if last_exc.error_code == 429 and last_exc.retry_after:
                    await asyncio.sleep(last_exc.retry_after)
                    continue
                raise last_exc from e
        raise last_exc

    async def call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for k, v in list(params.items()):
            if isinstance(v, (dict, list)):
                params[k] = json.dumps(v)
        async def _do():
            resp = await self._client.post(self._url(method), data=params)
            resp.raise_for_status()
            return self._parse(resp.content)
        return await self._with_retry(_do)

    async def callFile(self, method: str, fileKey: str, fileObj, filename: str, contentType: str, **params):
        fileData = fileObj.read() if hasattr(fileObj, "read") else fileObj
        params = {k: v for k, v in params.items() if v is not None}
        data = {}
        for k, v in params.items():
            data[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        async def _do():
            resp = await self._client.post(self._url(method), data=data, files={fileKey: (filename, fileData, contentType)})
            resp.raise_for_status()
            return self._parse(resp.content)
        return await self._with_retry(_do)

    async def callMediaGroup(self, chatId: int, items: list, **params):
        hasBytes = any(isinstance(item.get("_bytes"), (bytes, bytearray)) for item in items)
        if not hasBytes:
            mediaList = [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]
            return await self.call("sendMediaGroup", chat_id=chatId, media=mediaList, **params)
        fields = {"chat_id": str(chatId)}
        mediaJson = []
        for i, item in enumerate(items):
            rawBytes = item.get("_bytes")
            entry = {k: v for k, v in item.items() if not k.startswith("_")}
            if rawBytes is not None:
                attachName = f"file{i}"
                filename = item.get("_filename", f"file{i}.jpg")
                ct = item.get("_content_type") or mimeType(filename.rsplit(".", 1)[-1] if "." in filename else "jpg")
                fields[attachName] = (filename, bytes(rawBytes) if not isinstance(rawBytes, bytes) else rawBytes, ct)
                entry["media"] = f"attach://{attachName}"
            mediaJson.append(entry)
        fields["media"] = json.dumps(mediaJson)
        for k, v in params.items():
            if v is not None:
                fields[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        async def _do():
            resp = await self._client.post(self._url("sendMediaGroup"), files=fields)
            resp.raise_for_status()
            return self._parse(resp.content)
        return await self._with_retry(_do)

    async def callWithAttachments(self, method: str, jsonField: str, jsonValue, attachments: dict, **params):
        if not attachments:
            return await self.call(method, **{jsonField: jsonValue}, **params)
        fields = {jsonField: json.dumps(jsonValue)}
        for k, v in params.items():
            if v is not None:
                fields[k] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        async def _do():
            resp = await self._client.post(self._url(method), data=fields, files=dict(attachments))
            resp.raise_for_status()
            return self._parse(resp.content)
        return await self._with_retry(_do)

    async def close(self):
        try:
            await self._client.aclose()
        except Exception:
            pass


class TimerHandle:

    def __init__(self):
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


def matchText(
    text, commands=None, exact=None, starts=None, ends=None, contains=None, regex=None,
    withSlash: bool = True, withoutSlash: bool = True,
):
    if not text:
        return None, None
    t = text.strip()
    tl = t.lower()
    parts = t.split()
    fw = parts[0] if parts else ""

    if commands:
        cmds = [c.lstrip("/").lower() for c in toList(commands)]
        clean = fw.lstrip("/").split("@")[0].lower()
        hasSlash = fw.startswith("/")
        if clean in cmds and ((hasSlash and withSlash) or (not hasSlash and withoutSlash)):
            return parts[1:], None

    if exact is not None:
        for e in toList(exact):
            if tl == e.lower():
                return [], None

    if starts is not None:
        for s in toList(starts):
            if tl.startswith(s.lower()):
                return t[len(s):].strip().split(), None

    if ends is not None:
        for e in toList(ends):
            if tl.endswith(e.lower()):
                return t[:-len(e)].strip().split(), None

    if contains is not None:
        if any(c.lower() in tl for c in toList(contains)):
            return parts[1:], None

    if regex is not None:
        m = re.search(regex, t, re.IGNORECASE)
        if m:
            return list(m.groups()), m

    return None, None


class TextRoute:
    __slots__ = ("fn", "args_min", "args_error", "guard", "exact", "starts", "ends", "contains", "regex")

    def __init__(self, fn, argsMin: int = None, argsError: str = None, guard=None, exact=None, starts=None, ends=None, contains=None, regex=None):
        self.fn = fn
        self.args_min = argsMin
        self.args_error = argsError
        self.guard = guard
        self.exact = exact
        self.starts = starts
        self.ends = ends
        self.contains = contains
        self.regex = regex

    def match(self, text: str):
        tl = text.lower()
        if self.exact is not None:
            el = self.exact.lower()
            if tl == el:
                return [], None
            if tl.startswith(el + " "):
                return text[len(self.exact):].strip().split(), None
            return None, None
        if self.starts is not None:
            if tl.startswith(self.starts.lower()):
                return text[len(self.starts):].strip().split(), None
            return None, None
        if self.ends is not None:
            el = self.ends.lower()
            if tl.endswith(el):
                return text[:-len(self.ends)].strip().split(), None
            return None, None
        if self.contains is not None:
            if self.contains.lower() in tl:
                return text.split(), None
            return None, None
        if self.regex is not None:
            m = re.search(self.regex, text, re.IGNORECASE)
            if m:
                return list(m.groups()), m
            return None, None
        return None, None


class _RouteResult:
    __slots__ = ("route", "args", "match")

    def __init__(self, route, args, match):
        self.route = route
        self.args = args
        self.match = match


class CommandBlock:
    __slots__ = (
        "_gramly", "_triggersSingle", "_triggersMulti",
        "_deny", "_withSlash", "_withoutSlash",
        "_blockGuard", "_blockArgsMin", "_blockArgsError",
        "_defaultFn", "_textRoutes", "_callbackRoutes", "_registered",
    )

    def __init__(self, gramly, triggers: list, deny: str = None, withSlash: bool = True, withoutSlash: bool = True, guard=None, argsMin: int = None, argsError: str = None):
        self._gramly = gramly
        self._triggersSingle = set()
        self._triggersMulti = set()
        for t in triggers:
            tl = t.lstrip("/").lower()
            if " " in tl:
                self._triggersMulti.add(tl)
            else:
                self._triggersSingle.add(tl)
        self._deny = deny
        self._withSlash = withSlash
        self._withoutSlash = withoutSlash
        self._blockGuard = guard
        self._blockArgsMin = argsMin
        self._blockArgsError = argsError
        self._defaultFn = None
        self._textRoutes = []
        self._callbackRoutes = {}
        self._registered = False

    def _extractCommand(self, raw: dict):
        text = raw.get("text") or raw.get("caption") or ""
        t = text.strip()
        if not t:
            return None, None, None, None, None
        parts = t.split()
        fw = parts[0]
        hasSlash = fw.startswith("/")
        tl = t.lower()
        return t, parts, fw, hasSlash, tl

    def default(self, fn) -> Callable:
        self._defaultFn = fn
        return fn

    def on(self, *keys, exact=None, starts=None, ends=None, contains=None, regex=None, args: int = None, error: str = None, guard=None) -> Callable:
        def decorator(fn):
            if keys:
                for k in keys:
                    self._textRoutes.append((k.lower(), TextRoute(fn, args, error, guard, exact=k)))
            else:
                self._textRoutes.append((None, TextRoute(fn, args, error, guard, exact=exact, starts=starts, ends=ends, contains=contains, regex=regex)))
            return fn
        return decorator

    def onCallback(self, *actions, owner: bool = True, guard=None, args: int = None, error: str = None) -> Callable:
        def decorator(fn):
            for a in actions:
                self._callbackRoutes[a.lower()] = (fn, owner, guard, args, error)
            return fn
        return decorator

    def _register(self):
        if self._registered:
            return
        self._registered = True
        self._gramly._registerCommandBlock(self)

    def _matchTrigger(self, raw: dict) -> bool:
        t, parts, fw, hasSlash, tl = self._extractCommand(raw)
        if t is None:
            return False
        if hasSlash and not self._withSlash:
            return False
        if not hasSlash and not self._withoutSlash:
            return False
        if fw.lstrip("/").split("@")[0].lower() in self._triggersSingle:
            return True
        if self._triggersMulti:
            clean_tl = tl.lstrip("/")
            for mt in self._triggersMulti:
                if clean_tl == mt or clean_tl.startswith(mt + " "):
                    return True
        return False

    def _findRoute(self, subtext: str):
        for _, route in self._textRoutes:
            args, match = route.match(subtext)
            if args is not None:
                return _RouteResult(route, args, match)
        return None

    def dispatchMessage(self, raw: dict):
        g = self._gramly
        uid = (raw.get("from") or {}).get("id")
        if uid and not g._checkCooldown(uid, "msg"):
            return
        if not g._runGuards(raw):
            return
        if self._blockGuard and not self._blockGuard(Message(raw, [])):
            return

        t, parts, fw, hasSlash, tl = self._extractCommand(raw)
        if t is None:
            return

        matchedTrigger = None
        clean_tl = tl.lstrip("/")
        for mt in self._triggersMulti:
            if clean_tl == mt or clean_tl.startswith(mt + " "):
                matchedTrigger = mt
                break

        if matchedTrigger is not None:
            remainder = t[len(matchedTrigger):].strip()
            cmdArgs = remainder.split() if remainder else []
        else:
            cmdArgs = parts[1:]

        if self._blockArgsMin is not None and len(cmdArgs) < self._blockArgsMin:
            if self._blockArgsError:
                g.send(raw["chat"]["id"], self._blockArgsError)
            return

        g._runInterceptors(raw)
        g._markHandled(raw.get("message_id"))

        if not cmdArgs:
            if self._defaultFn:
                g._submit(self._defaultFn, Message(raw, []))
            return

        subtext = " ".join(cmdArgs)
        result = self._findRoute(subtext)
        if result is None:
            if self._defaultFn:
                g._submit(self._defaultFn, Message(raw, cmdArgs))
            return

        msg = Message(raw, result.args, match=result.match)
        if result.route.args_min is not None and len(result.args) < result.route.args_min:
            if result.route.args_error:
                g.send(raw["chat"]["id"], result.route.args_error)
            return
        if result.route.guard and not result.route.guard(msg):
            return
        g._submit(result.route.fn, msg)

    def dispatchCallback(self, raw: dict):
        g = self._gramly
        uid = (raw.get("from") or {}).get("id", 0)
        data = raw.get("data") or ""
        parts = data.split(":")
        if len(parts) < 2:
            return
        action = parts[1].lower()
        route = self._callbackRoutes.get(action)
        if not route:
            return

        def _run():
            cb = CallbackData(data)
            fn, checkOwner, routeGuard, argsRequired, errorMsg = route
            if checkOwner and cb.owner is not None and uid != cb.owner:
                g.alert(raw, self._deny, popup=True) if self._deny else g.ack(raw)
                return
            extra = cb.parts[2:]
            parsed = CallbackQuery(raw, cb, args=extra)
            if argsRequired is not None and len(extra) < argsRequired:
                g.alert(parsed, errorMsg, popup=True) if errorMsg else g.ack(parsed)
                return
            if routeGuard and not routeGuard(parsed):
                g.ack(parsed)
                return
            future = asyncio.run_coroutine_threadsafe(g._runCallback(parsed, fn), g._loop)
            future.result()

        g._ensure_loop()
        if g._loop.is_running():
            asyncio.ensure_future(g._submitForUser(uid, _run, callId=raw.get("id")))
        else:
            g._run_coro(g._submitForUser(uid, _run, callId=raw.get("id")))


class Gramly:
    def __init__(self, token: str, msgCooldown: float = 0.35, inlineCooldown: float = 0.5, connectTimeout: int = 10, readTimeout: int = 30, locksMaxsize: int = 10000, parseMode: str = "HTML", debug: bool = False):
        if not token or not token.strip():
            raise ValueError("Token must be a non-empty string")
        self.debug = debug
        self.parse_mode = parseMode
        setupLogging(debug)

        self._loop = None
        self._loop_owned = False
        self._api = AsyncAPIClient(token, connectTimeout=connectTimeout, readTimeout=readTimeout)
        self._msgCooldown = msgCooldown
        self._inlineCooldown = inlineCooldown

        self._guards = []
        self._interceptors = []
        self._stopEvent = threading.Event()
        self._pendingTimers: list = []

        self._userMsgTs = LRUCache(locksMaxsize)
        self._userMsgTsLock = threading.Lock()
        self._userInlineTs = LRUCache(locksMaxsize)
        self._userInlineTsLock = threading.Lock()
        self._handledMsgIds = LRUCache(locksMaxsize)
        self._handledMsgIdsLock = threading.Lock()
        self._userCbLocks = {}
        self._userCbLocksLock = asyncio.Lock()

        handlerAttrs = (
            "_messageHandlers", "_callbackHandlers", "_inlineHandlers", "_editedHandlers",
            "_postHandlers", "_mediaHandlers", "_anyHandlers", "_myStatusHandlers",
            "_memberHandlers", "_joinHandlers", "_reactionHandlers", "_pollHandlers",
            "_boostHandlers", "_checkoutHandlers", "_paymentHandlers", "_webappHandlers",
            "_paidMediaHandlers", "_guestHandlers", "_managedHandlers", "_generationStoppedHandlers",
            "_commandBlocks", "_stopCallbacks",
            "_bizMsgHandlers", "_bizEditedHandlers", "_bizDeletedHandlers", "_bizConnectionHandlers",
        )
        for attr in handlerAttrs:
            setattr(self, attr, [])

        self._bizConnCache = LRUCache(1000)
        self._bizConnCacheLock = threading.Lock()

    def _ensure_loop(self):
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                self._loop_owned = True

    def _run_coro(self, coro):
        self._ensure_loop()
        if self._loop.is_running():
            if _in_async_task():
                warnings.warn(
                    "Calling a sync method from an async handler without await — use 'await bot.func()'",
                    stacklevel=3)
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result()
        return self._loop.run_until_complete(coro)

    def _api_call(self, method, **params):
        coro = self._api.call(method, **params)
        if _in_async_task():
            return coro
        return self._run_coro(coro)

    async def _safeCoro(self, method, **params):
        try:
            return await self._api.call(method, **params)
        except Exception as e:
            _log.warning(method, err=e)
            return None

    def _safe(self, method, **params):
        coro = self._safeCoro(method, **params)
        if _in_async_task():
            return coro
        return self._run_coro(coro)

    def _editSafe(self, action: str, method: str, kind: str = None, **params):
        try:
            return self._api_call(method, **params)
        except TelegramError as e:
            if not isNotModified(e):
                _log.warning(action, kind=kind, err=e)

    def _api_callFile(self, method, fileKey, fileObj, filename, contentType, **params):
        coro = self._api.callFile(method, fileKey=fileKey, fileObj=fileObj, filename=filename, contentType=contentType, **params)
        if _in_async_task():
            return coro
        return self._run_coro(coro)

    def _api_callMediaGroup(self, chatId, items, **params):
        coro = self._api.callMediaGroup(chatId, items, **params)
        if _in_async_task():
            return coro
        return self._run_coro(coro)

    def _api_callRich(self, method, jsonField, jsonValue, attachments, **params):
        coro = self._api.callWithAttachments(method, jsonField, jsonValue, attachments, **params)
        if _in_async_task():
            return coro
        return self._run_coro(coro)

    def _debug(self, action: str, **ctx):
        if self.debug:
            _log.debug(action, **ctx)

    def _markHandled(self, msgId):
        if msgId is not None:
            with self._handledMsgIdsLock:
                self._handledMsgIds[msgId] = True

    def _popHandled(self, msgId) -> bool:
        if msgId is None:
            return False
        with self._handledMsgIdsLock:
            if msgId in self._handledMsgIds:
                del self._handledMsgIds[msgId]
                return True
        return False

    def _checkCooldown(self, uid: int, kind: str) -> bool:
        if kind == "msg":
            store, interval, lock = self._userMsgTs, self._msgCooldown, self._userMsgTsLock
        else:
            store, interval, lock = self._userInlineTs, self._inlineCooldown, self._userInlineTsLock
        now = time.time()
        with lock:
            last = store.get(uid, 0.0)
            if now - last < interval:
                return False
            store[uid] = now
        return True

    def _submit(self, fn, *args):
        coro = self._wrap(fn, *args)
        self._ensure_loop()
        if self._loop.is_running():
            try:
                asyncio.get_running_loop()
                asyncio.ensure_future(coro)
            except RuntimeError:
                asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:
            self._loop.run_until_complete(coro)

    async def _submitForUser(self, uid: int, fn, *args, callId: str = None):
        async with self._userCbLocksLock:
            if uid not in self._userCbLocks:
                self._userCbLocks[uid] = asyncio.Lock()
        lock = self._userCbLocks[uid]
        if not lock.locked():
            async with lock:
                await self._wrap(fn, *args)
            async with self._userCbLocksLock:
                if not lock.locked():
                    self._userCbLocks.pop(uid, None)
        else:
            self._debug("drop callback", uid=uid, reason="busy")
            if callId:
                try:
                    await self._api.call("answerCallbackQuery", callback_query_id=callId, cache_time=1)
                except Exception:
                    pass

    async def _wrap(self, fn, *args):
        try:
            if asyncio.iscoroutinefunction(fn):
                await fn(*args)
            else:
                await asyncio.to_thread(fn, *args)
        except Exception:
            _log.error("handler", fn=getattr(fn, "__name__", repr(fn)), exc_info=True)

    async def _runCallback(self, parsed, fn):
        try:
            if asyncio.iscoroutinefunction(fn):
                await fn(parsed)
            else:
                await asyncio.to_thread(fn, parsed)
        except Exception:
            _log.error("callback_handler", fn=fn.__name__, exc_info=True)
        finally:
            if not parsed._answered:
                _ack = self.alert(parsed, text="", popup=False)
                if asyncio.iscoroutine(_ack):
                    await _ack

    def _resolveParseMode(self, **kwargs) -> str:
        return kwargs.pop("parse_mode", None) or self.parse_mode

    def _resolveMarkup(self, inline=None, keyboard=None, forceReply: bool = None):
        if inline is not None:
            return buildInlineKeyboard(inline, forceReply=forceReply)
        if keyboard is not None:
            return buildReplyKeyboard(keyboard, forceReply=forceReply)
        if forceReply:
            return {"force_reply": True}
        return None

    def _ephemeralParams(self, receiver, callId, replaceCallbackMessage: bool = None) -> Optional[dict]:
        if receiver is None and callId is None and not replaceCallbackMessage:
            return None
        params = {}
        if receiver is not None:
            params["receiver_user_id"] = receiver
        if callId is not None:
            params["callback_query_id"] = callId
        if replaceCallbackMessage is not None:
            params["replace_callback_query_message"] = replaceCallbackMessage
        return params

    def _ephemeralFrom(self, to) -> tuple:
        if isinstance(to, CallbackQuery):
            return to.user_id, to.id, None
        if isinstance(to, Message):
            return to.user_id, None, to.ephemeralId
        if isinstance(to, int):
            return to, None, None
        if isinstance(to, dict):
            return userId(to), to.get("id"), to.get("ephemeral_message_id")
        return userId(to), getattr(to, "id", None), getattr(to, "ephemeral_message_id", None)

    def _msgTarget(self, call):
        if isinstance(call, CallbackQuery):
            hasPhoto = bool(call.message.get("photo") if isinstance(call.message, dict) else getattr(call.message, "photo", None))
            return call.chat_id, call.message_id, hasPhoto, call.bc_id, call.ephemeralId
        if isinstance(call, BusinessMessage):
            return call.chat_id, call.message_id, bool(call._raw.get("photo")), call.bc_id, None
        if isinstance(call, Message):
            return call.chat_id, call.message_id, bool(call._raw.get("photo")), None, call.ephemeralId
        if isinstance(call, dict):
            msg = call.get("message", call)
            return msg.get("chat", {}).get("id"), msg.get("message_id"), bool(msg.get("photo")), msg.get("business_connection_id"), msg.get("ephemeral_message_id")
        cid = getattr(call, "chat_id", None)
        if cid is None:
            chatObj = getattr(call, "chat", None)
            if chatObj is not None:
                cid = chatObj.get("id") if isinstance(chatObj, dict) else getattr(chatObj, "id", None)
        bcId = getattr(call, "bc_id", None) or getattr(call, "business_connection_id", None)
        return cid, getattr(call, "message_id", None), bool(getattr(call, "photo", None)), bcId, getattr(call, "ephemeral_message_id", None)

    def _msgFrom(self, message) -> tuple:
        if isinstance(message, Message):
            return message.chat_id, message.message_id
        if isinstance(message, dict):
            return message.get("chat", {}).get("id"), message.get("message_id")
        return message.chat.id, message.message_id

    def _runGuards(self, raw: dict) -> bool:
        for fn in self._guards:
            try:
                if not fn(raw):
                    return False
            except Exception:
                _log.error("guard", fn=fn.__name__, exc_info=True)
                return False
        return True

    def _runInterceptors(self, raw: dict):
        for fn in self._interceptors:
            try:
                fn(raw)
            except Exception:
                _log.error("interceptor", fn=fn.__name__, exc_info=True)

    def _registerCommandBlock(self, block: CommandBlock):
        self._commandBlocks.append(block)

    def guard(self, fn):
        self._guards.append(fn)
        return self

    def intercept(self, fn):
        self._interceptors.append(fn)
        return self

    def onStop(self, fn):
        self._stopCallbacks.append(fn)
        return self

    def command(self, *triggers, deny: str = None, withSlash: bool = True, withoutSlash: bool = True, guard=None, argsMin: int = None, argsError: str = None):
        def decorator(fn):
            block = CommandBlock(self, list(triggers), deny=deny, withSlash=withSlash, withoutSlash=withoutSlash, guard=guard, argsMin=argsMin, argsError=argsError)
            fn(block)
            block._register()
            return fn
        return decorator

    def onMessage(self, commands=None, exact=None, starts=None, ends=None, contains=None, regex=None, withSlash: bool = True, withoutSlash: bool = True, argsMin: int = None, argsError: str = None, guard=None, business: bool = False):
        if business:
            def decorator(fn):
                def _handle(msg: BusinessMessage):
                    if any(f is not None for f in (commands, exact, starts, ends, contains, regex)):
                        args, match = matchText(
                            msg.text, commands=commands, exact=exact, starts=starts, ends=ends,
                            contains=contains, regex=regex, withSlash=withSlash, withoutSlash=withoutSlash,
                        )
                        if args is None:
                            return
                        msg.args = args
                        msg.match = match
                    if guard and not guard(msg):
                        return
                    self._submit(fn, msg)
                self._bizMsgHandlers.append(_handle)
                return fn
            return decorator

        def decorator(fn):
            def _handle(raw: dict):
                text = raw.get("text") or raw.get("caption") or ""
                args, match = matchText(text, commands=commands, exact=exact, starts=starts, ends=ends, contains=contains, regex=regex, withSlash=withSlash, withoutSlash=withoutSlash)
                if args is None:
                    return
                uid = (raw.get("from") or {}).get("id")
                if uid and not self._checkCooldown(uid, "msg"):
                    return
                if not self._runGuards(raw):
                    return
                parsed = Message(raw, args, match=match)
                if guard and not guard(parsed):
                    return
                if argsMin is not None and len(args) < argsMin:
                    if argsError:
                        self.send(raw["chat"]["id"], argsError)
                    return
                self._runInterceptors(raw)
                self._markHandled(raw.get("message_id"))
                self._submit(fn, parsed)
            self._messageHandlers.append(_handle)
            return fn
        return decorator

    def onEdited(self, business: bool = False):
        def decorator(fn):
            if business:
                self._bizEditedHandlers.append(fn)
                return fn

            def _handle(raw: dict):
                uid = (raw.get("from") or {}).get("id")
                if uid and not self._checkCooldown(uid, "msg"):
                    return
                if not self._runGuards(raw):
                    return
                text = raw.get("text") or ""
                self._submit(fn, Message(raw, text.split() if text else []))
            self._editedHandlers.append(_handle)
            return fn
        return decorator

    def onPost(self):
        def decorator(fn):
            def _handle(raw: dict):
                if not self._runGuards(raw):
                    return
                self._submit(fn, Message(raw, []))
            self._postHandlers.append(_handle)
            return fn
        return decorator

    def onMedia(self, *contentTypes):
        def decorator(fn):
            ctypes = set(contentTypes)
            def _handle(raw: dict):
                for ct in ctypes:
                    if ct in raw:
                        uid = (raw.get("from") or {}).get("id")
                        if uid and not self._checkCooldown(uid, "msg"):
                            return
                        if not self._runGuards(raw):
                            return
                        self._runInterceptors(raw)
                        self._submit(fn, Message(raw, []))
                        return
            self._mediaHandlers.append(_handle)
            return fn
        return decorator

    def onAny(self):
        def decorator(fn):
            def _handle(raw: dict):
                if self._popHandled(raw.get("message_id")):
                    return
                if not self._runGuards(raw):
                    return
                self._submit(fn, Message(raw, []))
            self._anyHandlers.append(_handle)
            return fn
        return decorator

    def onCallback(self, *prefixes, owner: bool = True, ownerPos: int = None, deny=None, guard=None):
        def decorator(fn):
            def _handle(raw: dict):
                data = raw.get("data") or ""
                if prefixes and not any(data.startswith(p) for p in prefixes):
                    return
                uid = (raw.get("from") or {}).get("id", 0)

                def _run():
                    cb = CallbackData(data)
                    parsed = CallbackQuery(raw, cb)
                    checkPos = ownerPos if ownerPos is not None else (0 if owner else None)
                    if checkPos is not None:
                        oid = cb.get(checkPos, int)
                        if oid is not None and uid != oid:
                            deny(parsed) if deny else self.ack(parsed)
                            return
                    if guard and not guard(parsed):
                        self.ack(parsed)
                        return
                    future = asyncio.run_coroutine_threadsafe(self._runCallback(parsed, fn), self._loop)
                    future.result()

                self._ensure_loop()
                if self._loop.is_running():
                    asyncio.ensure_future(self._submitForUser(uid, _run, callId=raw.get("id")))
                else:
                    self._run_coro(self._submitForUser(uid, _run, callId=raw.get("id")))
            self._callbackHandlers.append(_handle)
            return fn
        return decorator

    def onInline(self, minLength: int = 0):
        def decorator(fn):
            def _handle(raw: dict):
                q = raw.get("query", "")
                if len(q.strip()) < minLength:
                    return
                uid = (raw.get("from") or {}).get("id", 0)
                if not self._checkCooldown(uid, "inline"):
                    return
                self._submit(fn, InlineQuery(raw, self))
            self._inlineHandlers.append(_handle)
            return fn
        return decorator

    def onCheckout(self, fn):
        self._checkoutHandlers.append(fn)
        return fn

    def onPayment(self, fn):
        self._paymentHandlers.append(fn)
        return fn

    def onPaidMedia(self, fn):
        self._paidMediaHandlers.append(fn)
        return fn

    def onJoinRequest(self, fn):
        self._joinHandlers.append(fn)
        return fn

    def onMyStatus(self, fn):
        self._myStatusHandlers.append(fn)
        return fn

    def onChatMember(self, fn):
        self._memberHandlers.append(fn)
        return fn

    def onBoost(self, fn):
        self._boostHandlers.append(fn)
        return fn

    def onReaction(self, fn):
        self._reactionHandlers.append(fn)
        return fn

    def onPoll(self, fn):
        self._pollHandlers.append(fn)
        return fn

    def onWebApp(self, fn):
        self._webappHandlers.append(fn)
        return fn

    def onGuest(self, fn):
        self._guestHandlers.append(fn)
        return fn

    def onManaged(self, fn):
        self._managedHandlers.append(fn)
        return fn

    def onGenerationStopped(self, fn):
        self._generationStoppedHandlers.append(fn)
        return fn

    def onBusinessConnection(self, fn):
        self._bizConnectionHandlers.append(fn)
        return fn

    def onBusinessDeleted(self, fn):
        self._bizDeletedHandlers.append(fn)
        return fn

    def onBusinessEdited(self):
        return self.onEdited(business=True)

    def onBusinessMessage(self, **kw):
        return self.onMessage(business=True, **kw)

    def businessConnection(self, bcId: str):
        coro = self._businessConnection(bcId)
        if _in_async_task():
            return coro
        return self._run_coro(coro)

    async def _businessConnection(self, bcId: str):
        with self._bizConnCacheLock:
            cached = self._bizConnCache.get(bcId)
        if cached:
            return cached
        try:
            raw = await self._api.call("getBusinessConnection", business_connection_id=bcId)
            conn = BusinessConnection(dict(raw) if isinstance(raw, Obj) else raw)
            with self._bizConnCacheLock:
                self._bizConnCache[bcId] = conn
            return conn
        except Exception as e:
            _log.warning("businessConnection", bc=bcId, err=e)
            return None

    def businessSend(self, target, text: str, bcId: str, inline=None, keyboard=None, **kwargs):
        markup = self._resolveMarkup(inline, keyboard)
        return self._api_call("sendMessage", chat_id=chatId(target), text=text, parse_mode=self.parse_mode, business_connection_id=bcId, reply_markup=markup, **kwargs)

    def businessAction(self, target, bcId: str, action: str = "typing"):
        return self._safe("sendChatAction", chat_id=chatId(target), action=action, business_connection_id=bcId)

    def businessRead(self, bcId: str, chatIdVal: int, maxMessageId: int):
        return self._safe("readBusinessMessage", business_connection_id=bcId, chat_id=chatIdVal, message_id=maxMessageId)

    def businessDelete(self, bcId: str, chatIdVal: int, messageIds: list):
        return self._safe("deleteBusinessMessages", business_connection_id=bcId, chat_id=chatIdVal, message_ids=messageIds)

    def timer(self, seconds: float, fn, fireNow: bool = False) -> TimerHandle:
        handle = TimerHandle()

        def _loop():
            if fireNow:
                try:
                    self._submit(fn)
                except RuntimeError:
                    return
            while not self._stopEvent.is_set() and not handle.stopped:
                handle._stop.wait(seconds)
                if self._stopEvent.is_set() or handle.stopped:
                    break
                try:
                    self._submit(fn)
                except RuntimeError:
                    break

        threading.Thread(target=_loop, daemon=True).start()
        return handle

    def send(self, target, text, inline=None, keyboard=None, photo=None, forceReply: bool = None, **kwargs):
        cid = chatId(target)
        markup = self._resolveMarkup(inline, keyboard, forceReply=forceReply)
        if isinstance(text, dict):
            built = text
            cleaned, attachments = _collectRichAttachments(built)
            self._debug("send", chat=cid, rich=True, blocks=len(built.get("blocks", [])) or None)
            return self._api_callRich("sendRichMessage", "rich_message", cleaned, attachments,
                chat_id=cid, reply_markup=markup, **kwargs)
        self._debug("send", chat=cid, text=f"{text[:40]!r}")
        if photo is not None:
            return self._api_call("sendPhoto", chat_id=cid, photo=photo, caption=text, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)
        return self._api_call("sendMessage", chat_id=cid, text=text, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def reply(self, message, text, keyboard=None, inline=None, photo=None, forceReply: bool = None, **kwargs):
        markup = self._resolveMarkup(inline, keyboard, forceReply=forceReply)
        chatIdVal, msgId = self._msgFrom(message)
        if isinstance(text, dict):
            built = text
            cleaned, attachments = _collectRichAttachments(built)
            return self._api_callRich("sendRichMessage", "rich_message", cleaned, attachments,
                chat_id=chatIdVal, reply_markup=markup, reply_to_message_id=msgId, **kwargs)
        if photo is not None:
            return self._api_call("sendPhoto", chat_id=chatIdVal, photo=photo, caption=text, parse_mode=self.parse_mode, reply_markup=markup, reply_to_message_id=msgId, **kwargs)
        return self._api_call("sendMessage", chat_id=chatIdVal, text=text, parse_mode=self.parse_mode, reply_markup=markup, reply_to_message_id=msgId, **kwargs)

    def ephemeral(self, target, text, to=None, keyboard=None, inline=None, photo=None,
                  replaceCallbackMessage: bool = None, **kwargs):
        cid = chatId(target)
        markup = self._resolveMarkup(inline, keyboard)
        receiver, callId, replyEphId = self._ephemeralFrom(to if to is not None else target)
        replyParams = {"ephemeral_message_id": replyEphId} if replyEphId is not None else None
        ephParams = self._ephemeralParams(receiver, callId, replaceCallbackMessage)
        self._debug("ephemeral", chat=cid, to=receiver)
        if isinstance(text, dict):
            built = text
            cleaned, attachments = _collectRichAttachments(built)
            self._debug("ephemeral", chat=cid, to=receiver, rich=True)
            return self._api_callRich("sendRichMessage", "rich_message", cleaned, attachments,
                chat_id=cid, reply_markup=markup, ephemeral_message_parameters=ephParams,
                reply_parameters=replyParams, **kwargs)
        if photo is not None:
            return self._api_call("sendPhoto", chat_id=cid, photo=photo, caption=text, parse_mode=self.parse_mode,
                reply_markup=markup, ephemeral_message_parameters=ephParams, reply_parameters=replyParams, **kwargs)
        return self._api_call("sendMessage", chat_id=cid, text=text, parse_mode=self.parse_mode,
            reply_markup=markup, ephemeral_message_parameters=ephParams, reply_parameters=replyParams, **kwargs)

    def edit(self, call, text, inline=None, photo=None, showCaption: bool = None, **kwargs):
        chatIdVal, msgId, hasPhoto, bcId, ephId = self._msgTarget(call)
        markup = buildInlineKeyboard(inline) if inline is not None else None
        if ephId is not None:
            self._debug("edit", chat=chatIdVal, ephemeral=ephId)
            if photo is not None:
                media = _resolveInputMedia(photo, "photo", caption=text, parse_mode=self.parse_mode,
                                            show_caption_above_media=showCaption)
                cleanedMedia, attachments = _collectRichAttachments(media)
                return self._api_callRich("editEphemeralMessageMedia", "media", cleanedMedia, attachments,
                    chat_id=chatIdVal, ephemeral_message_id=ephId, reply_markup=markup, **kwargs)
            if hasPhoto:
                return self._editSafe("edit", "editEphemeralMessageCaption", kind="ephemeral_caption", caption=text, chat_id=chatIdVal, ephemeral_message_id=ephId, parse_mode=self.parse_mode, show_caption_above_media=showCaption, reply_markup=markup, **kwargs)
            if isinstance(text, dict):
                cleaned, attachments = _collectRichAttachments(text)
                if attachments:
                    raise ValueError("edit: rich message references local files; use a file_id or URL when editing an ephemeral message")
                return self._editSafe("edit", "editEphemeralMessageText", kind="ephemeral_rich", rich_message=cleaned, chat_id=chatIdVal, ephemeral_message_id=ephId, reply_markup=markup, **kwargs)
            return self._editSafe("edit", "editEphemeralMessageText", kind="ephemeral_text", text=text, chat_id=chatIdVal, ephemeral_message_id=ephId, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)
        if isinstance(text, dict):
            built = text
            cleaned, attachments = _collectRichAttachments(built)
            if attachments:
                raise ValueError("edit: rich message references local files; use a file_id or URL when editing")
            self._debug("edit", chat=chatIdVal, msg=msgId, bc=bcId, rich=True)
            return self._editSafe("edit", "editMessageText", kind="rich",
                rich_message=cleaned, chat_id=chatIdVal, message_id=msgId,
                business_connection_id=bcId, reply_markup=markup, **kwargs)
        self._debug("edit", chat=chatIdVal, msg=msgId, bc=bcId)
        if photo is not None:
            media = {"type": "photo", "media": photo, "caption": text, "parse_mode": self.parse_mode}
            return self._editSafe("edit", "editMessageMedia", kind="media", media=media, chat_id=chatIdVal, message_id=msgId, business_connection_id=bcId, reply_markup=markup, **kwargs)
        if hasPhoto:
            return self._editSafe("edit", "editMessageCaption", kind="caption", caption=text, chat_id=chatIdVal, message_id=msgId, business_connection_id=bcId, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)
        return self._editSafe("edit", "editMessageText", kind="text", text=text, chat_id=chatIdVal, message_id=msgId, business_connection_id=bcId, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def editMarkup(self, call, inline=None):
        chatIdVal, msgId, _, bcId, ephId = self._msgTarget(call)
        markup = buildInlineKeyboard(inline) if inline is not None else None
        if ephId is not None:
            return self._editSafe("editMarkup", "editEphemeralMessageReplyMarkup", kind="ephemeral", chat_id=chatIdVal, ephemeral_message_id=ephId, reply_markup=markup)
        return self._editSafe("editMarkup", "editMessageReplyMarkup", chat_id=chatIdVal, message_id=msgId, business_connection_id=bcId, reply_markup=markup)

    def replace(self, call, text: str, inline=None, photo=None, **kwargs):
        chatIdVal, msgId, hasPhoto, bcId, _ = self._msgTarget(call)
        markup = buildInlineKeyboard(inline) if inline is not None else None
        if photo is not None:
            media = {"type": "photo", "media": photo, "caption": text, "parse_mode": self.parse_mode}
            return self._editSafe("replace", "editMessageMedia", kind="swap_photo", media=media, chat_id=chatIdVal, message_id=msgId, business_connection_id=bcId, reply_markup=markup)
        if hasPhoto:
            try:
                self._api_call("deleteMessage", chat_id=chatIdVal, message_id=msgId)
            except Exception:
                pass
            return self._api_call("sendMessage", chat_id=chatIdVal, text=text, business_connection_id=bcId, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)
        return self._editSafe("replace", "editMessageText", text=text, chat_id=chatIdVal, message_id=msgId, business_connection_id=bcId, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def editLiveLocation(self, call, latitude: float, longitude: float, inline=None, **kwargs):
        chatIdVal, msgId, _, bcId, _ = self._msgTarget(call)
        markup = buildInlineKeyboard(inline) if inline is not None else None
        return self._api_call("editMessageLiveLocation", chat_id=chatIdVal, message_id=msgId, business_connection_id=bcId, latitude=latitude, longitude=longitude, reply_markup=markup, **kwargs)

    def stopLiveLocation(self, call, inline=None, **kwargs):
        chatIdVal, msgId, _, bcId, _ = self._msgTarget(call)
        markup = buildInlineKeyboard(inline) if inline is not None else None
        return self._api_call("stopMessageLiveLocation", chat_id=chatIdVal, message_id=msgId, business_connection_id=bcId, reply_markup=markup, **kwargs)

    def alert(self, call, text: str = "", popup: bool = False):
        if isinstance(call, CallbackQuery):
            if call._answered:
                return
            call._answered = True
            cid = call.id
        elif isinstance(call, dict):
            cid = call.get("id")
        else:
            cid = call.id if hasattr(call, "id") else call
        try:
            return self._api_call("answerCallbackQuery", callback_query_id=cid, text=text, show_alert=popup)
        except Exception as e:
            _log.warning("alert", err=e)

    def ack(self, call):
        self.alert(call, text="", popup=False)

    def delete(self, message) -> bool:
        try:
            chatIdVal, msgId, _, _, ephId = self._msgTarget(message)
            if ephId is not None:
                self._api_call("deleteEphemeralMessage", chat_id=chatIdVal, ephemeral_message_id=ephId)
            else:
                self._api_call("deleteMessage", chat_id=chatIdVal, message_id=msgId)
            return True
        except Exception:
            return False

    def deleteLater(self, message, delay: float):
        timer = threading.Timer(delay, self.delete, args=[message])
        timer.daemon = True
        self._pendingTimers.append(timer)
        timer.start()
        return timer

    def forward(self, target, message):
        fromChat, msgId = self._msgFrom(message)
        return self._api_call("forwardMessage", chat_id=chatId(target), from_chat_id=fromChat, message_id=msgId)

    def forwardMessages(self, target, fromChatId: int, messageIds: list, **kwargs):
        return self._api_call("forwardMessages", chat_id=chatId(target), from_chat_id=fromChatId, message_ids=messageIds, **kwargs)

    def copy(self, target, message, **kwargs):
        fromChat, msgId = self._msgFrom(message)
        return self._api_call("copyMessage", chat_id=chatId(target), from_chat_id=fromChat, message_id=msgId, **kwargs)

    def copyMessages(self, target, fromChatId: int, messageIds: list, **kwargs):
        return self._api_call("copyMessages", chat_id=chatId(target), from_chat_id=fromChatId, message_ids=messageIds, **kwargs)

    def action(self, target, action: str = "typing"):
        return self._safe("sendChatAction", chat_id=chatId(target), action=action)

    def pin(self, message, notify: bool = False):
        try:
            chatIdVal, msgId, _, bcId, _ = self._msgTarget(message)
            self._api_call("pinChatMessage", chat_id=chatIdVal, message_id=msgId, disable_notification=not notify, business_connection_id=bcId)
        except Exception as e:
            _log.warning("pin", err=e)

    def unpin(self, target, msgId: int = None, bcId: str = None):
        cid = chatId(target)
        try:
            if msgId is not None:
                self._api_call("unpinChatMessage", chat_id=cid, message_id=msgId, business_connection_id=bcId)
            else:
                self._api_call("unpinAllChatMessages", chat_id=cid)
        except Exception as e:
            _log.warning("unpin", err=e)

    def photo(self, target, photo, caption=None, inline=None, keyboard=None, **kwargs):
        cid = chatId(target)
        markup = self._resolveMarkup(inline, keyboard)
        if isinstance(photo, list):
            media = [{"type": "photo", "media": p.get("file") if isinstance(p, dict) else p} for p in photo]
            if caption:
                media[0].update(caption=caption, parse_mode=self.parse_mode)
            return self._api_call("sendMediaGroup", chat_id=cid, media=media, **kwargs)
        if hasattr(photo, "read") or isinstance(photo, (bytes, bytearray)):
            return self._api_callFile("sendPhoto", "photo", photo, "photo.jpg", "image/jpeg",
                chat_id=cid, caption=caption, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)
        return self._api_call("sendPhoto", chat_id=cid, photo=photo, caption=caption, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def businessPhoto(self, target, photo, bcId: str, caption=None, inline=None, keyboard=None, **kwargs):
        return self.photo(target, photo, caption=caption, inline=inline, keyboard=keyboard, business_connection_id=bcId, **kwargs)

    def animation(self, target, animation, caption: str = None, **kwargs):
        return self._api_call("sendAnimation", chat_id=chatId(target), animation=animation, caption=caption, parse_mode=self.parse_mode, **kwargs)

    def businessAnimation(self, target, animation, bcId: str, caption: str = None, **kwargs):
        return self.animation(target, animation, caption=caption, business_connection_id=bcId, **kwargs)

    def videoNote(self, target, videoNote, **kwargs):
        return self._api_call("sendVideoNote", chat_id=chatId(target), video_note=videoNote, **kwargs)

    def businessVideoNote(self, target, videoNote, bcId: str, **kwargs):
        return self.videoNote(target, videoNote, business_connection_id=bcId, **kwargs)

    def paidMedia(self, target, starCount: int, media: list, **kwargs):
        return self._api_call("sendPaidMedia", chat_id=chatId(target), star_count=starCount, media=media, **kwargs)

    def businessPaidMedia(self, target, starCount: int, media: list, bcId: str, **kwargs):
        return self.paidMedia(target, starCount, media, business_connection_id=bcId, **kwargs)

    def react(self, msg, *emojis, isBig=False):
        cid, mid, _, _, _ = self._msgTarget(msg)
        reaction = []
        for e in emojis:
            if isinstance(e, str) and e.isdigit():
                reaction.append({"type": "custom_emoji", "custom_emoji_id": e})
            else:
                reaction.append({"type": "emoji", "emoji": e})
        try:
            self._api_call("setMessageReaction", chat_id=cid, message_id=mid, reaction=reaction, is_big=isBig)
        except Exception as e:
            _log.warning("react", chat=cid, msg=mid, err=e)

    def _normalizeMediaItem(self, index: int, item, caption: str = None) -> dict:
        fields = {"caption": caption, "parse_mode": self.parse_mode if caption else None}
        return _resolveInputMedia(item, index=index, extensionlessOnly=True, **fields)

    def media(self, target, items, caption: str = None, inline=None, keyboard=None, **kwargs):
        cid = chatId(target)
        markup = self._resolveMarkup(inline, keyboard)

        if not isinstance(items, list):
            items = [items]
        if not items:
            _log.warning("media", reason="empty_list")
            return None
        if len(items) > 10:
            _log.warning("media", truncated_from=len(items), truncated_to=10)
            items = items[:10]

        normalized = [
            self._normalizeMediaItem(i, item, caption if i == 0 else None)
            for i, item in enumerate(items)
        ]

        if len(normalized) > 1:
            self._debug("media", chat=cid, count=len(normalized))
            return self._api_callMediaGroup(cid, normalized, reply_markup=markup, **kwargs)

        m = normalized[0]
        mtype = m.get("type", "photo")
        cap = m.get("caption")
        _METHODS = {
            "video": ("sendVideo", "video"),
            "audio": ("sendAudio", "audio"),
            "document": ("sendDocument", "document"),
        }
        method, key = _METHODS.get(mtype, ("sendPhoto", "photo"))
        if m.get("_bytes") is not None:
            return self._api_callFile(method, fileKey=key, fileObj=m["_bytes"], filename=m["_filename"], contentType=m["_content_type"], chat_id=cid, caption=cap, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)
        return self._api_call(method, chat_id=cid, **{key: m["media"]}, caption=cap, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def businessMedia(self, target, items, bcId: str, caption: str = None, inline=None, keyboard=None, **kwargs):
        return self.media(target, items, caption=caption, inline=inline, keyboard=keyboard, business_connection_id=bcId, **kwargs)

    def video(self, target, video, caption: str = None, inline=None, keyboard=None, **kwargs):
        markup = self._resolveMarkup(inline, keyboard)
        return self._api_call("sendVideo", chat_id=chatId(target), video=video, caption=caption, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def businessVideo(self, target, video, bcId: str, caption: str = None, inline=None, keyboard=None, **kwargs):
        return self.video(target, video, caption=caption, inline=inline, keyboard=keyboard, business_connection_id=bcId, **kwargs)

    def document(self, target, doc, caption: str = None, inline=None, keyboard=None, **kwargs):
        markup = self._resolveMarkup(inline, keyboard)
        return self._api_call("sendDocument", chat_id=chatId(target), document=doc, caption=caption, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def businessDocument(self, target, doc, bcId: str, caption: str = None, inline=None, keyboard=None, **kwargs):
        return self.document(target, doc, caption=caption, inline=inline, keyboard=keyboard, business_connection_id=bcId, **kwargs)

    def audio(self, target, audio, caption: str = None, inline=None, keyboard=None, **kwargs):
        markup = self._resolveMarkup(inline, keyboard)
        return self._api_call("sendAudio", chat_id=chatId(target), audio=audio, caption=caption, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def businessAudio(self, target, audio, bcId: str, caption: str = None, inline=None, keyboard=None, **kwargs):
        return self.audio(target, audio, caption=caption, inline=inline, keyboard=keyboard, business_connection_id=bcId, **kwargs)

    def voice(self, target, voice, caption: str = None, inline=None, keyboard=None, **kwargs):
        markup = self._resolveMarkup(inline, keyboard)
        return self._api_call("sendVoice", chat_id=chatId(target), voice=voice, caption=caption, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def businessVoice(self, target, voice, bcId: str, caption: str = None, inline=None, keyboard=None, **kwargs):
        return self.voice(target, voice, caption=caption, inline=inline, keyboard=keyboard, business_connection_id=bcId, **kwargs)

    def sticker(self, target, sticker, **kwargs):
        return self._api_call("sendSticker", chat_id=chatId(target), sticker=sticker, **kwargs)

    def businessSticker(self, target, sticker, bcId: str, **kwargs):
        return self.sticker(target, sticker, business_connection_id=bcId, **kwargs)

    def location(self, target, latitude: float, longitude: float, **kwargs):
        return self._api_call("sendLocation", chat_id=chatId(target), latitude=latitude, longitude=longitude, **kwargs)

    def businessLocation(self, target, latitude: float, longitude: float, bcId: str, **kwargs):
        return self.location(target, latitude, longitude, business_connection_id=bcId, **kwargs)

    def venue(self, target, latitude: float, longitude: float, title: str, address: str, **kwargs):
        return self._api_call("sendVenue", chat_id=chatId(target), latitude=latitude, longitude=longitude, title=title, address=address, **kwargs)

    def businessVenue(self, target, latitude: float, longitude: float, title: str, address: str, bcId: str, **kwargs):
        return self.venue(target, latitude, longitude, title, address, business_connection_id=bcId, **kwargs)

    def contact(self, target, phone: str, firstName: str, lastName: str = None, **kwargs):
        return self._api_call("sendContact", chat_id=chatId(target), phone_number=phone, first_name=firstName, last_name=lastName, **kwargs)

    def businessContact(self, target, phone: str, firstName: str, bcId: str, lastName: str = None, **kwargs):
        return self.contact(target, phone, firstName, lastName=lastName, business_connection_id=bcId, **kwargs)

    def poll(self, target, question: str, options: list, isAnonymous: bool = True, pollType: str = "regular", correctOptionIds: list = None, explanation: str = None, allowsMultipleAnswers: bool = False, allowsRevoting: bool = None, shuffleOptions: bool = None, allowAddingOptions: bool = None, hideResultsUntilCloses: bool = None, description: str = None, openPeriod: int = None, closeDate: int = None, membersOnly: bool = None, countryCodes: list = None, **kwargs):
        params = dict(chat_id=chatId(target), question=question, options=[{"text": o} if isinstance(o, str) else o for o in options], is_anonymous=isAnonymous, type=pollType, allows_multiple_answers=allowsMultipleAnswers, explanation=explanation)
        if correctOptionIds is not None:
            params["correct_option_ids"] = correctOptionIds
        if allowsRevoting is not None:
            params["allows_revoting"] = allowsRevoting
        if shuffleOptions is not None:
            params["shuffle_options"] = shuffleOptions
        if allowAddingOptions is not None:
            params["allow_adding_options"] = allowAddingOptions
        if hideResultsUntilCloses is not None:
            params["hide_results_until_closes"] = hideResultsUntilCloses
        if description is not None:
            params["description"] = description
        if openPeriod is not None:
            params["open_period"] = openPeriod
        if closeDate is not None:
            params["close_date"] = closeDate
        if membersOnly is not None:
            params["members_only"] = membersOnly
        if countryCodes is not None:
            params["country_codes"] = countryCodes
        params.update(kwargs)
        return self._api_call("sendPoll", **params)

    def businessPoll(self, target, question: str, options: list, bcId: str, **kwargs):
        return self.poll(target, question, options, business_connection_id=bcId, **kwargs)

    def stopPoll(self, chatIdVal: int, messageId: int, **kwargs):
        return self._api_call("stopPoll", chat_id=chatIdVal, message_id=messageId, **kwargs)

    def businessStopPoll(self, chatIdVal: int, messageId: int, bcId: str, **kwargs):
        return self.stopPoll(chatIdVal, messageId, business_connection_id=bcId, **kwargs)

    def dice(self, target, emoji: str = "\U0001f3b2", **kwargs):
        return self._api_call("sendDice", chat_id=chatId(target), emoji=emoji, **kwargs)

    def businessDice(self, target, bcId: str, emoji: str = "\U0001f3b2", **kwargs):
        return self.dice(target, emoji=emoji, business_connection_id=bcId, **kwargs)

    def game(self, target, gameShortName: str, inline=None, keyboard=None, **kwargs):
        markup = self._resolveMarkup(inline, keyboard)
        return self._api_call("sendGame", chat_id=chatId(target), game_short_name=gameShortName, reply_markup=markup, **kwargs)

    def livePhoto(self, target, photo, animation, caption: str = None, inline=None, keyboard=None, **kwargs):
        markup = self._resolveMarkup(inline, keyboard)
        return self._api_call("sendLivePhoto", chat_id=chatId(target), photo=photo, animation=animation, caption=caption, parse_mode=self.parse_mode, reply_markup=markup, **kwargs)

    def messageDraft(self, target, draftId: int, text="", inline=None, keyboard=None,
                      canStop: bool = None, keepOnStop: bool = None, **kwargs):
        if isinstance(text, dict):
            markup = self._resolveMarkup(inline, keyboard)
            cleaned, attachments = _collectRichAttachments(text)
            return self._api_callRich("sendRichMessageDraft", "rich_message", cleaned, attachments,
                chat_id=chatId(target), draft_id=draftId, reply_markup=markup,
                can_stop=canStop, keep_on_stop=keepOnStop, **kwargs)
        return self._api_call("sendMessageDraft", chat_id=chatId(target), draft_id=draftId, text=text,
            can_stop=canStop, keep_on_stop=keepOnStop, **kwargs)

    def checklist(self, target, title: str, tasks: list, **kwargs):
        return self._api_call("sendChecklist", chat_id=chatId(target), title=title, tasks=tasks, **kwargs)

    def editChecklist(self, chatIdVal: int, messageId: int, title: str, tasks: list, **kwargs):
        return self._api_call("editMessageChecklist", chat_id=chatIdVal, message_id=messageId, title=title, tasks=tasks, **kwargs)

    def ban(self, chatIdVal: int, userId: int, until: int = 0, revokeMessages: bool = False):
        return self._safe("banChatMember", chat_id=chatIdVal, user_id=userId, until_date=until, revoke_messages=revokeMessages)

    def unban(self, chatIdVal: int, userId: int):
        return self._safe("unbanChatMember", chat_id=chatIdVal, user_id=userId, only_if_banned=True)

    def kick(self, chatIdVal: int, userId: int):
        self.ban(chatIdVal, userId)
        self.unban(chatIdVal, userId)

    def mute(self, chatIdVal: int, userId: int, until: int = 0):
        return self._safe("restrictChatMember", chat_id=chatIdVal, user_id=userId, permissions={"can_send_messages": False}, until_date=until)

    def unmute(self, chatIdVal: int, userId: int):
        return self._safe("restrictChatMember", chat_id=chatIdVal, user_id=userId, permissions=dict(DEFAULT_PERMISSIONS))

    def restrict(self, chatIdVal: int, userId: int, permissions: dict, until: int = 0):
        return self._safe("restrictChatMember", chat_id=chatIdVal, user_id=userId, permissions=permissions, until_date=until)

    def promote(self, chatIdVal: int, userId: int, **permissions):
        return self._safe("promoteChatMember", chat_id=chatIdVal, user_id=userId, **permissions)

    def setAdminTitle(self, chatIdVal: int, userId: int, title: str):
        return self._safe("setChatAdministratorCustomTitle", chat_id=chatIdVal, user_id=userId, custom_title=title)

    def setMemberTag(self, chatIdVal: int, userId: int, tag: str):
        return self._safe("setChatMemberTag", chat_id=chatIdVal, user_id=userId, tag=tag)

    def setChatPermissions(self, chatIdVal: int, permissions: dict):
        return self._safe("setChatPermissions", chat_id=chatIdVal, permissions=permissions)

    def approveJoin(self, chatOrRq, userId: int = None):
        if isinstance(chatOrRq, JoinRequest):
            chatIdVal, uid = chatOrRq.chat_id, chatOrRq.user_id
        else:
            chatIdVal, uid = chatOrRq, userId
        return self._safe("approveChatJoinRequest", chat_id=chatIdVal, user_id=uid)

    def declineJoin(self, chatOrRq, userId: int = None):
        if isinstance(chatOrRq, JoinRequest):
            chatIdVal, uid = chatOrRq.chat_id, chatOrRq.user_id
        else:
            chatIdVal, uid = chatOrRq, userId
        return self._safe("declineChatJoinRequest", chat_id=chatIdVal, user_id=uid)

    def answerJoinRequestQuery(self, chatIdVal: int, userId: int, queryId: str, result: dict):
        return self._safe("answerChatJoinRequestQuery", chat_id=chatIdVal, user_id=userId, query_id=queryId, result=result)

    def sendJoinRequestWebApp(self, chatIdVal: int, userId: int, webAppUrl: str, **kwargs):
        return self._api_call("sendChatJoinRequestWebApp", chat_id=chatIdVal, user_id=userId, web_app_url=webAppUrl, **kwargs)

    def deleteMessages(self, chatIdVal: int, messageIds: list):
        return self._safe("deleteMessages", chat_id=chatIdVal, message_ids=messageIds)

    def leave(self, chatIdVal: int):
        return self._safe("leaveChat", chat_id=chatIdVal)

    def setChatTitle(self, chatIdVal: int, title: str):
        return self._safe("setChatTitle", chat_id=chatIdVal, title=title)

    def setChatDescription(self, chatIdVal: int, description: str):
        return self._safe("setChatDescription", chat_id=chatIdVal, description=description)

    def setChatPhoto(self, chatIdVal: int, photo):
        return self._safe("setChatPhoto", chat_id=chatIdVal, photo=photo)

    def deleteChatPhoto(self, chatIdVal: int):
        return self._safe("deleteChatPhoto", chat_id=chatIdVal)

    def banSender(self, chatIdVal: int, senderChatId: int):
        return self._safe("banChatSenderChat", chat_id=chatIdVal, sender_chat_id=senderChatId)

    def unbanSender(self, chatIdVal: int, senderChatId: int):
        return self._safe("unbanChatSenderChat", chat_id=chatIdVal, sender_chat_id=senderChatId)

    def exportInvite(self, chatIdVal: int) -> str:
        return self._api_call("exportChatInviteLink", chat_id=chatIdVal)

    def createInvite(self, chatIdVal: int, **kwargs):
        return self._api_call("createChatInviteLink", chat_id=chatIdVal, **kwargs)

    def editInvite(self, chatIdVal: int, inviteLink: str, **kwargs):
        return self._api_call("editChatInviteLink", chat_id=chatIdVal, invite_link=inviteLink, **kwargs)

    def revokeInvite(self, chatIdVal: int, inviteLink: str):
        return self._api_call("revokeChatInviteLink", chat_id=chatIdVal, invite_link=inviteLink)

    def createSubscriptionInvite(self, chatIdVal: int, subscriptionPeriod: int, subscriptionPrice: int, **kwargs):
        return self._api_call("createChatSubscriptionInviteLink", chat_id=chatIdVal, subscription_period=subscriptionPeriod, subscription_price=subscriptionPrice, **kwargs)

    def editSubscriptionInvite(self, chatIdVal: int, inviteLink: str, **kwargs):
        return self._api_call("editChatSubscriptionInviteLink", chat_id=chatIdVal, invite_link=inviteLink, **kwargs)

    def createTopic(self, chatIdVal: int, name: str, **kwargs):
        return self._api_call("createForumTopic", chat_id=chatIdVal, name=name, **kwargs)

    def editTopic(self, chatIdVal: int, messageThreadId: int, **kwargs):
        return self._api_call("editForumTopic", chat_id=chatIdVal, message_thread_id=messageThreadId, **kwargs)

    def closeTopic(self, chatIdVal: int, messageThreadId: int):
        return self._safe("closeForumTopic", chat_id=chatIdVal, message_thread_id=messageThreadId)

    def reopenTopic(self, chatIdVal: int, messageThreadId: int):
        return self._safe("reopenForumTopic", chat_id=chatIdVal, message_thread_id=messageThreadId)

    def deleteTopic(self, chatIdVal: int, messageThreadId: int):
        return self._safe("deleteForumTopic", chat_id=chatIdVal, message_thread_id=messageThreadId)

    def unpinTopicMessages(self, chatIdVal: int, messageThreadId: int):
        return self._safe("unpinAllForumTopicMessages", chat_id=chatIdVal, message_thread_id=messageThreadId)

    def editGeneralTopic(self, chatIdVal: int, name: str):
        return self._safe("editGeneralForumTopic", chat_id=chatIdVal, name=name)

    def closeGeneralTopic(self, chatIdVal: int):
        return self._safe("closeGeneralForumTopic", chat_id=chatIdVal)

    def reopenGeneralTopic(self, chatIdVal: int):
        return self._safe("reopenGeneralForumTopic", chat_id=chatIdVal)

    def hideGeneralTopic(self, chatIdVal: int):
        return self._safe("hideGeneralForumTopic", chat_id=chatIdVal)

    def unhideGeneralTopic(self, chatIdVal: int):
        return self._safe("unhideGeneralForumTopic", chat_id=chatIdVal)

    def unpinGeneralTopicMessages(self, chatIdVal: int):
        return self._safe("unpinAllGeneralForumTopicMessages", chat_id=chatIdVal)

    def answerInlineQuery(self, query, results: list, cacheTime: int = 30, isPersonal: bool = True, nextOffset: str = ""):
        qid = query.id if isinstance(query, InlineQuery) else query.get("id") if isinstance(query, dict) else query.id
        return self._safe("answerInlineQuery", inline_query_id=qid, results=results, cache_time=cacheTime, is_personal=isPersonal, next_offset=nextOffset)

    def answerWebAppQuery(self, webAppQueryId: str, result: dict):
        return self._safe("answerWebAppQuery", web_app_query_id=webAppQueryId, result=result)

    def answerGuestQuery(self, queryId: str, text: str, parseMode: str = None, **kwargs):
        return self._api_call("answerGuestQuery", guest_query_id=queryId, message={"text": text, "parse_mode": parseMode or self.parse_mode, **kwargs})

    def answerShippingQuery(self, shippingQueryId: str, ok: bool, **kwargs):
        return self._api_call("answerShippingQuery", shipping_query_id=shippingQueryId, ok=ok, **kwargs)

    def savePreparedInlineMessage(self, userId: int, result: dict, allowUserChats: bool = None, allowBotChats: bool = None, allowGroupChats: bool = None, allowChannelChats: bool = None):
        return self._api_call("savePreparedInlineMessage", user_id=userId, result=result, allow_user_chats=allowUserChats, allow_bot_chats=allowBotChats, allow_group_chats=allowGroupChats, allow_channel_chats=allowChannelChats)

    def savePreparedKeyboardButton(self, text: str, requestUser: dict = None, requestChat: dict = None, requestManagedBot: dict = None, webApp: dict = None, loginUrl: dict = None, switchInline: str = None, switchInlineCurrent: str = None, switchInlineChosen: dict = None, copyText: dict = None, pay: bool = None):
        btn = {"text": text}
        fields = (
            ("request_users", requestUser),
            ("request_chat", requestChat),
            ("request_managed_bot", requestManagedBot),
            ("web_app", webApp),
            ("login_url", loginUrl),
            ("switch_inline_query", switchInline),
            ("switch_inline_query_current_chat", switchInlineCurrent),
            ("switch_inline_query_chosen_chat", switchInlineChosen),
            ("copy_text", copyText),
            ("pay", pay),
        )
        btn.update((key, value) for key, value in fields if value)
        return self._api_call("savePreparedKeyboardButton", button=btn)

    def invoice(self, target, title: str, description: str, payload: str, stars: int, photoUrl: str = None, inline=None, protectContent: bool = False, **kwargs):
        prices = [{"label": title, "amount": stars}]
        markup = buildInlineKeyboard(inline) if inline is not None else None
        return self._api_call("sendInvoice", chat_id=chatId(target), title=title, description=description, payload=payload, provider_token="", currency="XTR", prices=prices, photo_url=photoUrl, reply_markup=markup, protect_content=protectContent, **kwargs)

    def createInvoiceLink(self, title: str, description: str, payload: str, stars: int, photoUrl: str = None, subscriptionPeriod: int = None, **kwargs) -> str:
        prices = [{"label": title, "amount": stars}]
        return self._api_call("createInvoiceLink", title=title, description=description, payload=payload, provider_token="", currency="XTR", prices=prices, photo_url=photoUrl, subscription_period=subscriptionPeriod, **kwargs)

    def gift(self, userId: int, giftId: str, payForUpgrade: bool = False, **kwargs):
        return self._api_call("sendGift", user_id=userId, gift_id=giftId, pay_for_upgrade=payForUpgrade, **kwargs)

    def giftPremium(self, userId: int, **kwargs):
        return self._api_call("giftPremiumSubscription", user_id=userId, **kwargs)

    def refund(self, userId: int, chargeId: str) -> bool:
        try:
            self._api_call("refundStarPayment", user_id=userId, telegram_payment_charge_id=chargeId)
            return True
        except Exception as e:
            _log.warning("refund", err=e)
            return False

    def getStarTransactions(self, offset: int = 0, limit: int = 100):
        return self._api_call("getStarTransactions", offset=offset, limit=limit)

    def getStarBalance(self):
        try:
            result = self._api_call("getMyStarBalance")
            return result.amount if result else None
        except Exception as e:
            _log.warning("getStarBalance", err=e)
            return None

    def editStarSubscription(self, userId: int, telegramPaymentChargeId: str, isCanceled: bool):
        return self._safe("editUserStarSubscription", user_id=userId, telegram_payment_charge_id=telegramPaymentChargeId, is_canceled=isCanceled)

    def setEmojiStatus(self, userId: int, customEmojiId: str = None, **kwargs) -> bool:
        try:
            self._api_call("setUserEmojiStatus", user_id=userId, emoji_status={"custom_emoji_id": customEmojiId} if customEmojiId else {}, **kwargs)
            return True
        except Exception as e:
            _log.warning("setEmojiStatus", err=e)
            return False

    def getUserPhotos(self, userId: int, offset: int = 0, limit: int = 100):
        return self._api_call("getUserProfilePhotos", user_id=userId, offset=offset, limit=limit)

    def getUserAudios(self, userId: int, offset: int = 0, limit: int = 100):
        return self._api_call("getUserProfileAudios", user_id=userId, offset=offset, limit=limit)

    def getPersonalMessages(self, userId: int, **kwargs):
        return self._api_call("getUserPersonalChatMessages", user_id=userId, **kwargs)

    def getChat(self, target):
        return self._api_call("getChat", chat_id=chatId(target))

    def getChatMember(self, chatIdVal: int, userId: int):
        return self._api_call("getChatMember", chat_id=chatIdVal, user_id=userId)

    def getChatMemberCount(self, chatIdVal: int) -> int:
        return self._api_call("getChatMemberCount", chat_id=chatIdVal)

    def isAdmin(self, chatIdVal: int, userId: int) -> bool:
        try:
            member = self.getChatMember(chatIdVal, userId)
            return member.status in ("administrator", "creator")
        except Exception:
            return False

    def getAdmins(self, chatIdVal: int, includeBots: bool = False) -> list:
        admins = self._api_call("getChatAdministrators", chat_id=chatIdVal)
        if not includeBots:
            admins = [a for a in admins if not getattr(a, "is_bot", False)]
        return admins

    def getUserBoosts(self, chatIdVal: int, userId: int):
        return self._api_call("getUserChatBoosts", chat_id=chatIdVal, user_id=userId)

    def getMe(self):
        return self._api_call("getMe")

    def getFile(self, fileId: str):
        return self._api_call("getFile", file_id=fileId)

    def getWebhookInfo(self):
        return self._api_call("getWebhookInfo")

    def setMyCommands(self, commands: list, scope: dict = None, languageCode: str = None):
        return self._api_call("setMyCommands", commands=commands, scope=scope, language_code=languageCode)

    def deleteMyCommands(self, scope: dict = None, languageCode: str = None):
        return self._api_call("deleteMyCommands", scope=scope, language_code=languageCode)

    def getMyCommands(self, scope: dict = None, languageCode: str = None):
        return self._api_call("getMyCommands", scope=scope, language_code=languageCode)

    def setMyName(self, name: str, languageCode: str = None):
        return self._api_call("setMyName", name=name, language_code=languageCode)

    def getMyName(self, languageCode: str = None):
        return self._api_call("getMyName", language_code=languageCode)

    def setMyDescription(self, description: str, languageCode: str = None):
        return self._api_call("setMyDescription", description=description, language_code=languageCode)

    def getMyDescription(self, languageCode: str = None):
        return self._api_call("getMyDescription", language_code=languageCode)

    def setMyShortDescription(self, shortDescription: str, languageCode: str = None):
        return self._api_call("setMyShortDescription", short_description=shortDescription, language_code=languageCode)

    def getMyShortDescription(self, languageCode: str = None):
        return self._api_call("getMyShortDescription", language_code=languageCode)

    def setMyProfilePhoto(self, photo):
        return self._api_call("setMyProfilePhoto", photo=photo)

    def removeMyProfilePhoto(self, photoId: str = None):
        return self._api_call("removeMyProfilePhoto", custom_emoji_id=photoId)

    def setMenuButton(self, chatIdVal: int = None, menuButton: dict = None):
        return self._api_call("setChatMenuButton", chat_id=chatIdVal, menu_button=menuButton)

    def getMenuButton(self, chatIdVal: int = None):
        return self._api_call("getChatMenuButton", chat_id=chatIdVal)

    def setDefaultAdminRights(self, rights: dict = None, forChannels: bool = False):
        return self._api_call("setMyDefaultAdministratorRights", rights=rights, for_channels=forChannels)

    def getDefaultAdminRights(self, forChannels: bool = False):
        return self._api_call("getMyDefaultAdministratorRights", for_channels=forChannels)

    def logOut(self):
        return self._api_call("logOut")

    def closeBot(self):
        return self._api_call("close")

    def getAccessSettings(self):
        return self._api_call("getManagedBotAccessSettings")

    def setAccessSettings(self, settings: dict):
        return self._api_call("setManagedBotAccessSettings", **settings)

    def getManagedToken(self):
        return self._api_call("getManagedBotToken")

    def replaceManagedToken(self):
        return self._api_call("replaceManagedBotToken")

    def getStickerSet(self, name: str):
        return self._api_call("getStickerSet", name=name)

    def getCustomEmojiStickers(self, customEmojiIds: list):
        return self._api_call("getCustomEmojiStickers", custom_emoji_ids=customEmojiIds)

    def uploadStickerFile(self, userId: int, sticker, stickerFormat: str):
        return self._api_call("uploadStickerFile", user_id=userId, sticker=sticker, sticker_format=stickerFormat)

    def createNewStickerSet(self, userId: int, name: str, title: str, stickers: list, **kwargs):
        return self._api_call("createNewStickerSet", user_id=userId, name=name, title=title, stickers=stickers, **kwargs)

    def addStickerToSet(self, userId: int, name: str, sticker: dict):
        return self._api_call("addStickerToSet", user_id=userId, name=name, sticker=sticker)

    def setStickerPosition(self, sticker: str, position: int):
        return self._api_call("setStickerPositionInSet", sticker=sticker, position=position)

    def deleteStickerFromSet(self, sticker: str):
        return self._api_call("deleteStickerFromSet", sticker=sticker)

    def replaceStickerInSet(self, userId: int, name: str, oldSticker: str, sticker: dict):
        self._api_call("replaceStickerInSet", user_id=userId, name=name, old_sticker=oldSticker, sticker=sticker)

    def setStickerEmojiList(self, sticker: str, emojiList: list):
        return self._api_call("setStickerEmojiList", sticker=sticker, emoji_list=emojiList)

    def setStickerKeywords(self, sticker: str, keywords: list):
        return self._api_call("setStickerKeywords", sticker=sticker, keywords=keywords)

    def setStickerMaskPosition(self, sticker: str, maskPosition: dict):
        return self._api_call("setStickerMaskPosition", sticker=sticker, mask_position=maskPosition)

    def setStickerSetTitle(self, name: str, title: str):
        return self._api_call("setStickerSetTitle", name=name, title=title)

    def setStickerSetThumbnail(self, name: str, userId: int, thumbnail: str = None):
        return self._api_call("setStickerSetThumbnail", name=name, user_id=userId, thumbnail=thumbnail)

    def setCustomEmojiStickerSetThumbnail(self, name: str, customEmojiId: str = None):
        return self._api_call("setCustomEmojiStickerSetThumbnail", name=name, custom_emoji_id=customEmojiId)

    def deleteStickerSet(self, name: str):
        return self._api_call("deleteStickerSet", name=name)

    def setChatStickerSet(self, chatIdVal: int, stickerSetName: str):
        return self._api_call("setChatStickerSet", chat_id=chatIdVal, sticker_set_name=stickerSetName)

    def deleteChatStickerSet(self, chatIdVal: int):
        return self._api_call("deleteChatStickerSet", chat_id=chatIdVal)

    def getGifts(self):
        return self._api_call("getAvailableGifts")

    def convertGiftToStars(self, giftId: str):
        return self._api_call("convertGiftToStars", gift_id=giftId)

    def upgradeGift(self, giftId: str, **kwargs):
        return self._api_call("upgradeGift", gift_id=giftId, **kwargs)

    def transferGift(self, giftId: str, userId: int):
        self._api_call("transferGift", gift_id=giftId, user_id=userId)

    def setWebhook(self, url: str, secretToken: str = None, allowedUpdates: list = None, maxConnections: int = None, ipAddress: str = None, certificate=None, dropPending: bool = False):
        params = {"url": url, "drop_pending_updates": dropPending}
        if secretToken:
            params["secret_token"] = secretToken
        if allowedUpdates is not None:
            params["allowed_updates"] = allowedUpdates
        if maxConnections:
            params["max_connections"] = maxConnections
        if ipAddress:
            params["ip_address"] = ipAddress
        if certificate:
            params["certificate"] = certificate
        self._api_call("setWebhook", **params)

    def deleteWebhook(self, dropPending: bool = False):
        self._api_call("deleteWebhook", drop_pending_updates=dropPending)

    def processUpdate(self, update: dict):
        raw = dict(update) if isinstance(update, Obj) else update
        self._dispatch(raw)

    def _dispatch(self, update: dict):
        if "business_connection" in update:
            conn = BusinessConnection(update["business_connection"])
            with self._bizConnCacheLock:
                self._bizConnCache[conn.id] = conn
            for h in self._bizConnectionHandlers:
                self._submit(h, conn)
            return

        if "business_message" in update:
            msg = BusinessMessage(update["business_message"], self)
            for h in self._bizMsgHandlers:
                h(msg)
            return

        if "edited_business_message" in update:
            msg = BusinessMessage(update["edited_business_message"], self)
            for h in self._bizEditedHandlers:
                self._submit(h, msg)
            return

        if "deleted_business_messages" in update:
            raw = wrap(update["deleted_business_messages"])
            for h in self._bizDeletedHandlers:
                self._submit(h, raw)
            return

        if "guest_message" in update:
            raw = update["guest_message"]
            for h in self._guestHandlers:
                self._submit(h, GuestQuery(raw, self))
            return

        elif "managed_bot" in update:
            raw = wrap(update["managed_bot"])
            for h in self._managedHandlers:
                self._submit(h, raw)

        elif "stopped_message_generation" in update:
            raw = wrap(update["stopped_message_generation"])
            for h in self._generationStoppedHandlers:
                self._submit(h, raw)

        elif "message" in update:
            raw = update["message"]
            if "successful_payment" in raw:
                msg = Message(raw, [])
                for h in self._paymentHandlers:
                    self._submit(h, msg)
            if "web_app_data" in raw:
                msg = Message(raw, [])
                for h in self._webappHandlers:
                    self._submit(h, msg)
            for block in self._commandBlocks:
                if block._matchTrigger(raw):
                    block.dispatchMessage(raw)
                    return
            for h in self._messageHandlers:
                h(raw)
            for h in self._mediaHandlers:
                h(raw)
            for h in self._anyHandlers:
                h(raw)

        elif "callback_query" in update:
            raw = update["callback_query"]
            data = raw.get("data") or ""
            parts = data.split(":")
            for block in self._commandBlocks:
                if len(parts) >= 2 and parts[1].lower() in block._callbackRoutes:
                    block.dispatchCallback(raw)
                    return
            for h in self._callbackHandlers:
                h(raw)

        elif "inline_query" in update:
            raw = update["inline_query"]
            for h in self._inlineHandlers:
                h(raw)

        elif "pre_checkout_query" in update:
            raw = update["pre_checkout_query"]
            pco = PreCheckout(raw, self)
            for h in self._checkoutHandlers:
                self._submit(h, pco)

        elif "purchased_paid_media" in update:
            raw = wrap(update["purchased_paid_media"])
            for h in self._paidMediaHandlers:
                self._submit(h, raw)

        elif "edited_message" in update:
            raw = update["edited_message"]
            for h in self._editedHandlers:
                h(raw)

        elif "channel_post" in update:
            raw = update["channel_post"]
            for h in self._postHandlers:
                h(raw)

        elif "edited_channel_post" in update:
            raw = update["edited_channel_post"]
            for h in self._editedHandlers:
                h(raw)

        elif "message_reaction" in update:
            raw = wrap(update["message_reaction"])
            for h in self._reactionHandlers:
                self._submit(h, raw)

        elif "poll_answer" in update:
            raw = wrap(update["poll_answer"])
            for h in self._pollHandlers:
                self._submit(h, raw)

        elif "chat_join_request" in update:
            rq = JoinRequest(update["chat_join_request"])
            for h in self._joinHandlers:
                self._submit(h, rq)

        elif "chat_boost" in update:
            raw = wrap(update["chat_boost"])
            for h in self._boostHandlers:
                self._submit(h, raw)

        elif "removed_chat_boost" in update:
            raw = wrap(update["removed_chat_boost"])
            for h in self._boostHandlers:
                self._submit(h, raw)

        elif "my_chat_member" in update:
            for h in self._myStatusHandlers:
                self._submit(h, wrap(update["my_chat_member"]))

        elif "chat_member" in update:
            for h in self._memberHandlers:
                self._submit(h, wrap(update["chat_member"]))

    def _autoUpdates(self) -> list:
        needed = set()
        for attr, updates in HANDLER_UPDATES.items():
            if getattr(self, attr, []):
                needed.update(updates)
        return sorted(needed) if needed else None

    def run(self, skipPending: bool = False, pollTimeout: int = 20, allowedUpdates: list = None):
        self._run_coro(self._run(skipPending, pollTimeout, allowedUpdates))

    def start(self, skipPending: bool = False, pollTimeout: int = 20, allowedUpdates: list = None):
        self._ensure_loop()
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._run(skipPending, pollTimeout, allowedUpdates), self._loop)
        else:
            self._loop.run_until_complete(self._run(skipPending, pollTimeout, allowedUpdates))

    async def _run(self, skipPending: bool = False, pollTimeout: int = 20, allowedUpdates: list = None):
        try:
            me = await self._api.call("getMe")
        except TelegramError as e:
            if e.error_code == 401:
                _log.error("startup", reason="invalid_token", err=e.description)
            else:
                _log.error("startup", reason="getMe_failed", err=e)
            return
        _log.info("ready", bot=f"@{me.username}", id=me.id)

        if allowedUpdates is None:
            allowedUpdates = self._autoUpdates()
        if allowedUpdates:
            _log.info("allowed_updates", updates=allowedUpdates)

        offset = 0
        if skipPending:
            try:
                updates = await self._api.call("getUpdates", offset=-1, timeout=1, limit=1)
                if updates:
                    updates = updates if isinstance(updates, list) else [updates]
                    offset = updates[-1].update_id + 1
                    _log.info("skip_pending", offset=offset)
            except Exception as e:
                _log.warning("skip_pending", err=e)

        self._debug("polling_start")
        while not self._stopEvent.is_set():
            try:
                updates = await self._api.call("getUpdates", offset=offset, timeout=pollTimeout, limit=100, allowed_updates=allowedUpdates)
                if not updates:
                    continue
                if not isinstance(updates, list):
                    updates = [updates]
                for u in updates:
                    uid = u.update_id if hasattr(u, "update_id") else u.get("update_id", 0)
                    offset = uid + 1
                    raw = _to_raw(u) if isinstance(u, Obj) else u
                    self._dispatch(raw)
            except TelegramError as e:
                if "conflict" in str(e).lower():
                    _log.error("polling", reason="conflict", err=e)
                    break
                _log.warning("polling", op="getUpdates", err=e)
                await asyncio.sleep(1)
            except Exception as e:
                if not self._stopEvent.is_set():
                    _log.warning("polling", err=e)
                    await asyncio.sleep(1)

        await self._shutdown()

    async def _shutdown(self):
        self._stopEvent.set()
        for t in self._pendingTimers:
            with suppress(Exception):
                t.cancel()
        self._pendingTimers.clear()
        await self._api.close()
        for fn in self._stopCallbacks:
            try:
                fn()
            except Exception:
                _log.error("stop callback error:", exc_info=True)
        if self._loop_owned and self._loop is not None:
            self._loop.close()
            self._loop = None

    def close(self):
        self._run_coro(self._shutdown())