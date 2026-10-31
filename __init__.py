"""娅娅视频解析：astrbot_plugin_media_parser 7.0.1（含 2026-10 本地补丁）在 NoneBot2 下的入口。

插件代码放在 media-resolver/，通过 astrbot_shim 提供所需的 AstrBot 接口。
配置沿用 AstrBot 版 JSON（MEDIA_PARSER_CONFIG）；缓存位于 MEDIA_PARSER_DATA_DIR 下，
该目录需对 SnowLuma 容器同路径可见，媒体以 file:// 本地路径发送。
"""

import asyncio
import importlib

from nonebot import get_driver, get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel

from . import astrbot_shim


class Config(BaseModel):
    media_parser_config: str = "/opt/qqbot/nonebot/config/media_parser.json"
    media_parser_data_dir: str = "/opt/qqbot/snowluma/data/nonebot"


__plugin_meta__ = PluginMetadata(
    name="media_parser",
    description="娅娅视频解析：B站/抖音/快手/微博/小红书/闲鱼/头条/小黑盒/X/TikTok/Pixiv 链接解析",
    usage="自动识别链接；管理员私聊“清理媒体”清缓存、“B站更新Cookie”扫码更新 Cookie",
    config=Config,
)

cfg = get_plugin_config(Config)
astrbot_shim.install(cfg.media_parser_data_dir)

# 目录名含连字符，不能写成 import 语句；插件内部都是相对导入，不受影响
VideoParserPlugin = importlib.import_module(".media-resolver.main", __name__).VideoParserPlugin

driver = get_driver()
_plugin = None
_tasks: set[asyncio.Task] = set()


@driver.on_startup
async def _start() -> None:
    global _plugin
    # 初始化失败只停用解析，不影响 NoneBot 其他插件（GsCore 等）
    try:
        _plugin = VideoParserPlugin(
            astrbot_shim.Context(), astrbot_shim.PluginConfig(cfg.media_parser_config)
        )
    except Exception:
        logger.exception("[media_parser] 初始化失败，视频解析已停用")


@driver.on_shutdown
async def _stop() -> None:
    if _plugin is not None:
        await _plugin.terminate()


async def _parse(bot: Bot, event: MessageEvent) -> None:
    try:
        await _plugin.auto_parse(astrbot_shim.AstrMessageEvent(bot, event))
    except Exception:
        logger.exception("[media_parser] 处理消息失败")


matcher = on_message(priority=10, block=False)


@matcher.handle()
async def _(bot: Bot, event: MessageEvent) -> None:
    if _plugin is None:
        return
    # 下载和发送可能持续数十秒，放到后台，避免阻塞后续优先级的插件
    task = asyncio.create_task(_parse(bot, event))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
