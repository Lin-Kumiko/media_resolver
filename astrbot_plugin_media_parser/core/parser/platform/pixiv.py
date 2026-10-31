"""
Pixiv 插画/漫画/动图/小说解析器。

负责：
1. 匹配 pixiv.net 作品链接（artworks / i）与小说链接（novel/show.php?id=）
2. 调用 Pixiv Web Ajax API 获取作品元信息
3. 返回图片直链集合供下载管理器处理；动图（ugoira）以 `ugoira:` 伪链接交给下载器合成 mp4

[local patch] 动图、小说、Cookie 失效回退、统计与简介，见 transfers/pixiv/
"""

import asyncio
import html
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp

from .base import BaseVideoParser
from ...constants import Config
from ...downloader.handler.ugoira import encode_ugoira_url
from ...logger import logger
from ...types import MediaMetadata
from ..utils import build_request_headers


# ── URL 正则 ──────────────────────────────────────────────

PIXIV_URL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.:/@-])(?:https?://)?(?:www\.)?pixiv\.net"
    r"/[^\s<>\"'()]+",
    re.IGNORECASE,
)

PIXIV_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.7103.48 Safari/537.36"
)

PIXIV_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja-JP;q=0.6,ja;q=0.5"


def _build_headers(
    illust_id: Optional[str] = None, cookie: str = "", referer_kind: str = "artworks"
) -> Dict[str, str]:
    """构建 Pixiv API 请求头。"""
    headers = {
        "accept-language": PIXIV_ACCEPT_LANGUAGE,
        "user-agent": PIXIV_USER_AGENT,
    }
    if cookie:
        headers["cookie"] = cookie.strip()
    if illust_id and referer_kind == "novel":
        headers["referer"] = f"https://www.pixiv.net/novel/show.php?id={illust_id}"
    elif illust_id:
        headers["referer"] = f"https://www.pixiv.net/artworks/{illust_id}"
    else:
        headers["referer"] = "https://www.pixiv.net/"
    return headers


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    illust_id: str,
    stage: str,
    cookie: str = "",
    proxy: Optional[str] = None,
    referer_kind: str = "artworks",
) -> Dict[str, Any]:
    """请求 Pixiv Ajax API 并验证返回 JSON（而非 HTML）。

    Raises:
        RuntimeError: 被 Cloudflare 拦截 / 返回 HTML / API error
        aiohttp.ClientResponseError: HTTP 状态码错误
    """
    headers = _build_headers(illust_id, cookie, referer_kind)
    proxy_arg = proxy or None

    async with session.get(url, headers=headers, proxy=proxy_arg) as resp:
        content_type = resp.headers.get("content-type", "")
        body_text = await resp.text()
        body_preview = body_text[:300]

        logger.debug(
            f"[Pixiv] {stage} 响应 → pid={illust_id}, "
            f"status={resp.status}, content-type={content_type}"
        )

        is_html = (
            "text/html" in content_type.lower()
            or body_text.lstrip().lower().startswith("<!doctype html")
        )
        if is_html:
            if "Just a moment" in body_text or "just a moment" in body_text:
                raise RuntimeError("Pixiv Ajax 被 Cloudflare 拦截，Cookie 可能已失效")
            raise RuntimeError(f"Pixiv Ajax 返回 HTML，无法解析：{body_preview!r}")

        resp.raise_for_status()

        try:
            data = await resp.json()
        except Exception as e:
            raise RuntimeError(
                f"Pixiv Ajax JSON 解析失败：{e} body={body_preview!r}"
            ) from e

        if not isinstance(data, dict):
            raise RuntimeError(f"Pixiv Ajax 返回的 JSON 不是对象：{stage}")

        if data.get("error"):
            err_msg = data.get("message") or "unknown error"
            raise RuntimeError(f"Pixiv API 返回错误：{err_msg}")

        return data


def _parse_pixiv_identity(url: str) -> Optional[str]:
    """严格解析 Pixiv 作品 URL 并返回作品 ID。"""
    if not isinstance(url, str) or not url.strip():
        return None
    normalized = url.strip()
    if "://" not in normalized:
        normalized = "https://" + normalized
    try:
        parsed = urlparse(normalized)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower().strip(".")
    if host not in {"pixiv.net", "www.pixiv.net"}:
        return None
    match = re.fullmatch(
        r"/(?:en/)?(?:artworks|i)/(\d{5,12})/?",
        parsed.path or "",
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _parse_pixiv_novel_identity(url: str) -> Optional[str]:
    """解析 Pixiv 小说链接（/novel/show.php?id=123）并返回小说 ID。"""
    if not isinstance(url, str) or not url.strip():
        return None
    normalized = url.strip()
    if "://" not in normalized:
        normalized = "https://" + normalized
    try:
        parsed = urlparse(normalized)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            return None
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower().strip(".")
    if host not in {"pixiv.net", "www.pixiv.net"}:
        return None
    if not re.fullmatch(r"/(?:en/)?novel/show\.php", parsed.path or "", re.IGNORECASE):
        return None
    ids = parse_qs(parsed.query).get("id") or []
    novel_id = ids[0] if ids else ""
    return novel_id if re.fullmatch(r"\d{1,12}", novel_id) else None


def _extract_pixiv_links(text: str) -> List[str]:
    """提取 Pixiv 作品/小说链接，按 ID 去重并保留原始链接形态。"""
    links: List[str] = []
    seen: set[str] = set()
    for match in PIXIV_URL_PATTERN.finditer(text or ""):
        link = match.group(0).rstrip(".,!?)]}>\"'，。！？；：）】》」")
        illust_id = _parse_pixiv_identity(link)
        key = f"i:{illust_id}" if illust_id else None
        if not key:
            novel_id = _parse_pixiv_novel_identity(link)
            key = f"n:{novel_id}" if novel_id else None
        if key and key not in seen:
            seen.add(key)
            links.append(link)
    return links


def _strip_html(text: str) -> str:
    """作品简介是 HTML：换行转空格、去标签、反转义。"""
    text = re.sub(r"<br\s*/?>", " ", text or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _clean_novel_text(text: str) -> str:
    """去掉 Pixiv 小说标记：[newpage]、[chapter:x]、[[rb:字 > 读音]]、[[jumpuri:x > url]]、插图等。"""
    text = re.sub(r"\[\[rb:\s*(.*?)\s*>.*?\]\]", r"\1", text or "")
    text = re.sub(r"\[\[jumpuri:\s*(.*?)\s*>.*?\]\]", r"\1", text)
    text = re.sub(r"\[chapter:\s*(.*?)\]", r"\1", text)
    text = re.sub(r"\[(?:newpage|pixivimage:[^\]]*|uploadedimage:[^\]]*|jump:[^\]]*)\]", " ", text)
    text = re.sub(r"[ \t　]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _fmt_count(value: Any) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if n >= 10000:
        return f"{n / 10000:.1f}万".replace(".0万", "万")
    return str(n)


def _local_time(value: Any) -> Any:
    """Pixiv 的 ISO 时间（带时区）转成本地 `YYYY-MM-DD HH:MM`，失败原样返回。"""
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return value


def _stats_line(body: Dict[str, Any]) -> str:
    """浏览/点赞/收藏，映射到卡片统计徽章（📈 浏览、👍 点赞、⭐ 收藏）。"""
    parts = []
    for emoji, key in (("📈", "viewCount"), ("👍", "likeCount"), ("⭐", "bookmarkCount")):
        if body.get(key) not in (None, ""):
            parts.append(f"{emoji} {_fmt_count(body.get(key))}")
    return "  ".join(parts)


class PixivParser(BaseVideoParser):
    """Pixiv 插画/漫画解析器。"""

    def __init__(
        self,
        cookie: str = "",
        proxy: Optional[str] = None,
    ):
        super().__init__("pixiv")
        self.cookie = cookie
        self.proxy = proxy
        self.semaphore = asyncio.Semaphore(Config.PARSER_MAX_CONCURRENT)
        # Cookie 一旦被 Pixiv 拒绝（401/403），本进程内改为免登录请求，避免每次都多打一轮
        self._cookie_rejected = False

    # ── URL 匹配 ──────────────────────────────────────────

    def can_parse(self, url: str) -> bool:
        return (
            _parse_pixiv_identity(url) is not None
            or _parse_pixiv_novel_identity(url) is not None
        )

    def extract_links(self, text: str) -> List[str]:
        return _extract_pixiv_links(text)

    # ── 解析 ──────────────────────────────────────────────

    async def parse(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> Optional[MediaMetadata]:
        async with self.semaphore:
            novel_id = _parse_pixiv_novel_identity(url)
            if novel_id:
                return await self._parse_novel(session, url, novel_id)
            return await self._parse(session, url)

    async def _fetch(
        self,
        session: aiohttp.ClientSession,
        url: str,
        ref_id: str,
        stage: str,
        referer_kind: str = "artworks",
    ) -> Dict[str, Any]:
        """带 Cookie 请求；Cookie 被拒（401/403）时记一次警告并改为免登录重试。"""
        cookie = "" if self._cookie_rejected else self.cookie
        try:
            return await _fetch_json(
                session, url, ref_id, stage, cookie, proxy=self.proxy,
                referer_kind=referer_kind,
            )
        except aiohttp.ClientResponseError as e:
            if not cookie or e.status not in (401, 403):
                raise
            self._cookie_rejected = True
            logger.warning(
                f"[Pixiv] Cookie 被拒绝（HTTP {e.status}），可能已失效，"
                f"改为免登录请求（R-18 等需登录的作品将无法解析）"
            )
            return await _fetch_json(
                session, url, ref_id, stage, "", proxy=self.proxy,
                referer_kind=referer_kind,
            )

    async def _parse(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> Optional[MediaMetadata]:
        t_start = time.time()

        illust_id = _parse_pixiv_identity(url)
        if not illust_id:
            logger.debug(f"[Pixiv] URL 不匹配 pixiv 模式: {url}")
            return None

        logger.info(f"[Pixiv] 开始解析作品 → pid={illust_id}")

        if not self.cookie:
            logger.warning(f"[Pixiv] 未配置 Cookie，可能无法获取原图: pid={illust_id}")

        # 获取作品元信息
        info_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
        info_data = await self._fetch(session, info_url, illust_id, "元信息")
        body = info_data.get("body") or {}
        if not isinstance(body, dict):
            raise RuntimeError("Pixiv 元信息 body 不是对象")
        returned_id = body.get("illustId") or body.get("illust_id")
        if returned_id not in (None, "") and str(returned_id) != illust_id:
            raise RuntimeError("Pixiv API 返回了其他作品的数据")

        # R-18 / R-18G 只发卡片，不取图也不发图（不论 Cookie 是否有效）
        try:
            r18_card_only = int(body.get("xRestrict") or 0) > 0
        except (TypeError, ValueError):
            r18_card_only = False

        # 获取多页图片 URL
        raw_pages: List[Any] = []
        if not r18_card_only:
            pages_url = f"https://www.pixiv.net/ajax/illust/{illust_id}/pages?lang=zh"
            pages_data = await self._fetch(session, pages_url, illust_id, "pages")
            raw_pages = pages_data.get("body") or []
            if not isinstance(raw_pages, list):
                raise RuntimeError("Pixiv pages body 不是列表")
            if not raw_pages:
                raise RuntimeError("Pixiv 作品未包含可下载图片")

        # ── 解析图片列表 ──
        image_urls: List[List[str]] = []
        page_count = len(raw_pages)

        for item in raw_pages:
            if not isinstance(item, dict):
                logger.warning(f"[Pixiv] pid={illust_id} 页面数据不是对象，跳过")
                continue
            urls = item.get("urls") or {}
            if not isinstance(urls, dict):
                logger.warning(f"[Pixiv] pid={illust_id} 页面 URL 数据不是对象，跳过")
                continue
            original = urls.get("original")
            regular = urls.get("regular") or urls.get("small") or original

            if not original and not regular:
                logger.warning(f"[Pixiv] pid={illust_id} 页面缺少图片 URL，跳过")
                continue

            candidates = []
            # 原图优先
            if original:
                candidates.append(original)
            if regular and regular != original:
                candidates.append(regular)
            if candidates:
                image_urls.append(candidates)

        if not image_urls and not r18_card_only:
            raise RuntimeError("Pixiv 作品未包含有效图片 URL")

        # ── 标签处理 ──
        tags_container = body.get("tags") or {}
        tags_raw = (
            tags_container.get("tags") or [] if isinstance(tags_container, dict) else []
        )
        tags: List[str] = []
        for item in tags_raw if isinstance(tags_raw, list) else []:
            if not isinstance(item, dict):
                continue
            tag_name = item.get("tag")
            if not tag_name:
                continue
            if tag_name == "R-18":
                tag_name = "R18"
            translation = item.get("translation") or {}
            if not isinstance(translation, dict):
                translation = {}
            if translation.get("en"):
                tag_name = translation["en"]
            tags.append(str(tag_name).replace(" ", "_"))

        # ── 构建元数据 ──
        title = str(body.get("illustTitle") or body.get("title") or "Untitled")
        user_name = str(body.get("userName") or "Unknown")
        user_id = str(body.get("userId") or "")

        def safe_int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        x_restrict = safe_int(body.get("xRestrict"))
        ai_type = safe_int(body.get("aiType"))
        sanity_level = safe_int(body.get("sl"))

        # 构建描述（作品简介 + 标签 + 限制信息）
        desc_parts = []
        comment = _strip_html(str(body.get("illustComment") or body.get("description") or ""))
        if comment:
            desc_parts.append(comment[:150] + ("…" if len(comment) > 150 else ""))
        if tags:
            desc_parts.append(" | ".join(f"#{t}" for t in tags[:20]))
        restriction_label = ""
        if x_restrict == 1:
            restriction_label = "R-18"
        elif x_restrict == 2:
            restriction_label = "R-18G"
        if ai_type == 2:
            restriction_label = (
                restriction_label + " / " if restriction_label else ""
            ) + "AI生成"
        if restriction_label:
            desc_parts.append(f"[{restriction_label}]")
        desc = "  ".join(desc_parts) if desc_parts else ""

        # 动图（illustType=2）：取帧 zip 与帧时长，交给下载器合成 mp4；首帧作封面
        video_urls: List[List[str]] = []
        video_cover_urls: List[List[str]] = []
        is_ugoira = safe_int(body.get("illustType")) == 2 and not r18_card_only
        if is_ugoira:
            meta = await self._fetch(
                session,
                f"https://www.pixiv.net/ajax/illust/{illust_id}/ugoira_meta",
                illust_id,
                "ugoira_meta",
            )
            ug = meta.get("body") or {}
            frames = [
                f for f in (ug.get("frames") or [])
                if isinstance(f, dict) and f.get("file")
            ]
            zips = [z for z in (ug.get("originalSrc"), ug.get("src")) if z]
            if frames and zips:
                video_urls = [[encode_ugoira_url(z, frames) for z in dict.fromkeys(zips)]]
                video_cover_urls = [image_urls[0]] if image_urls else []
                image_urls = []
            else:
                logger.warning(f"[Pixiv] pid={illust_id} 动图缺少帧信息，按静态图发送")
                is_ugoira = False

        media_headers = build_request_headers(
            is_video=False,
            referer=f"https://www.pixiv.net/artworks/{illust_id}",
            user_agent=PIXIV_USER_AGENT,
        )

        metadata: MediaMetadata = {
            "url": url,
            "source_url": url,
            "title": title,
            "author": user_name,
            "avatar_url": self._extract_avatar_url(body),
            "platform": "pixiv",
            "parser_name": "pixiv",
            "desc": desc,
            "image_urls": image_urls,
            "video_urls": video_urls,
            "image_headers": media_headers,
            "video_headers": dict(media_headers) if video_urls else {},
            "use_image_proxy": bool(self.proxy),
            "use_video_proxy": bool(self.proxy),
            "proxy_url": self.proxy,
            "has_valid_media": bool(image_urls or video_urls),
            "timestamp": _local_time(body.get("createDate") or body.get("uploadDate")),
            "stats_line": _stats_line(body),
        }
        if video_cover_urls:
            metadata["video_cover_urls"] = video_cover_urls
        if r18_card_only:
            # R-18 只发基础文字：不渲染卡片（main.py 的 render_one 认这个键）、不带头像，
            # 简介与标签不发，只留分级标记
            metadata["_skip_card"] = True
            metadata["avatar_url"] = ""
            metadata["desc"] = f"[{restriction_label}]" if restriction_label else "[R-18]"

        # 附加 Pixiv 特有字段
        metadata["pixiv_illust_id"] = illust_id
        metadata["pixiv_user_id"] = user_id
        metadata["pixiv_x_restrict"] = x_restrict
        metadata["pixiv_ai_type"] = ai_type
        metadata["pixiv_sanity_level"] = sanity_level
        metadata["pixiv_page_count"] = page_count

        elapsed = time.time() - t_start
        r18_label = ""
        if x_restrict == 1:
            r18_label = " [R-18]"
        elif x_restrict == 2:
            r18_label = " [R-18G]"
        ai_label = " [AI]" if ai_type == 2 else ""
        kind_label = "动图" if is_ugoira else f"共 {page_count} 页 / {len(image_urls)} 张"
        logger.info(
            f"[Pixiv] 解析完成 → pid={illust_id} "
            f"「{title}」by {user_name} "
            f"{kind_label}{r18_label}{ai_label} "
            f"({elapsed:.2f}s)"
        )

        return metadata

    async def _parse_novel(
        self,
        session: aiohttp.ClientSession,
        url: str,
        novel_id: str,
    ) -> Optional[MediaMetadata]:
        """小说：标题 + 作者 + 正文开头 + 封面图。"""
        t_start = time.time()
        logger.info(f"[Pixiv] 开始解析小说 → id={novel_id}")
        data = await self._fetch(
            session,
            f"https://www.pixiv.net/ajax/novel/{novel_id}",
            novel_id,
            "小说",
            referer_kind="novel",
        )
        body = data.get("body") or {}
        if not isinstance(body, dict):
            raise RuntimeError("Pixiv 小说 body 不是对象")

        title = str(body.get("title") or "Untitled")
        user_name = str(body.get("userName") or "Unknown")
        content = _clean_novel_text(str(body.get("content") or ""))
        excerpt = content[:220].replace("\n", " ")
        if len(content) > 220:
            excerpt += f"……（全文 {len(content)} 字）"
        if not excerpt:
            excerpt = _strip_html(str(body.get("description") or ""))[:220]

        tags_raw = (body.get("tags") or {}).get("tags") or []
        tags = [
            str(t.get("tag")).replace(" ", "_")
            for t in tags_raw
            if isinstance(t, dict) and t.get("tag")
        ]
        desc_parts = [excerpt] if excerpt else []
        if tags:
            desc_parts.append(" | ".join(f"#{t}" for t in tags[:12]))
        try:
            x_restrict = int(body.get("xRestrict") or 0)
        except (TypeError, ValueError):
            x_restrict = 0
        if x_restrict:
            desc_parts.append("[R-18G]" if x_restrict == 2 else "[R-18]")

        cover = str(body.get("coverUrl") or "")
        image_urls = [[cover]] if cover else []
        metadata: MediaMetadata = {
            "url": url,
            "source_url": url,
            "title": title,
            "author": user_name,
            "avatar_url": "",
            "platform": "pixiv",
            "parser_name": "pixiv",
            "desc": "  ".join(desc_parts),
            "image_urls": image_urls,
            "video_urls": [],
            "image_headers": build_request_headers(
                is_video=False,
                referer=f"https://www.pixiv.net/novel/show.php?id={novel_id}",
                user_agent=PIXIV_USER_AGENT,
            ),
            "video_headers": {},
            "use_image_proxy": bool(self.proxy),
            "proxy_url": self.proxy,
            # 无封面时也算有效：小说本身就是文字内容
            "has_valid_media": True,
            "timestamp": _local_time(body.get("createDate") or body.get("uploadDate")),
            "stats_line": _stats_line(body),
        }
        metadata["pixiv_novel_id"] = novel_id
        metadata["pixiv_x_restrict"] = x_restrict
        logger.info(
            f"[Pixiv] 小说解析完成 → id={novel_id} 「{title}」by {user_name} "
            f"{len(content)} 字 ({time.time() - t_start:.2f}s)"
        )
        return metadata
