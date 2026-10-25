# -*- coding: utf-8 -*-
"""长期记忆（v1）

目的：他说过的事她真的记得，才能「耐心提醒」而不是「骂他忘」。

- 每 N 轮，用一次很小的模型调用，从这轮对话里抽出「值得长期记住的事实」
  （承诺、待办、偏好、重要事件），存进 facts.json
- 每次请求前，挑和当前话题相关的几条注入 system_prompt
- 去重：文本相似度 > 0.85 就跳过；超过上限淘汰最久没用的

命令：/记忆  /忘掉 <编号>  /记住 <内容>
"""

import asyncio
import json
import os
import re
import time
from difflib import SequenceMatcher

from astrbot.api import logger, star
from astrbot.api.event import filter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", ".."))
FACTS = os.path.join(HERE, "facts.json")
CONF = os.path.join(DATA, "config", "astrbot_plugin_memory_config.json")

DEFAULT_CFG = {"extract_every": 3, "max_facts": 120, "inject_count": 6, "enabled": True}

EXTRACT_PROMPT = """读这段对话，挑出值得长期记住的事实，最多 3 条。既记他的，也记她自己的。

只记：承诺、待办、约定、重要日期、明确偏好（吃/不爱/害怕）、正在做的事、重要事件。
不记：闲聊、一次性情绪、夸赞、寒暄。

只输出 JSON 数组，每条是一个对象 {"who":"他"或"她", "text":"第三人称短句，不超过 20 字"}。
他的例子：{"who":"他","text":"他周五前要交论文"}
她的例子：{"who":"她","text":"她最近在准备考试"}
没有就输出 []。

对话：
他：{user}
她：{her}"""


def load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[memory] 保存失败: {e}")


class MemoryPlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = {**DEFAULT_CFG, **load_json(CONF, {})}
        self.facts = load_json(FACTS, [])
        self._turns = 0
        self._busy = False
        logger.info(f"[memory] 已加载，现有记忆 {len(self.facts)} 条")

    # ---------------- 存取 ----------------
    def _similar(self, a: str, b: str) -> bool:
        return SequenceMatcher(None, a, b).ratio() > 0.85

    def _add(self, text: str, who: str = "他") -> bool:
        text = (text or "").strip()
        if not (2 <= len(text) <= 40):
            return False
        for f in self.facts:
            if self._similar(f["text"], text):
                f["used"] = f.get("used", 0) + 1
                return False
        self.facts.append(
            {"who": who, "text": text, "t": time.time(), "used": 0}
        )
        limit = int(self.cfg["max_facts"])
        if len(self.facts) > limit:
            self.facts.sort(key=lambda x: (x.get("used", 0), x.get("t", 0)), reverse=True)
            self.facts = self.facts[:limit]
        save_json(FACTS, self.facts)
        logger.info(f"[memory] 记住（{who}）：{text}")
        return True

    def _pick_for(self, text: str, n: int):
        if not self.facts or n <= 0:
            return []
        scored = []
        for i, f in enumerate(self.facts):
            score = 0
            for w in re.findall(r"[\u4e00-\u9fa5]{2,4}", text or ""):
                if w in f["text"]:
                    score += 2
            age_days = (time.time() - f.get("t", 0)) / 86400
            score += max(0, 2 - age_days * 0.1)
            scored.append((score, i, f))
        scored.sort(key=lambda x: (-x[0], -x[2].get("t", 0)))
        picked = [x[2] for x in scored[:n]]
        for f in picked:
            f["used"] = f.get("used", 0) + 1
        save_json(FACTS, self.facts)
        return picked

    # ---------------- 钩子 ----------------
    @filter.on_llm_request()
    async def inject(self, event, req) -> None:
        # 注入已交给 context 聚合器，这里只负责提取记忆（on_decorating_result）
        return

    @filter.on_decorating_result()
    async def after(self, event) -> None:
        """在消息发出前捕获这轮对话，每 N 轮提取一次记忆。

        用 on_decorating_result 而不是 on_llm_response：实测后者在
        星型 pipeline 下不触发（表情包用这个钩子一直正常）。
        """
        try:
            result = event.get_result()
            her = ""
            if result and getattr(result, "chain", None):
                parts = [
                    getattr(c, "text", "") or ""
                    for c in result.chain
                    if type(c).__name__ == "Plain"
                ]
                her = "".join(parts).strip()
            user = (event.message_str or "").strip()
            if len(user) < 4 or not her:
                return

            self._turns += 1
            every = max(1, int(self.cfg.get("extract_every", 3)))
            logger.info(f"[memory] 第 {self._turns} 轮（每 {every} 轮提取）")
            if self._turns % every != 0 or self._busy:
                return
            self._busy = True
            asyncio.get_event_loop().create_task(self._extract(user, her))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 调度失败: {e}")

    async def _extract(self, user: str, her: str):
        try:
            prov = self.context.get_using_provider()
            if not prov:
                return
            prompt = EXTRACT_PROMPT.replace("{user}", user[:200]).replace("{her}", her[:200])
            r = await prov.text_chat(prompt=prompt, contexts=[])
            raw = (getattr(r, "completion_text", "") or "").strip()
            m = re.search(r"\[.*\]", raw, re.S)
            if not m:
                return
            try:
                items = json.loads(m.group(0))
            except Exception:
                return
            for it in items[:3]:
                if isinstance(it, str):
                    self._add(it, "他")
                elif isinstance(it, dict):
                    self._add(str(it.get("text", "")), str(it.get("who", "他")))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory] 提取失败: {e}")
        finally:
            self._busy = False

    # ---------------- 命令 ----------------
    @filter.command("记忆")
    async def cmd_list(self, event):
        if not self.facts:
            yield event.plain_result("我还不记得什么呢")
            return
        lines = []
        for i, f in enumerate(self.facts[-12:], 1):
            days = int((time.time() - f.get("t", 0)) / 86400)
            lines.append(f"{i}. {f['text']}（{days} 天前）")
        yield event.plain_result(f"我记着的（共 {len(self.facts)} 条）：\n" + "\n".join(lines))

    @filter.command("记住")
    async def cmd_remember(self, event, text: str = ""):
        if not text:
            yield event.plain_result("要记什么？比如：记住 我周五要交论文")
            return
        ok = self._add(text)
        yield event.plain_result("好，记住了" if ok else "这个我好像已经记得了")

    @filter.command("忘掉")
    async def cmd_forget(self, event, idx: str = ""):
        if not self.facts:
            yield event.plain_result("本来就没记什么")
            return
        try:
            i = int(idx) - 1
            item = self.facts.pop(i)
        except Exception:
            yield event.plain_result(f"要忘掉第几条？现在有 {len(self.facts)} 条")
            return
        save_json(FACTS, self.facts)
        yield event.plain_result(f"忘了：{item['text']}")
