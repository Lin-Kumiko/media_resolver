# media_resolver

QQ 机器人用的链接解析器：识别群聊/私聊里的视频、图文、直播链接，下载媒体并渲染成卡片发送。运行在 NoneBot2 + OneBot V11 上。

基于 [astrbot_plugin_media_parser_yaya](https://github.com/xiaoxi2760/astrbot_plugin_media_parser_yaya)（娅娅视频解析 v7.0.1，作者 xiaoxi2760），原插件为 AstrBot 插件，这里通过兼容层改在 NoneBot2 下运行，并做了下列改动。

## 支持的平台

B站（视频、图文、直播间）、抖音（视频、图文、直播间）、快手、微博、小红书、闲鱼、今日头条/西瓜、小黑盒、X/Twitter、TikTok、Pixiv（插画、漫画、动图、小说）。

## 目录

```
__init__.py                  NoneBot2 插件入口（on_message，后台任务处理）
astrbot_shim.py              兼容层：模拟原插件用到的 AstrBot 接口（消息组件、事件、Context、logger、数据目录）
media-resolver/              原插件代码（含本地改动）
```

`media-resolver` 目录名含连字符，入口用 `importlib.import_module(".media-resolver.main", __name__)` 加载；插件内部全是相对导入。插件注册名仍为 `astrbot_plugin_media_parser`（决定数据目录 `plugin_data/astrbot_plugin_media_parser/`），改名时不要动它，否则缓存、解析记录和 B站登录凭证会换目录。

## 部署

1. 放到 NoneBot 项目的插件目录，例如 `plugins/media_parser/`。
2. 依赖：`nonebot2`、`nonebot-adapter-onebot`、以及 `media-resolver/requirements.txt`（aiohttp、cryptography、qrcode、pillow）；系统需要 `ffmpeg`。
3. NoneBot `.env` 中配置：
   - `MEDIA_PARSER_CONFIG`：解析配置 JSON 路径（键名与原插件 `_conf_schema.json` 一致，含各平台 Cookie，**不要提交到仓库**）。
   - `MEDIA_PARSER_DATA_DIR`：缓存目录。媒体以 `file://` 本地路径发给 OneBot 实现，该目录必须对 OneBot 端同路径可见（容器部署时需同路径挂载）。
   - `API_TIMEOUT` 建议调大（如 300），大文件发送耗时较长。
4. 改配置后重启 NoneBot。

## 本地改动（相对原插件 v7.0.1）

代码中以 `[local patch]` 标注。

- **抖音**：详情接口改用开放平台路径 + 浏览器指纹（修复“无法获取视频信息”）。
- **小红书**：登录 Cookie 配置与失效回退；页面改版适配（`__INITIAL_STATE__` 中的 Map/Set、`user/profile` 链接、`EF4~EF7` 视频流选 H.264）；图集去水印地址；视频短边限制 720。
- **X/Twitter**：修复 `avatar_url` 未定义。
- **B站**：`bili2233.cn` 短链；**直播间卡片**（WBI 签名 `getInfoByRoom`，失败回退免签名接口）。
- **抖音直播间卡片**：`webcast.amemv.com`、`live.douyin.com`、`douyin.com/live` 链接，flv 流截取实时画面。
- **Pixiv**：Cookie 失效（401/403）自动改免登录；动图 ugoira 下载帧包后用 ffmpeg 合成 mp4（`ugoira:` 伪链接，`core/downloader/handler/ugoira.py`）；小说链接；简介、统计、发布时间；R-18/R-18G 只发基础文字（不发卡片和图片）。
- **发送**：合并转发节点使用发链接者的昵称/头像；不透明 WebP 转 JPG；卡片最多等 3 秒回执后先发媒体；`retcode=1200` 超时视为已送达；QQ 不给回执（内容被拦截）时提示“图片未通过QQ审核”。
- **卡片样式**：鲸鱼蓝品牌色、淡蓝渐变背景、统计项改线性图标、页脚署名头像（`core/rika_render/assets/whale-avatar.png`）。
- 水印与回退昵称改为“鲸鱼娘”。

## 许可

原插件以 **AGPL-3.0** 发布，本仓库沿用（见 `LICENSE`、`media-resolver/LICENSE`）；其中部分组件另有许可，见 `media-resolver/LICENSES/`。卡片渲染器移植自 astrbot_plugin_rika_share（MIT）。
