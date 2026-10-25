# -*- coding: utf-8 -*-
"""刷B站 v2：读字幕判断 + 观看历史 + 去重（移植 dsh-bili-scroll 的核心思路）

- fetch_hot       热门列表（自动排除最近看过的）
- fetch_video     视频详情 + 字幕原文
- fetch_subtitle  字幕（需要 /AstrBot/data/bili_cookies.json）
- 历史记录 /AstrBot/data/bili_history.json：{ts, bvid, title, up, decision, line}

命令：/b站  热门前5      /看过的  最近看过的
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
COOKIE_PATH = os.path.join(DATA, "bili_cookies.json")
HISTORY_PATH = os.path.join(DATA, "bili_history.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_cookie_cache = {"value": "", "ts": 0.0}


# ---------------- 网络 ----------------
def _headers(cookie: str = "") -> dict:
    h = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def _get(url: str, cookie: str = "", timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers=_headers(cookie))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def login_cookie() -> str:
    """已扫码登录的 cookie（含 SESSDATA），没有就返回空"""
    try:
        with open(COOKIE_PATH, encoding="utf-8") as f:
            ck = json.load(f).get("cookies") or {}
        return "; ".join(f"{k}={v}" for k, v in ck.items())
    except Exception:
        return ""


def guest_cookie(force: bool = False) -> str:
    now = time.time()
    if not force and _cookie_cache["value"] and now - _cookie_cache["ts"] < 1800:
        return _cookie_cache["value"]
    try:
        d = _get("https://api.bilibili.com/x/frontend/finger/spi")
        data = d.get("data") or {}
        c = "buvid3=%s; buvid4=%s; b_nut=1700000000" % (
            data.get("b_3", ""),
            data.get("b_4", ""),
        )
        if data.get("b_3"):
            _cookie_cache["value"] = c
            _cookie_cache["ts"] = now
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bili] 取 buvid 失败: {e}")
        return ""


def _api(url: str) -> dict:
    ck = login_cookie() or guest_cookie()
    d = _get(url, ck)
    if d.get("code") not in (0, None):
        logger.warning(f"[bili] {d.get('code')} {d.get('message')}")
        d = _get(url, guest_cookie(force=True))
    return d


# ---------------- 历史 ----------------
def load_history() -> list:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def add_history(entry: dict):
    hist = load_history()
    hist.append(entry)
    hist = hist[-500:]
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bili] 写历史失败: {e}")


def recent_bvids(days: int = 7) -> set:
    cutoff = time.time() - days * 86400
    return {h["bvid"] for h in load_history() if h.get("ts", 0) >= cutoff and h.get("bvid")}


# ---------------- 数据 ----------------
def _item(i: dict) -> dict:
    return {
        "title": i.get("title"),
        "bvid": i.get("bvid"),
        "up": (i.get("owner") or {}).get("name") or i.get("author"),
        "view": (i.get("stat") or {}).get("view") or i.get("play"),
        "desc": (i.get("desc") or i.get("description") or "")[:100],
    }


def fetch_subtitle(bvid: str, max_chars: int = 1600) -> str:
    """读字幕原文（需要登录 cookie）"""
    ck = login_cookie()
    if not ck:
        return ""
    try:
        v = _get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", ck)
        data = v.get("data") or {}
        aid, cid = data.get("aid"), data.get("cid")
        if not aid or not cid:
            return ""
        p = _get(
            f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}", ck
        )
        subs = ((p.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if not subs:
            return ""
        u = subs[0].get("subtitle_url") or ""
        if u.startswith("//"):
            u = "https:" + u
        j = _get(u, ck)
        text = " ".join(
            (x.get("content") or "") for x in (j.get("body") or [])
        ).strip()
        return text[:max_chars]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[bili] 取字幕失败 {bvid}: {e}")
        return ""


def fetch_hot(limit: int = 6, exclude_seen: bool = True) -> list:
    d = _api("https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1")
    lst = (d.get("data") or {}).get("list") or []
    seen = recent_bvids(7) if exclude_seen else set()
    out = []
    for i in lst:
        it = _item(i)
        if it["bvid"] in seen:
            continue
        out.append(it)
        if len(out) >= limit:
            break
    return out


def fetch_search(keyword: str, limit: int = 5) -> list:
    q = urllib.parse.quote(keyword)
    d = _api(
        "https://api.bilibili.com/x/web-interface/search/type"
        f"?search_type=video&keyword={q}"
    )
    out = []
    for i in ((d.get("data") or {}).get("result") or [])[:limit]:
        it = _item(i)
        it["title"] = re.sub(r"<[^>]+>", "", it["title"] or "")
        out.append(it)
    return out


def fetch_video(bvid: str, with_subtitle: bool = True):
    d = _api(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    data = d.get("data") or {}
    if not data:
        return None
    st = data.get("stat") or {}
    info = {
        "title": data.get("title"),
        "bvid": bvid,
        "up": (data.get("owner") or {}).get("name"),
        "view": st.get("view"),
        "like": st.get("like"),
        "duration": round((data.get("duration") or 0) / 60, 1),
        "desc": (data.get("desc") or "").strip().replace("\n", " ")[:250],
        "subtitle": "",
    }
    if with_subtitle:
        info["subtitle"] = fetch_subtitle(bvid)
    return info


def fmt_list(items: list) -> str:
    return "\n".join(
        f"{i}. 《{v['title']}》UP:{v['up']} 播放:{v['view']} BV:{v['bvid']}\n"
        f"   简介:{v['desc'] or '无'}"
        for i, v in enumerate(items, 1)
    )


def fmt_one(v: dict) -> str:
    s = (
        f"《{v['title']}》\nUP:{v['up']} 播放:{v['view']} 点赞:{v.get('like')} "
        f"时长:{v.get('duration')}分钟 BV:{v['bvid']}\n简介:{v['desc'] or '无'}"
    )
    if v.get("subtitle"):
        s += f"\n字幕原文（节选）：{v['subtitle']}"
    return s


HOT_WORDS = [
    "热门", "热榜", "热搜", "刷b站", "刷B站", "逛b站", "逛B站",
    "看看视频", "今天有什么", "b站有什么", "B站有什么", "有什么好玩的", "有什么好看的",
    "b站", "B站", "哔哩", "bilibili", "BILIBILI", "小破站",
]
SEARCH_PAT = re.compile(r"(?:搜一下|搜搜|搜索|找找|查查)\s*[「\"']?([^」\"'\s，。]{2,20})")
BV_PAT = re.compile(r"BV[0-9A-Za-z]{10}")

HINT = (
    "【你已经看过了下面的 B 站内容（有字幕原文），别说你看不到、别说你不知道】\n"
    "按你的人设，用一两句话跟他说说你看到了什么、有什么感想，"
    "挑最有意思的一条讲，最好带上视频里真实提到的细节，别罗列、别念标题。\n\n"
)


class BiliPlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        logger.info(
            "[bili] v2 已加载 | 登录=%s | 已看过 %d 条",
            "是" if login_cookie() else "否",
            len(load_history()),
        )

    # ---------- 关键词注入 ----------
    @filter.on_llm_request()
    async def inject(self, event, req) -> None:
        try:
            msg = (event.message_str or "").strip()
            logger.info(f"[bili] hook 触发 msg=[{msg[:40]}]")
            if event.get_extra("_bili_injected") or not msg:
                return

            payload = None
            bv = BV_PAT.search(msg)
            if bv:
                v = await asyncio.to_thread(fetch_video, bv.group(0))
                if v:
                    payload = "他发来一条视频，内容：\n" + fmt_one(v)
                    add_history(
                        {
                            "ts": time.time(),
                            "bvid": v["bvid"],
                            "title": v["title"],
                            "up": v["up"],
                            "decision": "分享",
                            "line": "",
                        }
                    )
                    logger.info(f"[bili] 注入视频 {v['bvid']} 字幕{len(v.get('subtitle') or '')}字")
            elif any(w in msg for w in HOT_WORDS):
                items = await asyncio.to_thread(fetch_hot, 6)
                if items:
                    payload = "你刚翻了 B 站热门，看到这些：\n" + fmt_list(items)
                    logger.info(f"[bili] 注入热门 {len(items)} 条")
                else:
                    logger.warning("[bili] 热门为空，未注入")
            else:
                m = SEARCH_PAT.search(msg)
                if m:
                    kw = m.group(1)
                    items = await asyncio.to_thread(fetch_search, kw, 5)
                    if items:
                        payload = f"你刚在 B 站搜了「{kw}」，结果：\n" + fmt_list(items)
                        logger.info(f"[bili] 注入搜索「{kw}」")

            if payload:
                req.system_prompt = (
                    (getattr(req, "system_prompt", "") or "") + "\n\n" + HINT + payload
                )
                event.set_extra("_bili_injected", True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[bili] 注入失败: {e}")

    # ---------- 工具 ----------
    @filter.llm_tool(name="bili_hot")
    async def bili_hot(self, event, count: int = 8) -> str:
        """看 B 站当前的热门视频（自动排除最近看过的）。

        Args:
            count(number): 想看几条，默认 8
        """
        items = await asyncio.to_thread(fetch_hot, max(1, min(int(count), 20)))
        logger.info(f"[bili] tool bili_hot 返回 {len(items)} 条")
        return "B站热门：\n" + fmt_list(items) if items else "没拿到热门"

    @filter.llm_tool(name="bili_search")
    async def bili_search(self, event, keyword: str, count: int = 5) -> str:
        """在 B 站搜索视频。

        Args:
            keyword(string): 搜索关键词
            count(number): 返回几条，默认 5
        """
        items = await asyncio.to_thread(
            fetch_search, keyword, max(1, min(int(count), 10))
        )
        logger.info(f"[bili] tool bili_search「{keyword}」返回 {len(items)} 条")
        return f"搜「{keyword}」：\n" + fmt_list(items) if items else f"没搜到「{keyword}」"

    @filter.llm_tool(name="bili_video")
    async def bili_video(self, event, bvid: str) -> str:
        """看某一条 B 站视频（含字幕原文）。

        Args:
            bvid(string): 视频的 BV 号
        """
        bvid = (bvid or "").strip()
        if not bvid.upper().startswith("BV"):
            return "需要 BV 号"
        v = await asyncio.to_thread(fetch_video, bvid)
        logger.info(f"[bili] tool bili_video {bvid} -> {'成功' if v else '失败'}")
        return fmt_one(v) if v else f"没找到 {bvid}"

    # ---------- 命令 ----------
    @filter.command("b站")
    async def bili_debug(self, event):
        try:
            items = await asyncio.to_thread(fetch_hot, 5)
            if not items:
                yield event.plain_result("没拿到热门（接口异常，看日志）")
                return
            lines = [f"{i}. {v['title'][:26]}｜{v['up']}" for i, v in enumerate(items, 1)]
            yield event.plain_result("B站热门前5：\n" + "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"取热门失败：{e}")

    @filter.command("看过的")
    async def bili_history(self, event):
        hist = load_history()[-10:]
        if not hist:
            yield event.plain_result("还没看过视频呢")
            return
        lines = []
        for h in reversed(hist):
            t = time.strftime("%m-%d %H:%M", time.localtime(h.get("ts", 0)))
            lines.append(f"{t} [{h.get('decision', '?')}] {str(h.get('title'))[:24]}")
        yield event.plain_result("最近看过的：\n" + "\n".join(lines))
