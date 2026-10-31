"""Pixiv 动图（ugoira）处理器：下载帧 zip，按每帧时长用 ffmpeg 合成 mp4。

[local patch] 解析器把 zip 地址和帧表编码成 `ugoira:<base64>` 伪链接放进
video_urls，下载管理器把它当作必须本地处理的流（同 dash:），由 router 分发到这里。
"""

import asyncio
import base64
import json
import os
import shutil
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from ...constants import Config
from ...logger import logger
from ...storage import cleanup_file, stamp_subdir
from ..budget import resolve_max_bytes
from ..fileio import run_blocking
from ..security import safe_request

UGOIRA_PREFIX = "ugoira:"
# 太短的动图在 QQ 里一闪而过：循环到至少这么长（毫秒），最多循环 MAX_LOOPS 次
MIN_DURATION_MS = 4000
MAX_LOOPS = 8
MAX_FRAMES = 2000


def encode_ugoira_url(zip_url: str, frames: List[Dict[str, Any]]) -> str:
    payload = {
        "zip": zip_url,
        "frames": [[str(f["file"]), int(f.get("delay") or 100)] for f in frames],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return UGOIRA_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")


def decode_ugoira_url(url: str) -> Tuple[str, List[Tuple[str, int]]]:
    raw = base64.urlsafe_b64decode(url[len(UGOIRA_PREFIX):].encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    frames = [(os.path.basename(str(n)), max(int(d), 20)) for n, d in data["frames"]]
    return str(data["zip"]), frames[:MAX_FRAMES]


def _extract_frames(zip_path: str, frame_dir: str, names: List[str]) -> None:
    """只解出帧表里列出的文件，文件名取 basename，防止 zip 路径穿越。"""
    os.makedirs(frame_dir, exist_ok=True)
    wanted = set(names)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = os.path.basename(info.filename)
            if name not in wanted or info.is_dir():
                continue
            with zf.open(info) as src, open(os.path.join(frame_dir, name), "wb") as dst:
                shutil.copyfileobj(src, dst)


def _write_concat(frame_dir: str, frames: List[Tuple[str, int]]) -> str:
    total = sum(d for _, d in frames) or 1
    loops = min(MAX_LOOPS, max(1, -(-MIN_DURATION_MS // total)))
    lines = ["ffconcat version 1.0"]
    for _ in range(loops):
        for name, delay in frames:
            lines.append(f"file '{name}'")
            lines.append(f"duration {delay / 1000:.3f}")
    # concat 分离器会忽略最后一帧的 duration，再补一次最后一帧
    lines.append(f"file '{frames[-1][0]}'")
    path = os.path.join(frame_dir, "frames.ffconcat")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


async def _download_zip(
    session: aiohttp.ClientSession,
    url: str,
    path: str,
    headers: Optional[dict],
    proxy: Optional[str],
    limit: int,
) -> Tuple[Optional[int], Optional[str]]:
    timeout = aiohttp.ClientTimeout(total=Config.VIDEO_DOWNLOAD_TIMEOUT)
    # 与其它下载一致：走公共地址安全连接器，逐跳校验重定向目标
    resp = await safe_request(
        session, "GET", url, headers=headers or {}, proxy=proxy or None, timeout=timeout
    )
    try:
        if resp.status != 200:
            return resp.status, f"动图帧包下载失败 HTTP {resp.status}"
        size = 0
        with open(path, "wb") as f:
            async for chunk in resp.content.iter_chunked(256 * 1024):
                size += len(chunk)
                if size > limit:
                    return resp.status, "动图帧包超过下载大小限制"
                f.write(chunk)
        return resp.status, None
    finally:
        resp.release()


async def download_ugoira_to_cache(
    session: aiohttp.ClientSession,
    media_url: str,
    cache_dir: str,
    media_id: str,
    index: int = 0,
    headers: dict = None,
    proxy: str = None,
    max_bytes: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    try:
        zip_url, frames = decode_ugoira_url(media_url)
    except Exception as e:
        return {"file_path": None, "size_mb": None, "status_code": None,
                "error": f"动图链接无效: {e}"}
    if not frames:
        return {"file_path": None, "size_mb": None, "status_code": None,
                "error": "动图没有帧"}

    limit = resolve_max_bytes(max_bytes, is_video=True)
    cache_subdir = os.path.normpath(os.path.join(cache_dir, media_id))
    os.makedirs(cache_subdir, exist_ok=True)
    stamp_subdir(cache_subdir)
    zip_path = os.path.join(cache_subdir, f"video_{index}_ugoira.zip")
    frame_dir = os.path.join(cache_subdir, f"video_{index}_frames")
    output_path = os.path.normpath(os.path.join(cache_subdir, f"video_{index}.mp4"))
    temp_output = output_path + ".part.mp4"

    status_code = None
    process = None
    try:
        status_code, error = await _download_zip(
            session, zip_url, zip_path, headers, proxy, limit
        )
        if error:
            return {"file_path": None, "size_mb": None, "status_code": status_code,
                    "error": error}
        await run_blocking(_extract_frames, zip_path, frame_dir, [n for n, _ in frames])
        missing = [n for n, _ in frames if not os.path.isfile(os.path.join(frame_dir, n))]
        if missing:
            return {"file_path": None, "size_mb": None, "status_code": status_code,
                    "error": f"动图帧包缺少 {len(missing)} 帧"}
        concat_path = await run_blocking(_write_concat, frame_dir, frames)

        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", concat_path,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            "-fps_mode", "vfr",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-movflags", "+faststart",
            temp_output,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            process.communicate(), timeout=Config.VIDEO_DOWNLOAD_TIMEOUT
        )
        if process.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore").strip() if stderr else ""
            logger.warning(f"动图 ffmpeg 合成失败(退出码 {process.returncode}): {err[:200]}")
            return {"file_path": None, "size_mb": None, "status_code": status_code,
                    "error": "动图合成失败"}
        size = await run_blocking(os.path.getsize, temp_output)
        if size > limit:
            return {"file_path": None, "size_mb": None, "status_code": status_code,
                    "error": "动图合成后超过大小限制"}
        os.replace(temp_output, output_path)
        total_ms = sum(d for _, d in frames)
        logger.info(
            f"[Pixiv] 动图合成完成: {len(frames)} 帧 / {total_ms / 1000:.1f}s → "
            f"{size / 1024 / 1024:.2f}MB"
        )
        return {
            "file_path": output_path,
            "size_mb": size / (1024 * 1024),
            "status_code": status_code,
        }
    except asyncio.TimeoutError:
        logger.warning("动图下载或合成超时")
        return {"file_path": None, "size_mb": None, "status_code": status_code,
                "error": "动图处理超时"}
    except FileNotFoundError:
        logger.warning("ffmpeg 未找到，无法合成动图")
        return {"file_path": None, "size_mb": None, "status_code": status_code,
                "error": "ffmpeg 未找到"}
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
                await process.communicate()
            except Exception:
                pass
        cleanup_file(zip_path)
        cleanup_file(temp_output)
        shutil.rmtree(frame_dir, ignore_errors=True)
