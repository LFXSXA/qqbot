# -*- coding: utf-8 -*-
"""她自己的情绪（v1）

目的：她不是「情绪接收器」，她有自己的心情和精力，会主动说「今天累」「心情不太好」。

设计（零模型开销，纯本地状态机）：
- 心情 mood 0~100，基线 62，按半衰期回稳
- 精力 energy 0~100，按一天的时间曲线走（晚上低、熬夜更低、睡一觉回升）
- 生活事件：小概率触发（被领导说了 / 没睡好 / 吃到好吃的 / 下雨了 …），
  改心情和精力，并在接下来几小时里留在她的「今天」里
- 每次回复：按概率决定「这次要不要顺带提一句自己的状态」
- 她难受的时候不会硬撑，也不会强装开心

命令：/她的状态   /哄她   /惹她
"""

import json
import math
import os
import random
import re
import time
from datetime import datetime

import random

from astrbot.api import logger, star
from astrbot.api.event import filter

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
CONF = os.path.abspath(os.path.join(HERE, "..", "..", "config", "astrbot_plugin_her_state_config.json"))

DEFAULT_CFG = {
    "mention_chance": 0.12,
    "mention_cooldown_min": 90,
    "companion_minutes": 10,
    "event_chance": 0.05,
    "mood_swing": 1.4,
    "decay_hours": 8,
    "energy_floor": 15,
}

BASELINE = 62.0

# 亲昵词 → 随机回应（代码层指定，保证不总是一句）
INTIMATE_RE = re.compile(r"^(亲亲|抱抱|贴贴|么么|mua|亲|抱|要亲亲|要抱抱|亲亲你|抱抱你)+[呀啊哈哦]?$")
INTIMATE_REPLIES = [
    "亲亲", "", "抱抱", "抱抱你", "", "贴贴",
    "嗯嗯", "好哦", "抱一下", "亲一个", "抱抱我", "亲亲宝",
]

# 生活事件池：(文本, 心情变化, 精力变化, 权重, 时段限制)
SLEEP_WORDS = ["晚安", "睡了", "睡觉", "去睡", "先睡", "睡啦", "睡了哦", "困了先睡", "早点睡"]

EVENTS = [
    ("我今天开会开到头疼", -10, -12, 3, None),
    ("我今天被领导说了一顿", -14, -10, 2, (9, 19)),
    ("我今天加班到现在", -8, -18, 3, (17, 24)),
    ("我早上没睡好", -6, -14, 3, (0, 12)),
    ("我午饭吃到好吃的了", +7, +6, 3, (11, 15)),
    ("我下雨没带伞", -7, -6, 2, None),
    ("我路上堵了半天", -5, -6, 2, (7, 10)),
    ("我今天特别困", -4, -12, 3, None),
    ("我跟朋友聊了会儿，心情好点了", +8, +2, 2, None),
    ("我买了个小东西，挺开心", +6, +2, 2, None),
    ("我肚子有点不舒服", -6, -8, 2, None),
    ("我今天莫名有点丧", -8, -4, 3, (18, 24)),
    ("我想到你了，突然开心", +9, +3, 2, None),
    ("我同事请了奶茶", +5, +6, 2, (9, 18)),
    ("我今天被夸了", +7, +3, 2, None),
]

# 他说话的影响（轻量关键词，不调模型）
NEG_INPUT = ["凶", "烦", "别说", "闭嘴", "滚", "讨厌你", "不想理"]
POS_INPUT = ["想你", "喜欢", "宝", "乖", "抱", "亲", "爱你", "辛苦"]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


class HerStatePlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = {**DEFAULT_CFG, **load_json(CONF, {})}
        self.st = load_json(STATE, {})
        self.st.setdefault("mood", BASELINE)
        self.st.setdefault("energy", 70.0)
        self.st.setdefault("ts", time.time())
        self.st.setdefault("events", [])
        self.st.setdefault("last_nudge", 0.0)
        logger.info(
            "[her_state] 已加载：心情 %.0f / 精力 %.0f，事件 %d 条",
            self.st["mood"],
            self.st["energy"],
            len(self.st["events"]),
        )

    # ---------------- 动力 ----------------
    def _tick(self):
        """按时间推进：心情回稳、精力按一天曲线走"""
        now = time.time()
        dt_h = max(0.0, (now - float(self.st["ts"])) / 3600.0)
        if dt_h > 0.02:
            half = max(1.0, float(self.cfg["decay_hours"]))
            k = 0.5 ** (dt_h / half)
            self.st["mood"] = BASELINE + (self.st["mood"] - BASELINE) * k

        # 精力：按小时的目标曲线，向它靠拢
        h = datetime.now().hour
        target = {
            0: 28, 1: 22, 2: 18, 3: 18, 4: 20, 5: 25, 6: 35,
            7: 55, 8: 68, 9: 75, 10: 78, 11: 72, 12: 58,
            13: 52, 14: 62, 15: 70, 16: 68, 17: 60, 18: 52,
            19: 58, 20: 55, 21: 48, 22: 40, 23: 33,
        }.get(h, 60)
        floor = float(self.cfg["energy_floor"])
        target = max(floor, target)
        # 每小时靠近目标 35%
        self.st["energy"] += (target - self.st["energy"]) * min(1.0, 0.35 * max(dt_h, 0.1))
        self.st["energy"] = max(floor, min(100.0, self.st["energy"]))

        # 清理过期事件（12 小时）
        self.st["events"] = [
            e for e in self.st["events"] if now - float(e.get("t", 0)) < 6 * 3600
        ]
        self.st["ts"] = now
        save_json(STATE, self.st)

    def _add_event(self):
        now = time.time()
        hour = datetime.now().hour
        pool = [
            e for e in EVENTS
            if e[4] is None or (e[4][0] <= hour < e[4][1])
        ] or EVENTS
        texts = [e[0] for e in pool]
        weights = [e[3] for e in pool]
        ev = random.choices(pool, weights=weights, k=1)[0]
        swing = float(self.cfg["mood_swing"])
        self.st["mood"] = max(
            0.0, min(100.0, self.st["mood"] + ev[1] * swing * 0.5)
        )
        self.st["energy"] = max(
            float(self.cfg["energy_floor"]),
            min(100.0, self.st["energy"] + ev[2] * 0.5),
        )
        self.st["events"].append(
            {"t": now, "text": ev[0], "mood": ev[1], "energy": ev[2]}
        )
        self.st["events"] = self.st["events"][-3:]
        logger.info(f"[her_state] 生活事件：{ev[0]}（心情 {self.st['mood']:.0f} / 精力 {self.st['energy']:.0f}）")
        _ = texts

    def _react(self, text: str):
        """根据他这句话，微调她的情绪（10 分钟冷却，避免刷）"""
        now = time.time()
        if now - float(self.st.get("last_nudge", 0)) < 600:
            return
        d = 0.0
        if any(k in text for k in NEG_INPUT):
            d = -6 * float(self.cfg["mood_swing"]) * 0.5
        elif any(k in text for k in POS_INPUT):
            d = +4 * float(self.cfg["mood_swing"]) * 0.5
        if d:
            self.st["mood"] = max(0.0, min(100.0, self.st["mood"] + d))
            self.st["last_nudge"] = now

    # ---------------- 文案 ----------------
    def _mood_word(self, m: float) -> str:
        if m >= 82:
            return "心情特别好"
        if m >= 70:
            return "心情不错"
        if m >= 58:
            return "心情还行"
        if m >= 45:
            return "心情有点低"
        if m >= 32:
            return "心情挺差的"
        return "心情很差"

    def _energy_word(self, e: float) -> str:
        if e >= 78:
            return "精神很好"
        if e >= 62:
            return "还行，有点困但撑得住"
        if e >= 45:
            return "有点累"
        if e >= 30:
            return "挺累的"
        return "累到不想动"

    def _companion_block(self) -> str:
        """他在要陪伴时，别一句话就停"""
        until = float(self.st.get("companion_until", 0) or 0)
        if until <= time.time():
            return ""
        return (
            "\n\n【他现在是想让你陪】\n"
            "- 这次不要一句话就结束，说两三句，多问一句（「今天怎么了」「想聊点什么」）。\n"
            "- 如果他说孤单但没说原因，就问一句「怎么了」，别自己猜。\n"
            "- 不许说困、不许说去睡、不许把话题转到别的地方。\n"
            "- 语气还是你的语气，别突然变得像开导。"
        )

    def _state_line(self, mention: bool) -> str:
        m, e = self.st["mood"], self.st["energy"]
        now = datetime.now().strftime("%H:%M")
        parts = [
            f"【你现在的状态】{now}，{self._mood_word(m)}（{m:.0f}/100），{self._energy_word(e)}（精力 {e:.0f}/100）。"
        ]
        if self.st["events"]:
            ev = "；".join(e["text"] for e in self.st["events"][-2:])
            parts.append(f"今天发生在【你自己】身上的事：{ev}。")
        if mention:
            parts.append(
                "这一次可以顺带说一句你自己的状态（比如今天累、遇到什么事），"
                "像真人随口提一句，不用展开。同一件事只说一次，别重复。"
            )
        else:
            parts.append("这次不必主动说自己的状态，除非他问。")
        parts.append(
            "记住：上面写的全部是【你自己】的状态和经历，跟他无关。"
            "说的时候主语必须是「我」；不要说成「你困」「你累」。"
            "不要每句都提累或困，也不要反复说同一件事。"
        )
        return "".join(parts)

    # ---------------- 钩子 ----------------
    @filter.on_llm_request()
    async def inject(self, event, req) -> None:
        try:
            if event.get_extra("_her_state_injected"):
                return
            self.cfg = {**DEFAULT_CFG, **load_json(CONF, {})}
            self._tick()
            text = (event.message_str or "").strip()
            if text:
                self._react(text)
                if random.random() < float(self.cfg["event_chance"]):
                    self._add_event()
            # 亲昵词：代码随机指定这次的说法，避免每次都一样
            intimate_hint = ""
            if INTIMATE_RE.match(text):
                pool = [v for v in INTIMATE_REPLIES if v != self.st.get("last_intimate")]
                pick = random.choice(pool or INTIMATE_REPLIES)
                self.st["last_intimate"] = pick
                intimate_hint = (
                    f"\n\n【这次怎么回】他在跟你撒娇/求亲昵。"
                    f"这次就用「{pick}」这种说法回他，不要换成别的词，也别加长。"
                )
                logger.info(f"[her_state] 亲昵回应 → {pick}")

            # 提状态要有冷却，且同一条事件只说一次
            import time as _t

            now_ts = _t.time()
            last_m = float(self.st.get("last_mention", 0))
            cool_s = float(self.cfg.get("mention_cooldown_min", 90)) * 60
            fresh_ev = [e for e in self.st.get("events", []) if not e.get("mentioned")]
            company = any(k in text for k in ("陪", "别走", "孤单", "一个人", "没人说"))
            if company:
                self.st["companion_until"] = now_ts + float(
                    self.cfg.get("companion_minutes", 10)
                ) * 60
                save_json(STATE, self.st)
                logger.info("[her_state] 进入陪伴模式")
            mention = (
                not company
                and random.random() < float(self.cfg["mention_chance"])
                and now_ts - last_m > cool_s
                and bool(fresh_ev)
            )
            if mention:
                self.st["last_mention"] = now_ts
                for e in fresh_ev[:1]:
                    e["mentioned"] = True
                save_json(STATE, self.st)
            # 状态/陪伴块交给 context 聚合器统一注入，这里只存起来
            self.st["_state_line"] = self._state_line(mention)
            self.st["_companion_block"] = self._companion_block()
            if intimate_hint:
                req.system_prompt = (
                    (getattr(req, "system_prompt", "") or "") + intimate_hint
                )
            event.set_extra("_her_state_injected", True)
            save_json(STATE, self.st)
            logger.info(
                "[her_state] 注入：心情 %.0f / 精力 %.0f / 主动提=%s",
                self.st["mood"], self.st["energy"], mention,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[her_state] 注入失败: {e}")

    # ---------------- 命令 ----------------

    # 她说晚安/睡了 → 进入睡眠，睡到明早 7 点
    def _next_wake(self) -> float:
        # 固定睡 6 小时
        return time.time() + 6 * 3600

    @filter.on_decorating_result()
    async def _mark_sleep(self, event) -> None:
        try:
            result = event.get_result()
            if not result or not getattr(result, "chain", None):
                return
            body = "".join(
                getattr(c, "text", "") or "" for c in result.chain
                if type(c).__name__ == "Plain"
            )
            if any(w in body for w in SLEEP_WORDS):
                self.st["asleep_until"] = self._next_wake()
                self.st["last_mention"] = time.time()
                # 睡了就不留「困」的事件，免得早上还接着抱怨
                self.st["events"] = [e for e in self.st.get("events", []) if "困" not in e.get("text", "")]
                save_json(STATE, self.st)
                logger.info(f"[her_state] 她说晚安，进入睡眠，睡到明早 7 点")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[her_state] 睡眠检测失败: {e}")


    @filter.command("她的状态")
    async def show(self, event):
        self._tick()
        m, e = self.st["mood"], self.st["energy"]
        bar = lambda v: "█" * int(v / 10) + "░" * (10 - int(v / 10))  # noqa: E731
        ev = "\n".join(f"  · {x['text']}" for x in self.st["events"]) or "  （今天还没有特别的事）"
        yield event.plain_result(
            f"心情 {bar(m)} {m:.0f}/100（{self._mood_word(m)}）\n"
            f"精力 {bar(e)} {e:.0f}/100（{self._energy_word(e)}）\n"
            f"今天：\n{ev}"
        )

    @filter.command("哄她")
    async def cheer(self, event):
        self._tick()
        gain = 12 * float(self.cfg["mood_swing"])
        self.st["mood"] = min(100.0, self.st["mood"] + gain)
        self.st["energy"] = min(100.0, self.st["energy"] + 5)
        save_json(STATE, self.st)
        yield event.plain_result(f"哄了一下，心情 {self.st['mood']:.0f} 了")

    @filter.command("惹她")
    async def annoy(self, event):
        self._tick()
        loss = 12 * float(self.cfg["mood_swing"])
        self.st["mood"] = max(0.0, self.st["mood"] - loss)
        save_json(STATE, self.st)
        yield event.plain_result(f"惹了一下，心情 {self.st['mood']:.0f} 了")
