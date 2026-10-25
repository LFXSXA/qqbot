# -*- coding: utf-8 -*-
"""需要查资料时自动联网搜索，再用她自己的方式回答

为什么不用 function calling：实测 AstrBot 里模型不会主动触发 tool，
所以改成「关键词命中 → 后台搜索 → 把结果塞进 system prompt」。

触发：他在问一个需要事实/方法/价格/推荐的问题
注入：搜索结果摘要 + 「用这些回答，给具体建议，别念资料」
"""

import asyncio
import json
import os
import re
import time
import urllib.parse
import urllib.request

from astrbot.api import logger, star
from astrbot.api.event import filter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", ".."))
CONF = os.path.join(DATA, "config", "astrbot_plugin_search_config.json")

DEFAULT_CFG = {"enabled": True, "max_results": 4, "min_len": 6, "quiet_ok": True}

# 需要查资料的信号
# 只在这些「明确在问事实/方法」的情况下搜索。
# 「有没有 / 什么 / 怎么」这类太日常，会把闲聊也搜了（踩过坑）。
QUESTION_WORDS = [
    "怎么办", "该怎么", "如何", "怎么才能", "怎么弄", "怎么选",
    "推荐", "哪个好", "哪个牌子", "多少钱", "价格多少", "值不值",
    "有用吗", "靠谱吗", "攻略", "教程", "步骤", "区别", "对比",
    "查一下", "搜一下", "帮我查", "查查", "怎么解决", "是什么原理",
]
# 明显是聊感情/情绪，不查
SKIP = ["想你", "抱抱", "亲亲", "分手", "喜欢我", "晚安", "在干嘛", "想我"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
CACHE = {}


def _strip(html: str) -> str:
    html = re.sub(r"<[^>]+>", "", html)
    for a, b in [
        ("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&#39;", "'"), ("&nbsp;", " "), ("&ensp;", " "), ("&emsp;", " "),
        ("&#0183;", "·"), ("&middot;", "·"), ("&hellip;", "…"), ("&mdash;", "—"),
        ("&ldquo;", "“"), ("&rdquo;", "”"),
    ]:
        html = html.replace(a, b)
    html = re.sub(r"&#\d+;", "", html)
    return re.sub(r"\s+", " ", html).strip()


def bing_search(q: str, n: int = 4):
    """Bing 网页搜索，取标题+摘要"""
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(q) + "&setlang=zh-CN"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "ignore")

    out = []
    # Bing 结果块
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S)[: n * 2]:
        m = re.search(r"<h2>.*?<a[^>]*>(.*?)</a>", block, re.S)
        title = _strip(m.group(1)) if m else ""
        snip = ""
        for pat in (r'<p[^>]*>(.*?)</p>', r'class="b_caption".*?>(.*?)</div>'):
            mm = re.search(pat, block, re.S)
            if mm:
                snip = _strip(mm.group(1))
                break
        if title:
            out.append({"title": title, "snippet": snip[:160]})
        if len(out) >= n:
            break
    return out


class SearchPlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = dict(DEFAULT_CFG)
        self._load()
        logger.info("[search] 已加载")

    def _load(self):
        try:
            self.cfg.update(json.load(open(CONF, encoding="utf-8-sig")))
        except Exception:
            pass

    def _should_search(self, text: str) -> bool:
        if len(text) < int(self.cfg.get("min_len", 6)):
            return False
        if any(k in text for k in SKIP):
            return False
        # 明显在聊你我的私事，不是查资料
        if any(k in text for k in ("你", "宝", "我们", "宝宝", "抱抱")):
            return False
        return any(k in text for k in QUESTION_WORDS)

    async def _search(self, q: str):
        key = q[:40]
        hit = CACHE.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            res = await asyncio.to_thread(bing_search, q, int(self.cfg.get("max_results", 4)))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[search] 搜索失败: {e}")
            res = []
        CACHE[key] = (time.time(), res)
        if len(CACHE) > 100:
            for k in list(CACHE)[:30]:
                CACHE.pop(k, None)
        return res

    @filter.on_llm_request()
    async def inject(self, event, req) -> None:
        try:
            if event.get_extra("_search_injected"):
                return
            self._load()
            if not self.cfg.get("enabled", True):
                return
            text = (event.message_str or "").strip()
            if not self._should_search(text):
                return

            res = await self._search(text)
            if not res:
                return
            lines = [f"- {r['title']}：{r['snippet']}" for r in res if r.get("title")]
            block = (
                "【这一轮是正事，不是撒娇】\n"
                "他在问你一个实际的问题。你要像个正常人一样给答案。\n\n"
                "【刚查到的资料】（不一定全对，但比你凭空想准）\n"
                + "\n".join(lines)
                + "\n\n【必须遵守】\n"
                "1. 一定要用到上面的资料，说得出具体的东西（型号、方法、价格、步骤）。\n"
                "2. 先说结论/建议，再补一句理由。两到三句，别超过四句。\n"
                "3. 不许说「你自己查查」「不知道诶」这种推掉的话。\n"
                "4. 不许撒娇、不许只说想你抱抱。用「我觉得」「要不」这种日常口气。\n"
                "5. 不要提「我查了一下」「根据资料」，不要列条目。"
            )
            req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + block
            event.set_extra("_search_injected", True)
            logger.info(f"[search] 命中搜索：{text[:24]} → {len(res)} 条")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[search] 注入失败: {e}")
