"""直播间卡片（[local patch] bili live card / douyin live card）。

由 bilibili / douyin 解析器在 metadata["_live_card"] 中给出直播间数据，
main.py 的卡片渲染遇到该字段时改走本模块；直播链接只发这一张卡片。
版式：平台/状态徽章 → 封面 → 实时画面 → 开播信息 → 标题 → 主播 → 数据面板 → 页脚。
竖屏素材（抖音）封面与实时画面并排显示。

_live_card 可选字段（缺省为 B 站样式）：
  platform / platform_name / accent    平台标识、徽章文字、强调色
  status_text                          覆盖状态徽章文字
  sub_text                             覆盖主播名下方的小字
  stats                                [[标签, 数值, 图标], ...]，图标 flame/eye/person/shield/heart
  keyframe_stream                      直播流地址，用 ffmpeg 截一帧作实时画面
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..logger import logger
from .data import ParseResult, Platform
from .downloader import download_first_to_cache
from .render import (
    BRAND_ACCENT,
    PLATFORM_COLORS,
    ShareCardRenderer,
    _L,
    _THEMES,
    _LANCZOS,
    _hex_to_rgb,
    _with_alpha,
    strip_emoji,
    Image,
    ImageDraw,
)

LIVE_STYLE_VERSION = "live-2"

# live_status -> (徽章文字, 徽章颜色)
_STATUS = {
    1: ("直播中", "#FB7299"),
    2: ("轮播中", "#F59E0B"),
    0: ("未开播", "#8C95A9"),
}


def format_live_count(value: Any) -> Optional[str]:
    """2567.0万 / 1.2亿 / 860；None 表示数据缺失。"""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


class LiveCardRenderer(ShareCardRenderer):
    MEDIA_RATIO = 0.86      # 图片宽度占内容区比例
    MEDIA_RADIUS = 20
    STATS_H = 104
    AVATAR = 64

    # ---------- 图标 ----------

    @staticmethod
    def _live_icon(kind: str, size: int, rgb: tuple[int, int, int]) -> "Image.Image":
        """100x100 坐标系的实心小图标，4 倍超采样后缩小。"""
        S = 4 * size
        k = S / 100
        im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        col = (*rgb, 255)
        P = lambda pts: [(x * k, y * k) for x, y in pts]
        if kind == "flame":
            def drop(cx: float, tip: float, cy: float, r: float) -> list:
                # 水滴形：顶端尖、底部半圆，两侧用二次曲线连接
                pts = []
                for i in range(13):  # 右侧：尖端 -> 圆的最右点
                    t = i / 12
                    pts.append(((1 - t) ** 2 * cx + 2 * (1 - t) * t * (cx + r * 0.9) + t * t * (cx + r),
                                (1 - t) ** 2 * tip + 2 * (1 - t) * t * (cy - r * 0.6) + t * t * cy))
                for i in range(1, 24):  # 底部半圆
                    a = i / 24 * math.pi
                    pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
                for i in range(13):  # 左侧：圆的最左点 -> 尖端
                    t = i / 12
                    pts.append(((1 - t) ** 2 * (cx - r) + 2 * (1 - t) * t * (cx - r * 0.9) + t * t * cx,
                                (1 - t) ** 2 * cy + 2 * (1 - t) * t * (cy - r * 0.6) + t * t * tip))
                return pts
            dr.polygon(P(drop(50, 4, 64, 32)), fill=col)
            hole = Image.new("L", (S, S), 0)
            ImageDraw.Draw(hole).polygon(P(drop(50, 50, 78, 13)), fill=255)
            im.paste((0, 0, 0, 0), (0, 0), hole)
        elif kind == "eye":
            dr.ellipse(P([(2, 22), (98, 78)]), fill=col)
            dr.ellipse(P([(30, 30), (70, 70)]), fill=(255, 255, 255, 255))
            dr.ellipse(P([(39, 39), (61, 61)]), fill=col)
        elif kind == "person":
            dr.ellipse(P([(30, 6), (70, 46)]), fill=col)
            dr.pieslice(P([(10, 52), (90, 132)]), 180, 360, fill=col)
        elif kind == "shield":
            dr.polygon(P([(50, 4), (90, 18), (88, 52), (72, 78), (50, 96),
                          (28, 78), (12, 52), (10, 18)]), fill=col)
        elif kind == "heart":
            dr.ellipse(P([(6, 12), (54, 60)]), fill=col)
            dr.ellipse(P([(46, 12), (94, 60)]), fill=col)
            dr.polygon(P([(8, 44), (92, 44), (50, 92)]), fill=col)
        return im.resize((size, size), _LANCZOS)

    @staticmethod
    def _is_portrait(path: Optional[Path]) -> bool:
        if path is None:
            return False
        try:
            with Image.open(path) as im:
                w, h = im.size
            return h > w * 1.05
        except Exception:
            return False

    # ---------- 绘制 ----------

    def _status_pill(self, canvas, x_right: int, y: int, h: int, text: str, color: str) -> None:
        font = self._font(_L.F_CHIP, bold=True)
        w = 16 + 10 + 8 + self._text_width(text, font) + 18
        x = x_right - w
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=h // 2, fill=(*_hex_to_rgb(color), 235)
        )
        canvas.alpha_composite(layer, (x, y))
        draw = ImageDraw.Draw(canvas)
        cy = y + h // 2
        draw.ellipse((x + 16, cy - 5, x + 26, cy + 5), fill=(255, 255, 255, 255))
        self._draw_text(draw, (x + 34, y + (h - self._line_height(font)) // 2),
                        text, _L.F_CHIP, "#FFFFFF", bold=True)

    def _media(self, canvas, theme, accent_rgb, path: Optional[Path], x: int, y: int,
               w: int, h: int) -> None:
        try:
            if path is None:
                raise FileNotFoundError
            img = self._rounded_image(self._cover_fit(self._open_image(path), w, h),
                                      self.MEDIA_RADIUS)
        except Exception:
            img = self._fallback_cover(w, h, theme, accent_rgb, self.MEDIA_RADIUS)
        shadow = Image.new("RGBA", (w + 24, h + 24), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (12, 16, w + 11, h + 11), radius=self.MEDIA_RADIUS, fill=(0, 0, 0, 40)
        )
        from PIL import ImageFilter
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)), (x - 12, y - 12))
        canvas.alpha_composite(img, (x, y))
        border = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(border).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=self.MEDIA_RADIUS,
            outline=_with_alpha(theme.border, 30), width=1,
        )
        canvas.alpha_composite(border, (x, y))

    def render_live(self, live: Dict[str, Any], images: Dict[str, Optional[Path]],
                    url: str, out_path: Path) -> Path:
        theme = _THEMES[self.theme_name]
        platform = live.get("platform") or "bilibili"
        platform_name = live.get("platform_name") or "哔哩哔哩"
        accent = live.get("accent") or PLATFORM_COLORS.get(platform, BRAND_ACCENT)
        accent_rgb = _hex_to_rgb(accent)
        brand_rgb = _hex_to_rgb(BRAND_ACCENT)
        pad = _L.PAD
        inner_w = self.width - pad * 2

        status = int(live.get("live_status") or 0)
        status_text, status_color = _STATUS.get(status, _STATUS[0])
        if status == 1 and live.get("accent"):
            status_color = live["accent"]
        status_text = live.get("status_text") or status_text
        media_w = round(inner_w * self.MEDIA_RATIO)
        cover = images.get("cover")
        keyframe = images.get("keyframe") if status == 1 else None
        medias = [p for p in (cover, keyframe) if p is not None] or [None]
        # 竖屏素材并排（3:4），横屏素材上下排列（16:9）
        side = any(self._is_portrait(p) for p in medias)
        if side:
            gap = 18
            tile_w = (media_w - gap) // 2
            tile_h = round(tile_w * 4 / 3)
            row_w = tile_w * len(medias) + gap * (len(medias) - 1)
            row_x = (self.width - row_w) // 2
            slots = [(row_x + i * (tile_w + gap), 0, tile_w, tile_h) for i in range(len(medias))]
            media_block_h = tile_h
        else:
            media_h = round(media_w * 9 / 16)
            media_x = (self.width - media_w) // 2
            slots = [(media_x, i * (media_h + 18), media_w, media_h) for i in range(len(medias))]
            media_block_h = len(medias) * media_h + (len(medias) - 1) * 18

        meta_parts = []
        if status == 1 and live.get("live_start"):
            meta_parts.append(f"开播 {live['live_start']}")
        elif status != 1:
            meta_parts.append("当前未在直播")
        medal = strip_emoji(live.get("medal_name") or "")
        fansclub = live.get("fansclub")
        if medal:
            club = "粉丝团" if medal == "粉丝团" else f"粉丝团 {medal}"
            meta_parts.append(f"{club} {fansclub}" if fansclub is not None else club)
        meta_text = "  ·  ".join(meta_parts)

        title = strip_emoji(live.get("title") or "") or "直播间"
        title_font = self._font(34, bold=True)
        title_lines = self._fit_lines(title, title_font, inner_w, 2)

        name = strip_emoji(live.get("uname") or "") or "主播"
        if live.get("sub_text") is not None:
            sub_text = strip_emoji(str(live["sub_text"]))
        else:
            sub_parts = [strip_emoji(str(x)) for x in (live.get("parent_area"), live.get("area")) if x]
            if live.get("level"):
                sub_parts.append(f"UL {live['level']}")
            sub_text = " · ".join(p for p in sub_parts if p)

        if live.get("stats") is not None:
            stats = [(str(lab), format_live_count(val), str(icon)) for lab, val, icon in live["stats"]]
        else:
            stats = [
                ("人气", format_live_count(live.get("online")), "flame"),
                ("看过", format_live_count(live.get("watched")), "eye"),
                ("粉丝", format_live_count(live.get("attention")), "person"),
                ("舰队", format_live_count(live.get("guard")), "shield"),
            ]
        stats = [s for s in stats if s[1] is not None]

        # ---- 高度 ----
        y = 28 + _L.HEAD_PILL_H + 26
        y += media_block_h
        if keyframe is not None:
            y += 12 + 26
        y += 26
        if meta_text:
            y += 34
        y += len(title_lines) * 48 + 14
        y += self.AVATAR + 24
        if stats:
            y += self.STATS_H
        y += _L.FOOTER_H
        card_h = y

        canvas, draw = self._base_canvas(theme, brand_rgb, card_h)

        # 顶部徽章
        y = 28
        pw = self._platform_pill_width(platform_name)
        self._draw_flat_badge(
            canvas, theme, pad, y, _L.HEAD_PILL_H, pw,
            text=platform_name, accent_rgb=accent_rgb, dot=True,
            font_size=_L.F_PLATFORM, bold=True, text_rgb=theme.text_primary,
        )
        self._status_pill(canvas, self.width - pad, y, _L.HEAD_PILL_H, status_text, status_color)
        y += _L.HEAD_PILL_H + 26

        # 封面 + 实时画面
        for path, (sx, sy, sw, sh) in zip(medias, slots):
            self._media(canvas, theme, accent_rgb, path, sx, y + sy, sw, sh)
        y += media_block_h
        draw = ImageDraw.Draw(canvas)
        if keyframe is not None:
            cap_font = self._font(18)
            cap = "实时画面"
            kx, _, kw, _ = slots[-1]
            cx = kx + kw // 2 if side else self.width // 2
            self._draw_text(draw, (cx - self._text_width(cap, cap_font) // 2, y + 12),
                            cap, 18, theme.text_tertiary)
            y += 12 + 26
        y += 26

        if meta_text:
            self._draw_text(draw, (pad, y), meta_text, 20, theme.text_tertiary)
            y += 34
        for line in title_lines:
            self._draw_text(draw, (pad, y), line, 34, theme.text_primary, bold=True)
            y += 48
        y += 14

        # 主播
        d = {"name": name}
        self._avatar_block(canvas, draw, pad, y, self.AVATAR, {"avatar": images.get("face")},
                           d, accent_rgb)
        draw = ImageDraw.Draw(canvas)
        nx = pad + self.AVATAR + 18
        name_font = self._font(25, bold=True)
        name_text = self._fit_lines(name, name_font, inner_w - self.AVATAR - 18, 1)[0]
        if sub_text:
            self._draw_text(draw, (nx, y + 4), name_text, 25, theme.text_primary, bold=True)
            self._draw_text(draw, (nx, y + 38), sub_text, 18, theme.text_tertiary)
        else:
            self._draw_text(draw, (nx, y + (self.AVATAR - self._line_height(name_font)) // 2),
                            name_text, 25, theme.text_primary, bold=True)
        y += self.AVATAR + 24

        # 数据面板
        if stats:
            box = (pad, y, pad + inner_w, y + self.STATS_H)
            self._glass(canvas, box, 22, tint_rgb=(255, 255, 255) if self.theme_name == "light"
                        else theme.stat_pill_bg,
                        tint_alpha=150 if self.theme_name == "light" else theme.frost_alpha,
                        border_rgb=theme.border, border_alpha=theme.border_alpha + 6, blur=8)
            draw = ImageDraw.Draw(canvas)
            col_w = inner_w / len(stats)
            val_font = self._font(26, bold=True)
            lab_font = self._font(18)
            for i, (label, value, icon_kind) in enumerate(stats):
                cx = pad + col_w * i + col_w / 2
                icon = self._live_icon(icon_kind, 22, accent_rgb)
                vw = self._text_width(value, val_font)
                group_w = 22 + 8 + vw
                gx = round(cx - group_w / 2)
                vy = y + 18
                canvas.alpha_composite(icon, (gx, vy + (self._line_height(val_font) - 22) // 2))
                draw = ImageDraw.Draw(canvas)
                self._draw_text(draw, (gx + 30, vy), value, 26, theme.text_primary, bold=True)
                lw = self._text_width(label, lab_font)
                self._draw_text(draw, (round(cx - lw / 2), vy + 44), label, 18, theme.text_tertiary)
            y += self.STATS_H

        result = ParseResult(platform=Platform(platform, platform_name), url=url)
        self._footer_block(canvas, ImageDraw.Draw(canvas), theme, BRAND_ACCENT, brand_rgb,
                           result, y, inner_w)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path


async def render_live_card(
    metadata: Dict[str, Any],
    *,
    save_dir: str,
    custom_font: str = "",
    theme: str = "light",
    width: int = 800,
) -> Optional[Path]:
    """渲染直播卡片，成功返回 PNG 路径，失败返回 None（调用方回退普通流程）。"""
    live = metadata.get("_live_card") or {}
    save_path = Path(save_dir)
    downloaded: list[Path] = []
    try:
        save_path.mkdir(parents=True, exist_ok=True)
        font_path = custom_font if custom_font and Path(custom_font).is_file() else None
        renderer = LiveCardRenderer(
            cache_dir=save_path, enabled=True, width=width, theme=theme, font_path=font_path,
        )
        if not renderer.enabled:
            return None

        async def fetch(kind: str, url: Any) -> tuple[str, Optional[Path]]:
            if not url:
                return kind, None
            try:
                path = await download_first_to_cache(
                    [str(url)], cache_dir=save_path, metadata=metadata, kind=kind
                )
                downloaded.append(path)
                return kind, path
            except Exception as e:
                logger.debug(f"直播卡片 {kind} 下载失败: {e}")
                return kind, None

        async def grab(stream_url: str) -> tuple[str, Optional[Path]]:
            """从直播流截一帧（抖音没有现成的关键帧图）。"""
            out = save_path / f"live_frame_{hashlib.md5(stream_url.encode()).hexdigest()[:12]}.jpg"
            downloaded.append(out)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-loglevel", "error", "-rw_timeout", "8000000",
                    "-i", stream_url, "-frames:v", "1", "-q:v", "3", str(out),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=15)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return "keyframe", None
                if out.is_file() and out.stat().st_size > 0:
                    return "keyframe", out
            except Exception as e:
                logger.debug(f"直播卡片截帧失败: {e}")
            return "keyframe", None

        is_live = int(live.get("live_status") or 0) == 1
        if is_live and live.get("keyframe_stream") and not live.get("keyframe"):
            key_task = grab(str(live["keyframe_stream"]))
        else:
            key_task = fetch("keyframe", live.get("keyframe") if is_live else None)
        pairs = await asyncio.gather(
            fetch("cover", live.get("cover")),
            key_task,
            fetch("face", live.get("face")),
        )
        images = dict(pairs)
        url = str(metadata.get("url") or "")
        # 数据按分钟变化，不长期复用缓存
        digest = hashlib.md5(
            f"{LIVE_STYLE_VERSION}|{renderer.theme_name}|{renderer.width}|{url}|"
            f"{int(time.time() // 60)}".encode("utf-8")
        ).hexdigest()[:16]
        out_path = save_path / f"card_live_{digest}.png"
        return await asyncio.to_thread(renderer.render_live, live, images, url, out_path)
    except Exception:
        logger.exception(f"直播卡片渲染失败: {metadata.get('url', '')}")
        return None
    finally:
        for p in downloaded:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
