# -*- coding: utf-8 -*-
"""让她说话：把回复转成语音（edge-tts）

依赖：edge-tts（已装到 /AstrBot/data/site-packages，容器重启不丢）

行为：
- 他要是说「语音 / 说话 / 想听你声音」→ 这一条一定发语音
- 其它时候按 auto_chance 概率发（默认 20%）
- 太长的句子不转（默认超过 60 字）
- 命令 /语音 测试用
"""

import asyncio
import hashlib
import json
import os
import sys
import time

from astrbot.api import logger, star
from astrbot.api.event import filter
from astrbot.api.message_components import Plain, Record

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", ".."))
CONF = os.path.join(DATA, "config", "astrbot_plugin_voice_config.json")
TMP = "/tmp/astrbot_voice"
SITE = "/AstrBot/data/site-packages"

DEFAULT_CFG = {
    "enabled": True,
    "auto_chance": 0.2,
    "voice": "zh-CN-XiaoyiNeural",
    "rate": "+8%",
    "max_chars": 60,
    "keep_text": False,
}

# 他要听语音的信号
ASK = ["语音", "说话", "声音", "听你说", "发条语音", "说句话", "念给我", "读给我"]
SKIP = ["[", "http", "https"]

_tts = None


def _load_tts():
    global _tts
    if _tts is not None:
        return _tts
    if SITE not in sys.path:
        sys.path.insert(0, SITE)
    try:
        import edge_tts

        _tts = edge_tts
        return _tts
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[voice] edge_tts 导入失败: {e}")
        return None


class VoicePlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = dict(DEFAULT_CFG)
        self._load_cfg()
        os.makedirs(TMP, exist_ok=True)
        self._cleanup_tmp()
        _load_tts()
        logger.info(
            f"[voice] 已加载 | 音色 {self.cfg['voice']} | 自动 {int(self.cfg['auto_chance']*100)}%"
        )

    def _load_cfg(self):
        try:
            self.cfg.update(json.load(open(CONF, encoding="utf-8-sig")))
        except Exception:
            pass


    def _cleanup_tmp(self):
        """清掉 24 小时前的语音文件，别把磁盘吃满"""
        now = time.time()
        n = 0
        try:
            for f in os.listdir(TMP):
                p = os.path.join(TMP, f)
                if os.path.isfile(p) and now - os.path.getmtime(p) > 86400:
                    os.remove(p)
                    n += 1
        except Exception:
            pass
        if n:
            logger.info(f"[voice] 清理旧语音 {n} 个")

    async def _synth(self, text: str):
        """生成语音，返回文件路径"""
        t = (text or "").strip()
        if not t or len(t) > int(self.cfg.get("max_chars", 60)):
            return None
        et = _load_tts()
        if not et:
            return None
        h = hashlib.md5((t + self.cfg["voice"] + self.cfg["rate"]).encode()).hexdigest()[:12]
        path = os.path.join(TMP, f"{h}.mp3")
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            return path
        try:
            c = et.Communicate(
                text=t,
                voice=self.cfg.get("voice", "zh-CN-XiaoyiNeural"),
                rate=self.cfg.get("rate", "+8%"),
            )
            await c.save(path)
            if os.path.exists(path) and os.path.getsize(path) > 1500:
                logger.info(f"[voice] 生成 {os.path.getsize(path)//1024}KB: {t[:20]}")
                return path
            logger.warning("[voice] 生成的文件太小")
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[voice] 合成失败: {e}")
            return None

    # ---------- 命令：测试 ----------
    @filter.command("语音")
    async def cmd_voice(self, event, text: str = ""):
        self._load_cfg()
        line = (text or "").strip() or "宝，我在呢，你想听我说什么呀"
        p = await self._synth(line)
        if not p:
            yield event.plain_result("语音生成失败了")
            return
        event.stop_event()
        yield event.chain_result([Record.fromFileSystem(p)])

    # ---------- 自动：把文字回复换成语音 ----------
    @filter.on_decorating_result()
    async def decorate(self, event) -> None:
        try:
            self._load_cfg()
            if not self.cfg.get("enabled", True):
                return
            result = event.get_result()
            if not result or not getattr(result, "chain", None):
                return
            texts = [c.text for c in result.chain if isinstance(c, Plain)]
            body = "".join(texts).strip()
            if not body or len(body) > int(self.cfg.get("max_chars", 60)):
                return
            if any(k in body for k in SKIP):
                return

            ask = any(k in (event.message_str or "") for k in ASK)
            if not ask and random_p() >= float(self.cfg.get("auto_chance", 0.2)):
                return

            p = await self._synth(body)
            if not p:
                return
            new_chain = [c for c in result.chain if not isinstance(c, Plain)]
            if self.cfg.get("keep_text", False):
                new_chain.append(Plain(body))
            new_chain.append(Record.fromFileSystem(p))
            result.chain = new_chain
            logger.info(f"[voice] 已转为语音: {body[:24]}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[voice] 转语音失败: {e}")


def random_p() -> float:
    return time.time_ns() % 10000 / 10000.0
