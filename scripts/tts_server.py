#!/usr/bin/env python3
"""本地 TTS 服务骨架：标准库 http.server，只监听本地回环。

接口（与 BUKA LESSONS §9 的本地缓存层配套，不接任何外部 TTS）：

- ``POST /v1/speech``，body JSON ``{"text": "...", "voice": "可选", "rate": 160}``
  成功 → ``audio/wav``（与合成器一致），响应头 ``X-Cache: hit|miss``；
  文本为空/规范化失败/超 200 字 → 400 + JSON 错误；合成失败 → 500 + JSON 错误。
- ``GET /healthz`` → ``{"ok": true, "cache_entries": N}``

安全边界：只绑 ``127.0.0.1``，不开放外网；请求体上限 64 KB。
用法：``python scripts/tts_server.py [--port 18200]``
"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cairn.voice import tts
from cairn.voice.speech_cache import canonicalize, default_cache

DEFAULT_PORT = 18200
MAX_TEXT_CHARS = 200
MAX_BODY_BYTES = 64 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tts_server")


class TTSServerHandler(BaseHTTPRequestHandler):
    server_version = "CairnTTS/0.1"

    # -- 基础 ----------------------------------------------------------------

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    # -- 路由 ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send_json(200, {"ok": True, "cache_entries": default_cache().count()})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/speech":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "Content-Length 非法。"})
            return
        if not 0 < length <= MAX_BODY_BYTES:
            self._send_json(400, {"error": f"请求体大小须在 1–{MAX_BODY_BYTES} 字节。"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "body 不是合法 JSON。"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "body 必须是 JSON 对象。"})
            return

        text = body.get("text")
        if not isinstance(text, str):
            self._send_json(400, {"error": "text 必须是字符串。"})
            return
        canonical = canonicalize(text)
        if canonical is None:
            self._send_json(400, {"error": "文本为空或含控制字符/私用区字符。"})
            return
        if len(canonical) > MAX_TEXT_CHARS:
            self._send_json(400, {"error": f"文本超过 {MAX_TEXT_CHARS} 字。"})
            return

        voice = body.get("voice")
        if voice is not None and not isinstance(voice, str):
            self._send_json(400, {"error": "voice 必须是字符串。"})
            return
        rate = body.get("rate")
        if rate is None:
            rate = 160
        if not isinstance(rate, int) or isinstance(rate, bool) or not 1 <= rate <= 500:
            self._send_json(400, {"error": "rate 必须是 1–500 的整数。"})
            return

        try:
            # 缓存逻辑全在 tts.synthesize_ex 里（查缓存 → 单飞合成 → 落盘），
            # 这里只负责 HTTP 壳和 hit/miss 头。
            audio, hit = tts.synthesize_ex(canonical, voice=voice, rate=rate)
        except tts.TTSError as exc:
            logger.warning("[tts] 合成失败：%s", exc)
            self._send_json(500, {"error": f"合成失败：{exc}"})
            return
        except Exception as exc:  # 兜底：不许挂住连接
            logger.exception("[tts] 服务内部错误")
            self._send_json(500, {"error": f"服务内部错误：{type(exc).__name__}"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("X-Cache", "hit" if hit else "miss")
        self.end_headers()
        self.wfile.write(audio)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cairn 本地 TTS 服务（仅回环）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"默认 {DEFAULT_PORT}")
    parser.add_argument("--host", default="127.0.0.1", help="只允许本地回环，勿改为 0.0.0.0")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), TTSServerHandler)
    logger.info("Cairn TTS 服务启动：%s:%s（缓存目录见 CAIRN_TTS_CACHE_DIR）", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
