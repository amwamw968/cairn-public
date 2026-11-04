"""TTS 缓存层：规范化 → 磁盘缓存 → 完整性校验 → 单飞。

设计来自 docs/BUKA-LESSONS.md §9（他人产品的工程件拆解，只学设计不搬代码）：

1. ``canonicalize``：NFC 归一 + 空白折叠 + 拒收控制/私用区字符，让「同一句话」
   算出同一个缓存 key，不因空格差异分裂成两条。
2. ``cache_key``：``sha256("1\\n{voice}\\n{rate}\\n{text}")``——voice/rate 变了
   就是另一条缓存，与合成器实现细节无关。
3. ``SpeechAudioCache``：磁盘缓存，条数/字节双限（按最后访问时间 LRU 淘汰）；
   写入走 ``.tmp`` 原子替换（写一半断电不留半个文件）；读到的文件校验不过
   改名 ``.corrupt`` 隔离并视为未命中。
4. ``is_valid_audio``：长度区间 + WAV（本机 ``say`` 的输出格式）RIFF 块自洽
   校验；其它格式（mp3）只做长度与帧头粗检。
5. ``SpeechRequestCoalescer``：同一 key 并发只合成一次，其它调用方等结果。

关于锁的选择：``tts.synthesize`` 是**同步**函数，在小智链路里经
``asyncio.to_thread`` 跑在线程池里——``asyncio.Lock`` 绑定事件循环、跨线程
使用会直接抛错，所以单飞用 ``threading.Lock`` 字典（同步环境，线程安全）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_CACHE_DIR = "CAIRN_TTS_CACHE_DIR"
ENV_MAX_ENTRIES = "CAIRN_TTS_CACHE_ENTRIES"
ENV_MAX_BYTES = "CAIRN_TTS_CACHE_BYTES"

DEFAULT_CACHE_DIR = "data/tts-cache"
DEFAULT_MAX_ENTRIES = 128
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

MIN_AUDIO_BYTES = 256
MAX_AUDIO_BYTES = 8 * 1024 * 1024

# key 格式版本号：将来换口径时整体失效，不会新旧混存。
_KEY_VERSION = "1"

_WHITESPACE_RE = re.compile(r"\s+")
_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})


def canonicalize(text: str) -> str | None:
    """NFC 归一 → 去首尾空白 → 连续空白合并成一个。

    空串、或含控制字符/私用区字符（Unicode 类别 Cc/Cf/Co/Cs）→ ``None``。
    """
    normalized = unicodedata.normalize("NFC", text).strip()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    if not normalized:
        return None
    for ch in normalized:
        if unicodedata.category(ch) in _REJECTED_CATEGORIES:
            return None
    return normalized


def cache_key(text: str, voice: str = "", rate: str | int = "") -> str:
    """规范化后的文本 + voice/rate → 稳定的缓存 key。"""
    payload = f"{_KEY_VERSION}\n{voice}\n{rate}\n{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_valid_audio(data: bytes) -> bool:
    """音频完整性粗校验：长度 256 B–8 MB；WAV 查 RIFF 块自洽；mp3 查帧头。"""
    if not isinstance(data, (bytes, bytearray)):
        return False
    if not (MIN_AUDIO_BYTES <= len(data) <= MAX_AUDIO_BYTES):
        return False
    data = bytes(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return _valid_wav(data)
    return _valid_mp3(data)


def _valid_wav(data: bytes) -> bool:
    """RIFF/WAVE：总长声明自洽，且存在 ``data`` 块、声明长度与文件自洽。"""
    riff_size = int.from_bytes(data[4:8], "little")
    if riff_size < 4 or 8 + riff_size > len(data):
        return False
    pos = 12  # 跳过 "RIFF<4字节总长>WAVE"
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        if chunk_id == b"data":
            return chunk_size > 0 and pos + 8 + chunk_size <= len(data)
        pos += 8 + chunk_size + (chunk_size & 1)  # 块按 2 字节对齐
    return False


def _valid_mp3(data: bytes) -> bool:
    """mp3 粗检：ID3 头，或前 4096 字节内找到帧同步字（0xFFEx）。"""
    if data[:3] == b"ID3":
        return True
    for i in range(min(len(data) - 1, 4096)):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            return True
    return False


class SpeechRequestCoalescer:
    """单飞：同一 key 的并发请求共用一次合成。

    用法：调用方 ``with coalescer.slot(key):`` 包住「查缓存 → 未命中合成 →
    落盘」整段。后来的调用方在同一把锁上排队，进槽后缓存里已有结果，
    直接命中返回，不再合成。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextmanager
    def slot(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield


class SpeechAudioCache:
    """TTS 音频磁盘缓存：双限 LRU + 原子写 + 损坏隔离。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        max_entries: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.root = Path(
            root if root is not None else os.environ.get(ENV_CACHE_DIR) or DEFAULT_CACHE_DIR
        )
        self.max_entries = (
            max_entries
            if max_entries is not None
            else int(os.environ.get(ENV_MAX_ENTRIES) or DEFAULT_MAX_ENTRIES)
        )
        self.max_bytes = (
            max_bytes
            if max_bytes is not None
            else int(os.environ.get(ENV_MAX_BYTES) or DEFAULT_MAX_BYTES)
        )
        self.coalescer = SpeechRequestCoalescer()

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.wav"

    def get(self, key: str) -> bytes | None:
        """命中返回音频 bytes；未命中或校验不过（隔离为 .corrupt）返回 None。"""
        path = self._path(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("[tts] 缓存读取失败 key=%s：%s", key, exc)
            return None
        if not is_valid_audio(data):
            corrupt = path.with_name(path.name + ".corrupt")
            try:
                os.replace(path, corrupt)
            except OSError:
                pass
            logger.warning("[tts] 缓存损坏 key=%s 已丢弃", key)
            return None
        self._touch(path)
        return data

    def put(self, key: str, data: bytes) -> None:
        """校验通过的音频落盘：先写 .tmp 再原子替换，随后按双限淘汰。"""
        if not is_valid_audio(data):
            logger.warning("[tts] 合成结果校验不过，不落盘 key=%s", key)
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("[tts] 缓存写入失败 key=%s：%s", key, exc)
            return
        self._evict()

    def count(self) -> int:
        """当前缓存条数（healthz 用）。"""
        return len(self._entries())

    # -- 内部 ---------------------------------------------------------------

    def _entries(self) -> list[tuple[Path, os.stat_result]]:
        if not self.root.is_dir():
            return []
        stats: list[tuple[Path, os.stat_result]] = []
        try:
            for path in self.root.iterdir():
                if path.is_file() and path.suffix == ".wav":
                    try:
                        stats.append((path, path.stat()))
                    except OSError:
                        continue
        except OSError:
            return []
        return stats

    def _touch(self, path: Path) -> None:
        """命中即刷新访问时间——LRU 的「最后访问」以这个为准，不赌文件系统。"""
        try:
            os.utime(path, None)
        except OSError:
            pass

    def _evict(self) -> None:
        """条数/字节任一超限，就按最后访问时间从旧到新删。"""
        entries = self._entries()
        if (
            len(entries) <= self.max_entries
            and sum(st.st_size for _, st in entries) <= self.max_bytes
        ):
            return
        entries.sort(key=lambda item: item[1].st_atime_ns)
        excess_entries = len(entries) - self.max_entries
        over_bytes = sum(st.st_size for _, st in entries) - self.max_bytes
        victims: set[Path] = set()
        for path, st in entries:
            if excess_entries <= 0 and over_bytes <= 0:
                break
            victims.add(path)
            excess_entries -= 1
            over_bytes -= st.st_size
        for path in victims:
            try:
                path.unlink()
            except OSError:
                pass


_default_cache: SpeechAudioCache | None = None
_default_cache_lock = threading.Lock()


def default_cache() -> SpeechAudioCache:
    """进程级默认缓存（目录/上限从环境变量读，惰性创建一次）。"""
    global _default_cache
    with _default_cache_lock:
        if _default_cache is None:
            _default_cache = SpeechAudioCache()
        return _default_cache


__all__ = [
    "canonicalize",
    "cache_key",
    "is_valid_audio",
    "SpeechAudioCache",
    "SpeechRequestCoalescer",
    "default_cache",
    "ENV_CACHE_DIR",
    "ENV_MAX_ENTRIES",
    "ENV_MAX_BYTES",
]
