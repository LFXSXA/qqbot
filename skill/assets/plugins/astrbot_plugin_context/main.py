# -*- coding: utf-8 -*-
"""上下文聚合器（收口）

之前 her_state / memory / video 各自往 system_prompt 里塞自己的段落，
塞多了互相打架（困反复说、主语串、正事被情绪带偏）。

现在：
- her_state / memory / video 只把结果写进状态文件，不再直接注入
- 本插件是唯一的注入者，按优先级拼一个干净的块：

    正事（搜索命中）> 陪伴 > 状态+记忆+视频+时间

- 搜索、场景（reconcile）、B站（bili）保持原样——它们是「模式切换」，
  不是「氛围描述」，不与这里冲突。

依赖文件（都由别的插件写好）：
    her_state/state.json 里的 _state_line / _companion_block / asleep_until
    memory/facts.json
    last_video.json
"""

import json
import os
import re
import time
from datetime import datetime

from astrbot.api import logger, star
from astrbot.api.event import filter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", ".."))
STATE = os.path.join(DATA, "plugins", "astrbot_plugin_her_state", "state.json")
FACTS = os.path.join(DATA, "plugins", "astrbot_plugin_memory", "facts.json")
LAST_VIDEO = os.path.join(DATA, "last_video.json")
RECENT = os.path.join(DATA, "plugins", "astrbot_plugin_speak_up", "recent.json")

# 搜索触发词（与 search 插件一致，用于「正事优先」判断）
QUESTION_WORDS = [
    "怎么办", "该怎么", "如何", "怎么才能", "怎么弄", "怎么选",
    "推荐", "哪个好", "哪个牌子", "多少钱", "价格多少", "值不值",
    "有用吗", "靠谱吗", "攻略", "教程", "步骤", "区别", "对比",
    "查一下", "搜一下", "帮我查", "查查", "怎么解决", "是什么原理",
]
VIDEO_KEY = ["视频", "那个", "刚才", "学到", "套话", "你发的", "看了", "这条",
             "链接", "地址", "bv", "BV", "发的", "啥呀", "讲的"]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


class ContextPlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        logger.info("[context] 聚合器已加载")

    # ---------------- 各来源 ----------------
    def _state_block(self):
        st = load_json(STATE, {})
        if not st:
            return ""
        line = st.get("_state_line", "") or ""
        comp = st.get("_companion_block", "") or ""
        asleep = float(st.get("asleep_until", 0) or 0)
        # 陪伴模式优先（只说陪伴）
        if comp and time.time() < float(st.get("companion_until", 0) or 0):
            return comp
        if asleep > time.time():
            return "【你现在在睡觉，还没醒。他要是这时候找你，会吵醒你。】"
        # 兜底：her_state 还没写入 _state_line 时，自己拼一个
        if not line:
            line = self._build_state_line(st)
        return line

    def _build_state_line(self, st):
        m = float(st.get("mood", 62))
        e = float(st.get("energy", 70))
        evs = [x.get("text") for x in st.get("events", [])][-2:]
        mood = ("心情特别好" if m >= 82 else "心情不错" if m >= 70 else "心情还行" if m >= 58
                else "心情有点低" if m >= 45 else "心情挺差的" if m >= 32 else "心情很差")
        energy = ("精神很好" if e >= 78 else "还算有精神" if e >= 62 else "有点累"
                  if e >= 45 else "挺累的" if e >= 30 else "累到不想动")
        s = f"【你现在的状态】{mood}（{m:.0f}/100），{energy}（精力 {e:.0f}/100）。"
        if evs:
            s += "今天发生在【你自己】身上的事：" + "；".join(evs) + "。"
        s += "这是你自己的状态，主语用「我」，不必主动说，除非他问。"
        return s

    def _memory_block(self, text, n=5):
        facts = load_json(FACTS, [])
        if not facts:
            return ""
        scored = []
        for f in facts:
            s = 0
            for w in re.findall(r"[\u4e00-\u9fa5]{2,4}", text or ""):
                if w in f.get("text", ""):
                    s += 2
            age_days = (time.time() - float(f.get("t", 0))) / 86400
            s += max(0, 2 - age_days * 0.1)
            scored.append((s, f))
        scored.sort(key=lambda x: -x[0])
        picked = [f for s, f in scored[:n] if s > 0]
        if not picked:
            return ""
        his, her = [], []
        for f in picked:
            days = (time.time() - float(f.get("t", 0))) / 86400
            when = "今天" if days < 1 else f"{int(days)} 天前"
            txt = f.get("text", "")
            if f.get("who") == "她":
                t2 = txt.replace("她", "你", 1) if txt.startswith("她") else txt
                her.append(f"{t2}（{when}）")
            else:
                his.append(f"{txt}（{when}）")
        lines = []
        if his:
            lines.append("关于他：" + "；".join(his))
        if her:
            lines.append("关于你自己：" + "；".join(her))
        return (
            "【你记得的事】\n" + "\n".join(lines)
            + "\n（他忘了的时候提醒一句，别责怪；别把所有事都堆出来，只挑跟当前话题相关的说。）"
        )

    def _video_block(self, text):
        if not any(k in text for k in VIDEO_KEY):
            return ""
        info = load_json(LAST_VIDEO, None)
        if not info:
            return ""
        if time.time() - float(info.get("ts", 0)) > 45 * 60:
            return ""
        sub = (info.get("sub") or "").strip()
        desc = (info.get("desc") or "").strip()
        said = (info.get("line") or "").strip()
        block = (
            "【你最近一次接触的视频】\n"
            f"链接：https://www.bilibili.com/video/{info.get('bvid','')}\n"
            f"标题：{info.get('title','')}\nUP主：{info.get('up','')}\n"
            + (f"简介：{desc[:300]}\n" if desc else "")
            + (f"字幕节选：{sub[:800]}\n" if sub else "")
            + (f"你当时说的话：{said}\n" if said else "")
            + "你只刷到标题/简介/字幕，没完整看完，别编具体画面；"
            "字幕没拿到就别说具体方法名。"
        )
        return block


    def _recent_block(self):
        """她最近主动跟他说过的话（防止他追问时她否认）"""
        rec = load_json(RECENT, [])
        if not rec:
            return ""
        now = time.time()
        fresh = [r for r in rec if now - float(r.get("t", 0)) < 6 * 3600][-3:]
        if not fresh:
            return ""
        lines = [f"- {r['text']}" for r in fresh]
        return (
            "【你最近主动跟他说过的话】（这些是你自己说的，别否认）\n"
            + "\n".join(lines)
        )

    def _time_block(self):
        now = datetime.now()
        wd = "一二三四五六日"[now.weekday()]
        h = now.hour
        period = ("深夜" if h < 5 else "凌晨" if h < 7 else "早上" if h < 9
                  else "上午" if h < 12 else "中午" if h < 14 else "下午" if h < 18
                  else "傍晚" if h < 20 else "晚上" if h < 23 else "深夜")
        return f"【现在】{now.strftime('%Y-%m-%d %H:%M')}，星期{wd}，{period}。"

    def _schedule_block(self):
        """她最近在忙的事（长期、稳定，随上下文带出）"""
        sc = load_json(
            os.path.join(DATA, "plugins", "astrbot_plugin_her_state", "schedule.json"),
            {},
        )
        lt = sc.get("long_term") or []
        if not lt:
            return ""
        daily = sc.get("daily") or {}
        now = datetime.now()
        h = now.hour
        doing = ""
        for rng, act in daily.items():
            lo, hi = rng.split("-")
            if int(lo) <= h < int(hi):
                doing = act
                break
        line = "【她最近在忙的事】" + "、".join(lt) + "。"
        if doing:
            line += f"现在这个时候她一般在：{doing}。"
        return line

    # ---------------- 注入 ----------------
    @filter.on_llm_request()
    async def compose(self, event, req) -> None:
        try:
            if event.get_extra("_context_done"):
                return
            text = (event.message_str or "").strip()
            if not text:
                return
            # 正事优先：搜索会接管，这里不注入氛围
            if any(k in text for k in QUESTION_WORDS):
                return
            blocks = []
            st = self._state_block()
            if st:
                blocks.append(st)
            rc = self._recent_block()
            if rc:
                blocks.append(rc)
            sc = self._schedule_block()
            if sc:
                blocks.append(sc)
            mem = self._memory_block(text)
            if mem:
                blocks.append(mem)
            vid = self._video_block(text)
            if vid:
                blocks.append(vid)
            blocks.append(self._time_block())

            req.system_prompt = (
                (getattr(req, "system_prompt", "") or "")
                + "\n\n"
                + "\n\n".join(blocks)
            )
            event.set_extra("_context_done", True)
            logger.info("[context] 注入上下文块（%d 段）", len(blocks))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[context] 注入失败: {e}")
