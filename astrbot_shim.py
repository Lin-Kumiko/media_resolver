"""AstrBot 接口的最小兼容层。

只实现 astrbot_plugin_media_parser 实际用到的部分（消息组件、事件、Context、
logger、数据目录），让插件代码在 NoneBot2 + OneBot V11 下原样运行。
"""

import json
import logging
import os
import sys
import types
from pathlib import Path
from typing import Any, Iterable

from nonebot import get_bots
from nonebot import logger as nb_logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11 import Message as OBMessage
from nonebot.exception import ActionFailed

PLATFORM = "aiocqhttp"


# ── 消息组件 ───────────────────────────────────────────────


class BaseMessageComponent:
    type = "Unknown"

    def __init__(self, **kwargs: Any):
        self.__dict__.update(kwargs)


class Plain(BaseMessageComponent):
    type = "Plain"

    def __init__(self, text: str = "", **kwargs: Any):
        super().__init__(**kwargs)
        self.text = text


class _Media(BaseMessageComponent):
    def __init__(self, file: str = "", url: str = "", path: str = "", **kwargs: Any):
        super().__init__(**kwargs)
        self.file, self.url, self.path = file, url, path

    @classmethod
    def fromFileSystem(cls, path: str, **kwargs: Any):
        real = str(Path(path).resolve())
        return cls(file=f"file://{real}", path=real, **kwargs)

    @classmethod
    def fromURL(cls, url: str, **kwargs: Any):
        return cls(file=url, url=url, **kwargs)


class Image(_Media):
    type = "Image"


class Video(_Media):
    type = "Video"


class File(BaseMessageComponent):
    type = "File"

    def __init__(self, name: str = "", file: str = "", url: str = "", **kwargs: Any):
        super().__init__(**kwargs)
        self.name, self.file, self.url = name, file, url


class Reply(BaseMessageComponent):
    type = "Reply"

    def __init__(self, id: Any = "", chain: list | None = None, message_str: str = "", **kwargs: Any):
        super().__init__(**kwargs)
        self.id, self.chain, self.message_str = id, chain or [], message_str


class Json(BaseMessageComponent):
    type = "Json"

    def __init__(self, data: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.data = data


class Node(BaseMessageComponent):
    type = "Node"

    def __init__(self, content: list | None = None, name: str = "", uin: Any = "0", **kwargs: Any):
        super().__init__(**kwargs)
        self.content, self.name, self.uin = content or [], name, uin


class Nodes(BaseMessageComponent):
    type = "Nodes"

    def __init__(self, nodes: list | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.nodes = nodes or []


class MessageChain:
    def __init__(self, chain: Iterable | None = None):
        self.chain = list(chain or [])

    def message(self, text: str) -> "MessageChain":
        self.chain.append(Plain(text))
        return self


# ── 组件 ⇄ OneBot 消息段 ───────────────────────────────────


def _segments(components: Iterable) -> list[dict]:
    segs: list[dict] = []
    for c in components:
        if isinstance(c, str):
            segs.append({"type": "text", "data": {"text": c}})
        elif isinstance(c, Plain):
            segs.append({"type": "text", "data": {"text": c.text}})
        elif isinstance(c, Image):
            segs.append({"type": "image", "data": {"file": c.file}})
        elif isinstance(c, Video):
            segs.append({"type": "video", "data": {"file": c.file}})
        elif isinstance(c, Reply):
            segs.append({"type": "reply", "data": {"id": str(c.id)}})
        elif isinstance(c, Node):
            segs.append(_node(c))
        else:
            logging.getLogger(_LOGGER_NAME).warning(
                f"忽略不支持的消息组件: {type(c).__name__}"
            )
    return segs


def _node(node: Node) -> dict:
    uin = str(node.uin)
    return {
        "type": "node",
        "data": {"user_id": uin, "uin": uin, "nickname": node.name, "name": node.name,
                 "content": _segments(node.content)},
    }


def _components(message: OBMessage) -> list:
    out: list = []
    for seg in message:
        data = dict(seg.data)
        if seg.type == "text":
            out.append(Plain(data.get("text", "")))
        elif seg.type == "image":
            out.append(Image(file=data.get("file", ""), url=data.get("url", "")))
        elif seg.type == "video":
            out.append(Video(file=data.get("file", ""), url=data.get("url", "")))
        elif seg.type == "json":
            out.append(Json(data=data))
        else:
            out.append(BaseMessageComponent(type=seg.type, data=data))
    return out


# ── 发送 ───────────────────────────────────────────────────


async def _call(bot: Bot, api: str, **params: Any) -> Any:
    try:
        return await bot.call_api(api, **params)
    except ActionFailed as exc:
        # 插件按 retcode=1200 判断“未回执但已送达”
        info = getattr(exc, "info", None) or {}
        exc.retcode = info.get("retcode") if isinstance(info, dict) else None
        raise


async def send_chain(bot: Bot, private: bool, target: int, chain: list) -> None:
    nodes = [c for c in chain if isinstance(c, Nodes)]
    files = [c for c in chain if isinstance(c, File)]
    rest = [c for c in chain if not isinstance(c, (Nodes, File))]
    for n in nodes:
        messages = [_node(x) for x in n.nodes]
        if private:
            await _call(bot, "send_private_forward_msg", user_id=target, messages=messages)
        else:
            await _call(bot, "send_group_forward_msg", group_id=target, messages=messages)
    for f in files:
        path = str(Path(f.file).resolve())
        if private:
            await _call(bot, "upload_private_file", user_id=target, file=path, name=f.name)
        else:
            await _call(bot, "upload_group_file", group_id=target, file=path, name=f.name)
    if rest:
        key = "user_id" if private else "group_id"
        await _call(bot, "send_msg", message_type="private" if private else "group",
                    **{key: target}, message=_segments(rest))


def _as_chain(result: Any) -> list:
    if isinstance(result, MessageChain):
        return result.chain
    if isinstance(result, str):
        return [Plain(result)]
    return list(result or [])


# ── 事件与上下文 ───────────────────────────────────────────


class AstrMessageEvent:
    def __init__(self, bot: Bot, event: MessageEvent):
        self.bot, self._event = bot, event
        self._private = isinstance(event, PrivateMessageEvent)
        self._target = event.user_id if self._private else getattr(event, "group_id")
        kind = "FriendMessage" if self._private else "GroupMessage"
        self.unified_msg_origin = f"{PLATFORM}:{kind}:{self._target}"
        self.message_str = event.get_plaintext()
        self.message_obj = types.SimpleNamespace(
            message_id=str(event.message_id),
            raw_message={"post_type": event.post_type},
        )

    def is_private_chat(self) -> bool:
        return self._private

    def get_group_id(self) -> str:
        return "" if self._private else str(self._target)

    def get_sender_id(self) -> str:
        return str(self._event.user_id)

    def get_self_id(self) -> str:
        return str(self._event.self_id)

    def get_sender_name(self) -> str:
        sender = self._event.sender
        return (getattr(sender, "card", "") or getattr(sender, "nickname", "") or "").strip()

    def get_platform_name(self) -> str:
        return PLATFORM

    def get_messages(self) -> list:
        comps = _components(self._event.message)
        reply = self._event.reply
        if reply is not None:
            comps.insert(0, Reply(
                id=str(reply.message_id),
                chain=_components(reply.message),
                message_str=reply.message.extract_plain_text(),
            ))
        return comps

    def plain_result(self, text: str) -> MessageChain:
        return MessageChain([Plain(text)])

    def chain_result(self, chain: list) -> MessageChain:
        return MessageChain(chain)

    async def send(self, result: Any) -> None:
        await send_chain(self.bot, self._private, int(self._target), _as_chain(result))


class Context:
    """只提供 send_message（B 站 Cookie 协助登录给管理员私聊发消息时使用）。"""

    async def send_message(self, unified_msg_origin: str, chain: Any) -> None:
        _, kind, target = unified_msg_origin.split(":", 2)
        bot = next(iter(get_bots().values()))
        await send_chain(bot, kind == "FriendMessage", int(target), _as_chain(chain))


class Star:
    def __init__(self, context: Context):
        self.context = context


def register(*_args: Any, **_kwargs: Any):
    return lambda cls: cls


class _Filter:
    def event_message_type(self, *_args: Any, **_kwargs: Any):
        return lambda func: func


class EventMessageType:
    ALL = "ALL"


class PluginConfig(dict):
    """插件配置：dict + save_config()，与 AstrBotConfig 用法一致。"""

    def __init__(self, path: str):
        self._path = path
        with open(path, encoding="utf-8-sig") as f:
            super().__init__(json.load(f))

    def save_config(self) -> None:
        tmp = f"{self._path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)


class _LoguruHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: Any = nb_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        nb_logger.opt(exception=record.exc_info).log(level, record.getMessage())


_LOGGER_NAME = "qqbot.astrbot_plugin_media_parser"


def _make_logger() -> logging.Logger:
    """插件按标准库 logging 使用 logger，这里转发到 NoneBot 的 loguru。

    插件非调试模式下会 setLevel(NOTSET)，级别取自父 logger，故父 logger 固定为 INFO。
    """
    logging.getLogger("qqbot").setLevel(logging.INFO)
    lg = logging.getLogger(_LOGGER_NAME)
    lg.handlers = [_LoguruHandler()]
    lg.propagate = False
    return lg


# ── 注册为 astrbot.* 模块 ──────────────────────────────────


def install(data_dir: str) -> None:
    """把兼容层注册为 sys.modules 中的 astrbot.*；data_dir 对应 AstrBot 的 data 目录。"""
    if "astrbot" in sys.modules:
        return

    def module(name: str, **attrs: Any) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    components = dict(
        BaseMessageComponent=BaseMessageComponent, Plain=Plain, Image=Image, Video=Video,
        File=File, Reply=Reply, Json=Json, Node=Node, Nodes=Nodes,
    )
    module("astrbot")
    module("astrbot.api", logger=_make_logger())
    module("astrbot.api.event", AstrMessageEvent=AstrMessageEvent, MessageChain=MessageChain, filter=_Filter())
    module("astrbot.api.message_components", **components)
    module("astrbot.api.star", Context=Context, Star=Star, register=register)
    module("astrbot.core", astrbot_config={"data_dir": data_dir}, file_token_service=None)
    module("astrbot.core.utils")
    module("astrbot.core.utils.io", get_astrbot_data_path=lambda: data_dir)
    module("astrbot.core.star")
    module("astrbot.core.star.filter")
    module("astrbot.core.star.filter.event_message_type", EventMessageType=EventMessageType)
