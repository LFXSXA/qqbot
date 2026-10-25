# -*- coding: utf-8 -*-
"""把「当时的深情片段」接到「容易留下遗憾的场景」上

原理：
- scenes.json 里每条场景有三样东西：
    trigger  他这句话会触发哪个场景
    evidence 她真实说过的话（同一份聊天记录里的另一面）
    avoid    记录里那次她做错的地方（要避开的说法）
- 命中场景时，把这些注入 system_prompt：
    「关于这个情境，你真实说过的是……。上一次你是 XX，这次别这样。」

这不是让她变温柔，是让她**别再推开**。语气仍然是她的。

命令：/场景        看有哪些场景
      /场景 编号    看某条场景的证据和禁忌
"""

import asyncio
import json
import os
import random
import re
import time

from astrbot.api import logger, star
from astrbot.api.event import filter

HERE = os.path.dirname(os.path.abspath(__file__))
SCENES = os.path.join(HERE, "scenes.json")
CONF = os.path.abspath(
    os.path.join(HERE, "..", "..", "config", "astrbot_plugin_reconcile_config.json")
)
DEFAULT_CFG = {"enabled": True, "strength": "normal"}


class ReconcilePlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = dict(DEFAULT_CFG)
        self.scenes = []
        self._load()
        logger.info(f"[reconcile] 已加载 {len(self.scenes)} 个场景")

    def _load(self):
        try:
            self.cfg.update(json.load(open(CONF, encoding="utf-8-sig")))
        except Exception:
            pass
        try:
            data = json.load(open(SCENES, encoding="utf-8"))
            self.scenes = data.get("scenes", [])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reconcile] 读 scenes.json 失败: {e}")

    def _match(self, text: str):
        for sc in self.scenes:
            if any(k in text for k in sc.get("trigger", [])):
                return sc
        return None

    def _block(self, sc: dict) -> str:
        ev = " / ".join(sc.get("evidence", [])[:4])
        parts = [
            f"【你们真实发生过的】这个情境，你当时说过的是：{ev}。",
        ]
        if sc.get("avoid"):
            parts.append(f"【上一次你在这上面做错了】{sc['avoid']}。")
        if sc.get("hint"):
            parts.append(f"【这一次】{sc['hint']}")
        parts.append("语气还是你平时那样，别突然变温柔，别长篇大论。")
        return "\n".join(parts)

    @filter.on_llm_request()
    async def inject(self, event, req) -> None:
        try:
            import time as _t

            if event.get_extra("_reconcile_injected"):
                return
            if not self.cfg.get("enabled", True):
                return
            self._load()
            text = (event.message_str or "").strip()
            if not text:
                return
            # 分手再现留下的「说真话」要求：只用一次
            pend = getattr(self, "_pending_repair", None)
            if pend and pend.get("until", 0) > _t.time():
                self._pending_repair = None
                banned = "、".join(pend.get("banned", [])) or "（无）"
                req.system_prompt = (
                    (getattr(req, "system_prompt", "") or "")
                    + "\n\n【这是他再问一次的那一刻】"
                    + f"\n上一次你的回应是：{pend.get('real','')}。"
                    + f"\n\n【这一次】{pend.get('hint','')}"
                    + f"\n\n硬性要求：不许出现这些词——{banned}。不要解释你在做什么。"
                )
                event.set_extra("_reconcile_injected", True)
                logger.info("[reconcile] 分手再现 修复模式已注入")
                return

            # 正在吵架：优先用吵架守则（不看关键词）
            until = float(getattr(self, "_quarrel_until", 0))
            left = int(getattr(self, "_quarrel_turns", 0))
            if until > _t.time() and left > 0:
                sc_q = next((s for s in self.scenes if s.get("id") == "quarrel"), None)
                if sc_q:
                    self._quarrel_turns = left - 1
                    try:
                        import astrbot.api as _api

                        _api._quarrel_until = _t.time() + 300
                    except Exception:
                        pass
                    if self._quarrel_turns <= 0:
                        self._quarrel_until = 0
                    req.system_prompt = (
                        (getattr(req, "system_prompt", "") or "")
                        + "\n\n"
                        + self._block(sc_q)
                        + "\n\n【吵架守则】不冷战、不翻旧账、不反问「你想怎样」；一次不超过两句；收场由她主动。"
                        + (f"\n\n【你真正在意的是】{getattr(self, '_quarrel_topic', '')}\n"
                           "别停在「你怎么又忘了」这种表面的话上——你气的是那件事背后的东西。"
                           "说的时候要具体，但别一口气全倒出来。"
                           if getattr(self, "_quarrel_topic", "") else "")
                    )
                    event.set_extra("_reconcile_injected", True)
                    logger.info(f"[reconcile] 吵架上下文注入（剩 {self._quarrel_turns} 轮）")
                    return

            sc = self._match(text)
            if not sc:
                return
            req.system_prompt = (
                (getattr(req, "system_prompt", "") or "") + "\n\n" + self._block(sc)
            )
            event.set_extra("_reconcile_injected", True)
            logger.info(f"[reconcile] 命中场景：{sc.get('name')}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reconcile] 注入失败: {e}")


    # ---------------- 场景再现 ----------------
    def _persona(self) -> str:
        import sqlite3

        try:
            db = os.path.abspath(os.path.join(HERE, "..", "..", "data_v4.db"))
            con = sqlite3.connect(db, timeout=10)
            row = con.execute(
                "select system_prompt from personas where persona_id='default'"
            ).fetchone()
            con.close()
            return row[0] if row else ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reconcile] 读人格失败: {e}")
            return ""

    def _dialogs(self):
        import sqlite3

        try:
            db = os.path.abspath(os.path.join(HERE, "..", "..", "data_v4.db"))
            con = sqlite3.connect(db, timeout=10)
            row = con.execute(
                "select begin_dialogs from personas where persona_id='default'"
            ).fetchone()
            con.close()
            items = json.loads(row[0] or "[]") if row else []
            return [
                {"role": "user" if i % 2 == 0 else "assistant", "content": x}
                for i, x in enumerate(items)
            ]
        except Exception:
            return []

    async def _send(self, uid, text: str) -> bool:
        try:
            platform = self.context.get_platform("aiocqhttp")
            if not platform:
                logger.warning("[reconcile] 没有 aiocqhttp 平台")
                return False
            client = platform.get_client()
            await client.api.call_action(
                "send_private_msg",
                user_id=int(uid),
                message=[{"type": "text", "data": {"text": text}}],
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reconcile] 发送失败: {e}")
            return False

    @filter.command("场景再现")
    async def replay(self, event):
        import asyncio

        self._load()
        uid = event.get_sender_id()
        prov = self.context.get_using_provider()
        if not prov:
            yield event.plain_result("没有可用模型")
            return
        persona = self._persona()
        dialogs = self._dialogs()
        await self._send(uid, "下面是用真实记录接出来的六个场景👇")
        await asyncio.sleep(1.2)
        for i, sc in enumerate(self.scenes, 1):
            line = sc.get("demo") or sc["trigger"][0]
            try:
                r = await prov.text_chat(
                    prompt=line,
                    system_prompt=persona + "\n\n" + self._block(sc),
                    contexts=dialogs,
                )
                reply = (getattr(r, "completion_text", "") or "").strip().replace("\n", " ")
            except Exception as e:  # noqa: BLE001
                reply = f"（生成失败 {e}）"
            await self._send(uid, f"{i}. 你说「{line}」\n她：{reply}")
            logger.info(f"[reconcile] 再现场景 {i}: {line} -> {reply}")
            await asyncio.sleep(1.2)


    # ---------------- 吵架再现 ----------------
    @filter.command("吵架再现")
    async def replay_quarrel(self, event, which: str = ""):
        """只发一句：她带着由头质问他。之后 4 轮他自己回，她按吵架守则接。"""
        import time as _t

        self._load()
        uid = event.get_sender_id()
        topics = []
        try:
            data = json.load(open(os.path.join(HERE, "quarrel_topics.json"), encoding="utf-8"))
            topics = [t for t in data.get("topics", []) if t.get("opener")]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reconcile] 读 topics 失败: {e}")

        if topics:
            if which.isdigit() and 0 < int(which) <= len(topics):
                tp = topics[int(which) - 1]
            else:
                tp = random.choices(topics, weights=[t.get("weight", 1) for t in topics], k=1)[0]
            line = tp.get("opener", "")
            self._quarrel_topic = (
                f"{tp.get('core') or tp.get('subject', '')}"
                f"（她当时真实说过的：{tp.get('real', '')}）"
            )
            self._quarrel_pressure = tp.get("pressure", "")
        else:
            line = random.choice(["你怎么又忘了我说的事", "你是不是又忘了"])
            self._quarrel_topic = ""
            self._quarrel_pressure = ""

        self._quarrel_until = _t.time() + 600
        self._quarrel_turns = 4
        try:
            import astrbot.api as _api

            _api._quarrel_until = _t.time() + 900  # 让表情插件也知道在吵架
        except Exception:
            pass

        await self._send(uid, line)
        logger.info(f"[reconcile] 吵架开场(单条): {line} | 由头: {self._quarrel_topic}")
        event.stop_event()


    @filter.command("吵架再现1")
    async def q1(self, event):
        await self.replay_quarrel(event, "1")

    @filter.command("吵架再现2")
    async def q2(self, event):
        await self.replay_quarrel(event, "2")

    @filter.command("吵架再现3")
    async def q3(self, event):
        await self.replay_quarrel(event, "3")

    @filter.command("吵架再现4")
    async def q4(self, event):
        await self.replay_quarrel(event, "4")

    @filter.command("分手再现")
    async def replay_breakup(self, event, which: str = "1"):
        """只发一条：那一次的原话摘要。他下一句，她按「如果当时说真话」回。"""
        import time as _t

        self._load()
        uid = event.get_sender_id()
        try:
            data = json.load(open(os.path.join(HERE, "breakup.json"), encoding="utf-8"))
            scenes = data.get("scenes", [])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[reconcile] 读 breakup.json 失败: {e}")
            yield event.plain_result("读不到分手剧本")
            return

        try:
            sc = scenes[int(which) - 1]
        except Exception:
            yield event.plain_result(f"只有 {len(scenes)} 段，发「分手再现 1」或「分手再现 2」")
            return

        body = " / ".join(t["text"] for t in sc.get("transcript", []) if t["who"] == "她")
        msg = (
            f"【{sc.get('when', '')}】\n"
            f"你：{next((t['text'] for t in sc.get('transcript', []) if t['who'] == '你'), '')}\n"
            f"她：{body}\n\n"
            "—— 你现在再问一次，看她这次怎么回"
        )
        await self._send(uid, msg)

        # 下一句用「如果当时说真话」的要求来回答
        self._pending_repair = {
            "until": _t.time() + 900,
            "hint": sc.get("repair_hint", ""),
            "banned": sc.get("banned", []),
            "real": sc.get("real", ""),
        }
        logger.info(f"[reconcile] 分手再现(单条) 已发 {sc.get('id')}，等待他回应")
        event.stop_event()

    @filter.command("场景")
    async def cmd_list(self, event, idx: str = ""):
        self._load()
        if not self.scenes:
            yield event.plain_result("没有加载到场景")
            return
        if idx:
            try:
                sc = self.scenes[int(idx) - 1]
            except Exception:
                yield event.plain_result(f"没有第 {idx} 个场景")
                return
            yield event.plain_result(
                f"{sc.get('name')}\n"
                f"触发词：{'、'.join(sc.get('trigger', []))}\n"
                f"真实说过：{' / '.join(sc.get('evidence', []))}\n"
                f"上一次：{sc.get('avoid', '-')}\n"
                f"这次：{sc.get('hint', '-')}"
            )
            return
        event.stop_event()
        lines = [f"{i}. {sc.get('name')}" for i, sc in enumerate(self.scenes, 1)]
        yield event.plain_result("场景列表（发「场景 1」看详情）：\n" + "\n".join(lines))
