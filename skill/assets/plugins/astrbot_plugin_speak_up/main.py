# -*- coding: utf-8 -*-
"""主动搭话（移植自 Yuuyuko-uu/dsh-speak-up 的骰子模型）

逻辑：
  定时器到点 → 掷一个 0~100 的随机数
    命中线 = 基础命中线(峰时/谷时) × 时机因子 + 没中加成，夹在 0~100
       - 峰时：周一~周五 9:00-12:00、14:00-18:00 → 20
       - 谷时：其余全部时间 → 60
       - 距上次说话 < 20 分钟 → ×0.15（刚聊过，更难开口）
       - 超过 360 分钟没说话 → ×1.5（冷太久，更容易开口）
       - 每没掷中一次 +10，最多 +40
    掷中 → 才调模型生成一句话发出去（掷骰子本身不花 token）
    安静时段（默认 1~8 点）不掷。
"""

import asyncio
import json
import os
import random
import sqlite3
import time
from datetime import datetime

from astrbot.api import logger, star
from astrbot.api.event import filter

DEFAULT_CFG = {
    "enable": True,
    "dry_run": False,
    "target_user": "",
    "interval_min": 30,
    "cooldown_min": 20,
    "hit_peak": 20,
    "hit_off": 60,
    "miss_bonus": 10,
    "miss_bonus_cap": 40,
    "recent_min": 20,
    "recent_factor": 0.15,
    "cold_min": 360,
    "cold_factor": 1.5,
    "bili_chance": 0.45,
    "quiet_start": 1,
    "quiet_end": 8,
    "morning_greet": True,
    "morning_time": "08:00",
}

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
RECENT = os.path.join(HERE, "recent.json")
DB_PATH = os.path.abspath(os.path.join(HERE, "..", "..", "data_v4.db"))


TOPIC_KINDS = [
    ("分享自己", "说一件你今天真实的小事（从你现在的状态、今天发生的事、你最近在忙的事里找），别编一个没发生过的事"),
    ("问他近况", "问他一件具体的事（吃了没、在忙什么、今天怎么样），别只说想他"),
    ("吐槽", "吐槽一件小事（天气、人多、快递、舍友、工作），带着点情绪"),
    ("关心他", "关心他一件具体的事（睡够没、吃饭没、那件事怎么样了），一句就够"),
    ("说个想法", "说一个你自己的想法或观点，短，一两句，可以有判断"),
    ("给个建议", "给他一个具体的建议，别讲道理、别列点"),
    ("说以后", "说一件以后想一起做的事，具体一点"),
    ("刷到东西", "说一件你刚刷到的帖子或段子（不是视频！不要提视频），带一句你的评价"),
]

PROMPT_NUDGE = (
    "（现在他那边很安静，没人在跟你说话。你想主动找他聊一句，"
    "就一句，短一点，像随手发的一条消息。直接输出消息内容，不要加引号或解释。）"
)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[speak_up] 写文件失败 {path}: {e}")


def is_peak(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    h = dt.hour
    return 9 <= h < 12 or 14 <= h < 18


def in_quiet(dt: datetime, start: int, end: int) -> bool:
    if start == end:
        return False
    h = dt.hour
    if start < end:
        return start <= h < end
    return h >= start or h < end


DATA_DIR = os.path.abspath(os.path.join(HERE, "..", ".."))
BILI_COOKIE_PATH = os.path.join(DATA_DIR, "bili_cookies.json")
BILI_HISTORY = os.path.join(DATA_DIR, "bili_history.json")
LAST_VIDEO = os.path.join(DATA_DIR, "last_video.json")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _bili_get(url, cookie="", timeout=15):
    import urllib.request

    h = {
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _login_cookie():
    try:
        with open(BILI_COOKIE_PATH, encoding="utf-8") as f:
            ck = json.load(f).get("cookies") or {}
        return "; ".join(f"{k}={v}" for k, v in ck.items())
    except Exception:
        return ""


def _seen_bvids(days=7):
    try:
        with open(BILI_HISTORY, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        return set()
    cutoff = time.time() - days * 86400
    return {h["bvid"] for h in hist if h.get("ts", 0) >= cutoff and h.get("bvid")}


def _record_history(entry):
    try:
        with open(BILI_HISTORY, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = []
    hist.append(entry)
    try:
        with open(BILI_HISTORY, "w", encoding="utf-8") as f:
            json.dump(hist[-500:], f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[speak_up] 写历史失败: {e}")


def fetch_bili_hot(limit=10):
    """B站热门（popular + 真实 buvid），排除最近 7 天看过的"""
    try:
        spi = _bili_get("https://api.bilibili.com/x/frontend/finger/spi").get("data") or {}
        guest = "buvid3=%s; buvid4=%s; b_nut=1700000000" % (
            spi.get("b_3", ""),
            spi.get("b_4", ""),
        )
        d = _bili_get(
            "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1",
            _login_cookie() or guest,
        )
        items = (d.get("data") or {}).get("list") or []
        seen = _seen_bvids()
        out = []
        for i in items:
            bv = i.get("bvid")
            if not bv or bv in seen:
                continue
            out.append(
                {
                    "title": i.get("title"),
                    "bvid": bv,
                    "up": (i.get("owner") or {}).get("name"),
                    "view": (i.get("stat") or {}).get("view"),
                    "desc": (i.get("desc") or "")[:120],
                }
            )
            if len(out) >= limit:
                break
        logger.info(f"[speak_up] B站热门取到 {len(out)} 条（已排除看过的）")
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[speak_up] 取B站热门失败: {e}")
        return []


def fetch_bili_subtitle(bvid, max_chars=1200):
    """读字幕原文"""
    ck = _login_cookie()
    if not ck:
        return ""
    try:
        v = _bili_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", ck)
        data = v.get("data") or {}
        aid, cid = data.get("aid"), data.get("cid")
        if not aid or not cid:
            return ""
        p = _bili_get(f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}", ck)
        subs = ((p.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if not subs:
            return ""
        u = subs[0].get("subtitle_url") or ""
        if u.startswith("//"):
            u = "https:" + u
        j = _bili_get(u, ck)
        text = " ".join((x.get("content") or "") for x in (j.get("body") or [])).strip()
        return text[:max_chars]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[speak_up] 取字幕失败 {bvid}: {e}")
        return ""




HER_STATE = os.path.abspath(
    os.path.join(HERE, "..", "astrbot_plugin_her_state", "state.json")
)


def her_state_line():
    """读她的情绪状态，拼一句给模型"""
    try:
        with open(HER_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return ""
    m = float(st.get("mood", 62))
    e = float(st.get("energy", 70))
    evs = [x.get("text") for x in st.get("events", [])][-2:]
    mood = "心情特别好" if m >= 82 else "心情不错" if m >= 70 else "心情还行" if m >= 58         else "心情有点低" if m >= 45 else "心情挺差的" if m >= 32 else "心情很差"
    energy = "精神很好" if e >= 78 else "还算有精神" if e >= 62 else "有点累"         if e >= 45 else "挺累的" if e >= 30 else "累到不想动"
    s = f"【你现在的状态】{mood}（{m:.0f}），{energy}（精力 {e:.0f}）。"
    if evs:
        s += "今天发生的事：" + "；".join(evs) + "。"
    s += "这次是你主动找他说话，可以顺带带一句你自己的状态，像真人随口说的那样。"
    return s


class SpeakUpPlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        import astrbot.api as _api
        _api._speak_up_gen = getattr(_api, '_speak_up_gen', 0) + 1
        self._gen = _api._speak_up_gen
        self.cfg = {**DEFAULT_CFG, **load_json(CFG_PATH, {})}
        save_json(CFG_PATH, self.cfg)
        self.state = {"last_spoke": 0, "miss": 0, "history": []}
        self.state.update(load_json(STATE_PATH, {}))
        self._task = None
        logger.info(
            "[speak_up] 已加载 interval=%s min, dry_run=%s, target=%s",
            self.cfg["interval_min"],
            self.cfg["dry_run"],
            self.cfg["target_user"],
        )
        self._task = asyncio.get_event_loop().create_task(self._loop())

    # ---------- 核心循环 ----------
    async def _loop(self):
        while True:
            import astrbot.api as _api2
            cur = getattr(_api2, '_speak_up_gen', 0)
            if self._gen != cur:
                logger.info(f"[speak_up] 旧实例 gen={self._gen} 退出（当前 {cur}）")
                return
            try:
                await asyncio.sleep(max(60, int(self.cfg["interval_min"]) * 60))
                await self._round()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[speak_up] 轮次异常: {e}")

    def _hit_line(self, now_ts: float) -> float:
        cfg = self.cfg
        base = cfg["hit_peak"] if is_peak(datetime.now()) else cfg["hit_off"]
        last = float(self.state.get("last_spoke") or 0)
        factor = 1.0
        if last:
            gap_min = (now_ts - last) / 60
            if gap_min < cfg["recent_min"]:
                factor = cfg["recent_factor"]
            elif gap_min > cfg["cold_min"]:
                factor = cfg["cold_factor"]
        # 刚聊过就不插嘴（这是最关键的闸门）
        last_user = float(self.state.get("last_user_msg") or 0)
        active_gap = float(cfg.get("active_gap_min", 12)) * 60
        if last_user and now_ts - last_user < active_gap:
            return 0.0
        bonus = min(self.state.get("miss", 0) * cfg["miss_bonus"], cfg["miss_bonus_cap"])
        return max(0.0, min(100.0, base * factor + bonus))

    @filter.on_llm_request()
    async def _mark_user(self, event, req) -> None:
        """记下他最后一次说话的时间（用来判断「刚聊完别插嘴」）"""
        self.state["last_user_msg"] = time.time()
        save_json(STATE_PATH, self.state)


    async def _morning_greet(self):
        """每天一次早安；她睡着时跳过；当天发过就跳过"""
        now = datetime.now()
        hh, mm = self.cfg.get("morning_time", "08:00").split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if now < target:
            return
        today = now.strftime("%Y-%m-%d")
        if self.state.get("last_morning") == today:
            return
        try:
            with open(HER_STATE, encoding="utf-8") as _f:
                _st = json.load(_f)
            if float(_st.get("asleep_until", 0) or 0) > time.time():
                return
        except Exception:
            pass
        line = await self._morning_line()
        if not line:
            return
        ok = await self._send(line)
        if ok:
            self.state["last_morning"] = today
            save_json(STATE_PATH, self.state)
            logger.info(f"[speak_up] 早安已发: {line}")

    async def _morning_line(self) -> str:
        prov = self.context.get_using_provider()
        if not prov:
            return "早上好呀"
        try:
            r = await prov.text_chat(
                prompt="（早上刚醒，跟他说一句早安）",
                system_prompt=self._persona_prompt(),
                contexts=[],
            )
            t = (getattr(r, "completion_text", "") or "").strip().replace('"', "").replace("“", "").replace("”", "")
            return t[:40] or "早上好呀"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[speak_up] 早安生成失败: {e}")
            return "早上好呀"


    async def _round(self):
        cfg = self.cfg
        # 早安：与随机搭话解耦，即使 enable=False 也发（每天一次）
        if cfg.get("morning_greet", True):
            try:
                await self._morning_greet()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[speak_up] 早安失败: {e}")
        if not cfg["enable"]:
            return
        now = datetime.now()
        if in_quiet(now, cfg["quiet_start"], cfg["quiet_end"]):
            logger.info("[speak_up] 安静时段，跳过")
            return
        # 她睡着（说过晚安）就不主动搭话
        try:
            with open(HER_STATE, encoding="utf-8") as _f:
                _st = json.load(_f)
            _au = float(_st.get("asleep_until", 0) or 0)
            if _au > time.time():
                logger.info("[speak_up] 她在睡觉，跳过")
                return
        except Exception:
            pass

        now_ts = time.time()
        if now_ts - float(self.state.get("last_spoke") or 0) < cfg["cooldown_min"] * 60:
            logger.info("[speak_up] 冷却中，跳过")
            return

        line = self._hit_line(now_ts)
        roll = random.uniform(0, 100)
        hit = roll < line
        logger.info(f"[speak_up] 掷骰 {roll:.1f} vs 命中线 {line:.1f} → {'中' if hit else '没中'}")

        self.state.setdefault("history", []).append(
            {"t": now.strftime("%m-%d %H:%M"), "roll": round(roll, 1), "line": round(line, 1), "hit": hit}
        )
        self.state["history"] = self.state["history"][-50:]

        if not hit:
            self.state["miss"] = int(self.state.get("miss", 0)) + 1
            save_json(STATE_PATH, self.state)
            return

        self.state["miss"] = 0
        text = await self._generate()
        vid = getattr(self, "_last_video", None)
        if not text:
            save_json(STATE_PATH, self.state)
            return
        # 没真视频却说「刷到一个…视频」→ 改成不说视频，避免她声称有链接却没有
        if not vid and "视频" in text:
            text = text.replace("刷到一个", "看到一条").replace("视频", "帖子")
            logger.info(f"[speak_up] 无真链接，已改写: {text}")

        if cfg["dry_run"]:
            logger.info(f"[speak_up] 试运行，不发送: {text}")
        else:
            ok = await self._send(text)
            if ok:
                self.state["last_spoke"] = now_ts
                logger.info(f"[speak_up] 已主动搭话: {text}")
                # 记下她说过的话，避免他追问时她否认
                try:
                    rec = load_json(RECENT, [])
                    rec.append({"t": time.time(), "text": text})
                    save_json(RECENT, rec[-6:])
                except Exception:
                    pass
                if vid:
                    # 把视频链接发给他
                    link = f"https://www.bilibili.com/video/{vid['bvid']}"
                    await asyncio.sleep(1.5)
                    await self._send(link)
                    logger.info(f"[speak_up] 已发链接: {link}")
                if vid:
                    _record_history(
                        {
                            "ts": time.time(),
                            "bvid": vid["bvid"],
                            "title": vid["title"],
                            "up": vid["up"],
                            "decision": "发给你",
                            "line": text,
                        }
                    )
        save_json(STATE_PATH, self.state)

    # ---------- 生成 & 发送 ----------
    def _persona_prompt(self) -> str:
        try:
            con = sqlite3.connect(DB_PATH, timeout=10)
            row = con.execute(
                "select system_prompt from personas where persona_id='default'"
            ).fetchone()
            con.close()
            return row[0] if row else ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[speak_up] 读人格失败: {e}")
            return ""

    async def _generate(self) -> str:
        prov = self.context.get_using_provider()
        if not prov:
            logger.warning("[speak_up] 没有可用模型")
            return ""

        # 有概率这次是「她自己刷到一条B站视频」再开口
        prompt = "（主动开口）"
        extra = PROMPT_NUDGE
        kind, kind_hint = random.choice(TOPIC_KINDS)
        extra = (
            "（现在他那边很安静，你想主动找他聊一句。"
            f"这次要聊的是：{kind}——{kind_hint}。"
            "就一句，短，像随手发的一条。"
            "不要用「好困」「好累」「想睡」当话题，那是终止对话的话。"
            + (
                "这次是日常话题，不要出现「想你」「抱抱」「亲亲」「宝」这类词。"
                if kind not in ("说以后",)
                else ""
            )
            + "直接输出消息内容，不要加引号或解释。）"
        )
        logger.info(f"[speak_up] 开口类型: {kind}")
        bili = None
        self._last_video = None
        if random.random() < float(self.cfg.get("bili_chance", 0.45)):
            import asyncio as _aio

            vids = await _aio.to_thread(fetch_bili_hot, 30)
            if vids:
                bili = random.choice(vids)
                self.state["last_bili"] = bili
                self._last_video = bili
                extra = (
                    "（你现在正无聊刷 B 站，刚刷到上面这条视频，想跟他提一句。"
                    "用你自己的口吻说一句感想或者吐槽，别复述标题，别像在念新闻。就一句。）"
                )
                sub = await _aio.to_thread(fetch_bili_subtitle, bili["bvid"])
                try:
                    save_json(
                        LAST_VIDEO,
                        {
                            "ts": time.time(),
                            "bvid": bili["bvid"],
                            "title": bili["title"],
                            "up": bili.get("up", ""),
                            "desc": bili.get("desc", ""),
                            "sub": (sub or "")[:1500],
                        },
                    )
                except Exception as _e:
                    logger.warning(f"[speak_up] 存视频资料失败: {_e}")
                prompt = (
                    f"（你刚看了一条视频：标题《{bili['title']}》，"
                    f"UP主 {bili['up']}，播放 {bili['view']}。简介：{bili['desc'] or '无'}"
                    + (f"\n字幕原文：{sub}" if sub else "\n（这条没字幕，只能看标题和简介）")
                    + "）"
                )
                logger.info(f"[speak_up] 这次刷到B站: {bili['title']} ({bili['bvid']})")

        st_line = her_state_line()
        system = self._persona_prompt() + "\n\n" + extra + (("\n\n" + st_line) if st_line else "")
        try:
            resp = await prov.text_chat(prompt=prompt, system_prompt=system, contexts=[])
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text.replace('"', "").replace("“", "").replace("”", "")[:120]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[speak_up] 生成失败: {e}")
            return ""

    async def _send(self, text: str) -> bool:
        try:
            platform = self.context.get_platform("aiocqhttp")
            if not platform:
                logger.warning("[speak_up] 没有 aiocqhttp 平台")
                return False
            client = platform.get_client()
            await client.api.call_action(
                "send_private_msg",
                user_id=int(self.cfg["target_user"]),
                message=[{"type": "text", "data": {"text": text}}],
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[speak_up] 发送失败: {e}")
            return False

    # ---------- 命令 ----------
    @filter.command("搭话状态")
    async def status(self, event):
        s = self.state
        last = s.get("last_spoke") or 0
        last_s = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "还没说过"
        yield event.plain_result(
            f"间隔 {self.cfg['interval_min']} 分 / 冷却 {self.cfg['cooldown_min']} 分\n"
            f"没中加成 {s.get('miss', 0)}×{self.cfg['miss_bonus']}\n"
            f"上次主动开口：{last_s}\n"
            f"试运行：{'开' if self.cfg['dry_run'] else '关'}"
        )
