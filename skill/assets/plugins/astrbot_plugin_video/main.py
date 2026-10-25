# -*- coding: utf-8 -*-
"""发链接 → 下载 → 发视频

支持：
- B 站（BV 号 / 完整链接 / b23.tv 短链）
- 直链 mp4

体积超过 max_mb 时，用 ffmpeg 截取前 N 秒再发，并说一句为什么。

用法：
  发  https://www.bilibili.com/video/BVxxxx
  发  看看这个 https://b23.tv/xxxx
  命令：/发视频 <链接>
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

from astrbot.api import logger, star
from astrbot.api.event import filter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", ".."))
CONF = os.path.join(DATA, "config", "astrbot_plugin_video_config.json")
COOKIES = os.path.join(DATA, "bili_cookies.json")
TMPDIR = "/tmp/astrbot_video"
_recent_bv = {}  # bvid -> ts，防止同一链接重复下载
LAST_VIDEO = os.path.join(DATA, "last_video.json")

DEFAULT_CFG = {"enabled": True, "max_mb": 40, "clip_seconds": 45, "auto_detect": True}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
URL_RE = re.compile(r"https?://[^\s，。！？、）)】\]]+")
# 触发词：他说要看视频
ASK = ["发我", "发视频", "发一下", "下载", "看看这个", "看看这个视频", "发过来", "传给我", "给我看"]


def _cookies() -> str:
    try:
        d = json.load(open(COOKIES, encoding="utf-8"))
        jar = d.get("cookies") if isinstance(d.get("cookies"), dict) else d
        parts = [f"{k}={v}" for k, v in jar.items() if v and isinstance(v, str)]
        if parts:
            return "; ".join(parts)
    except Exception:
        pass
    try:
        req = urllib.request.Request(
            "https://api.bilibili.com/x/frontend/finger/spi", headers={"User-Agent": UA}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode())
        return f"buvid3={j['data']['b_3']}; buvid4={j['data']['b_4']}; b_nut=1700000000"
    except Exception:
        return ""


def _get_json(url: str, cookie: str):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/", "Cookie": cookie},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _expand_short(url: str) -> str:
    """b23.tv 短链 → 真实链接"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except Exception:
        return url


def _download(url: str, dest: str, cookie: str = "") -> int:
    hdr = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        hdr["Cookie"] = cookie
    req = urllib.request.Request(url, headers=hdr)
    total = 0
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    return total


def _clip(src: str, dst: str, seconds: int) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-t", str(seconds), "-c", "copy", dst],
            check=True,
            timeout=120,
        )
        return os.path.exists(dst) and os.path.getsize(dst) > 10240
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[video] 截取失败: {e}")
        return False



def _rm(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass



_BILI_CACHE = {}


def _bili_info(bvid: str):
    """按 BV 号取标题/简介/字幕（带缓存）"""
    hit = _BILI_CACHE.get(bvid)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    ck = _cookies()
    try:
        j = _get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", ck)
        if j.get("code") != 0:
            return None
        d = j["data"]
        sub = ""
        try:
            vj = _get_json(
                f"https://api.bilibili.com/x/player/wbi/v2?aid={d['aid']}&cid={d['cid']}", ck
            )
            subs = ((vj.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            if subs:
                u = subs[0]["subtitle_url"]
                if u.startswith("//"):
                    u = "https:" + u
                req = urllib.request.Request(u, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = json.loads(r.read().decode()).get("body", [])
                sub = "\n".join(x["content"] for x in body[:40])
        except Exception:
            pass
        info = {
            "bvid": bvid,
            "title": d.get("title", ""),
            "up": (d.get("owner") or {}).get("name", ""),
            "desc": d.get("desc", ""),
            "sub": sub[:1500],
        }
        _BILI_CACHE[bvid] = (time.time(), info)
        return info
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[video] 取资料失败 {bvid}: {e}")
        return None

class VideoPlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.cfg = dict(DEFAULT_CFG)
        self._load()
        os.makedirs(TMPDIR, exist_ok=True)
        logger.info("[video] 已加载")

    def _load(self):
        try:
            self.cfg.update(json.load(open(CONF, encoding="utf-8-sig")))
        except Exception:
            pass

    # ---------- B站 ----------
    def _bili(self, text: str):
        m = BV_RE.search(text)
        if m:
            bvid = m.group(1)
        else:
            u = URL_RE.search(text)
            if not u:
                return None
            link = u.group(0)
            if "b23.tv" in link:
                link = _expand_short(link)
            mm = BV_RE.search(link)
            if not mm:
                return None
            bvid = mm.group(1)

        cookie = _cookies()
        info = _get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", cookie)
        if info.get("code") != 0:
            logger.warning(f"[video] view 失败 {bvid}: {info.get('message')}")
            return None
        d = info["data"]
        cid = d["cid"]
        title = d.get("title", "")
        dur = d.get("duration", 0)
        for qn in (32, 16):
            try:
                j = _get_json(
                    f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}"
                    f"&qn={qn}&fnval=1&fnver=0&fourk=0&platform=html5&high_quality=1",
                    cookie,
                )
                if j.get("code") != 0:
                    continue
                durl = (j.get("data") or {}).get("durl") or []
                if durl:
                    return {
                        "url": durl[0]["url"],
                        "size": durl[0].get("size", 0),
                        "title": title,
                        "desc": d.get("desc", ""),
                        "duration": dur,
                        "bvid": bvid,
                        "up": (d.get("owner") or {}).get("name", ""),
                        "cookie": cookie,
                    }
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[video] playurl qn={qn} 失败: {e}")
        return None

    # ---------- 发送 ----------
    async def _send_video(self, uid, path: str) -> bool:
        try:
            platform = self.context.get_platform("aiocqhttp")
            if not platform:
                return False
            client = platform.get_client()
            await client.api.call_action(
                "send_msg",
                message_type="private",
                user_id=int(uid),
                message=[{"type": "video", "data": {"file": f"file://{path}"}}],
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[video] 发送失败: {e}")
            return False

    async def _send_text(self, uid, text: str):
        try:
            platform = self.context.get_platform("aiocqhttp")
            client = platform.get_client()
            await client.api.call_action(
                "send_msg",
                message_type="private",
                user_id=int(uid),
                message=[{"type": "text", "data": {"text": text}}],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[video] 发文字失败: {e}")

    # ---------- 主流程 ----------
    async def _handle(self, uid, text: str):
        self._load()
        info = await asyncio.to_thread(self._bili, text)
        if not info:
            # 直链 mp4
            u = URL_RE.search(text)
            if u and u.group(0).lower().split("?")[0].endswith((".mp4", ".mov")):
                info = {"url": u.group(0), "size": 0, "title": "", "duration": 0, "cookie": ""}
            else:
                await self._send_text(uid, "这个链接我打不开诶")
                return

        # 同一个视频 10 分钟内不重复下载
        _bv = info.get("bvid") or ""
        if _bv:
            _last = _recent_bv.get(_bv, 0)
            if time.time() - _last < 600:
                logger.info(f"[video] {_bv} 刚发过，跳过")
                await self._send_text(uid, "刚发过啦 往上翻翻")
                return
            _recent_bv[_bv] = time.time()

        stamp = int(time.time())
        # 把他要的这一条也记下来，覆盖上一条（防止答错视频）
        if info.get("bvid"):
            try:
                json.dump(
                    {
                        "ts": time.time(),
                        "bvid": info.get("bvid"),
                        "title": info.get("title", ""),
                        "up": info.get("up", ""),
                        "desc": info.get("desc", ""),
                        "sub": "",
                        "line": "",
                        "source": "他发链接",
                    },
                    open(LAST_VIDEO, "w", encoding="utf-8"),
                    ensure_ascii=False,
                )
                logger.info(f"[video] last_video 更新为 {info.get('bvid')}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[video] 写 last_video 失败: {e}")
        raw = os.path.join(TMPDIR, f"v_{stamp}.mp4")
        try:
            size = await asyncio.to_thread(_download, info["url"], raw, info.get("cookie", ""))
            mb = size / 1048576
            limit = float(self.cfg.get("max_mb", 40))
            logger.info(f"[video] 下载完成 {mb:.1f}MB 时长{info.get('duration')}s {info.get('title','')[:20]}")

            if mb <= limit:
                ok = await self._send_video(uid, raw)
                if not ok:
                    await self._send_text(uid, "发不出去 太大了")
                return

            # 太大 → 截取
            clip = os.path.join(TMPDIR, f"c_{stamp}.mp4")
            secs = int(self.cfg.get("clip_seconds", 45))
            ok = await asyncio.to_thread(_clip, raw, clip, secs)
            if ok:
                await self._send_text(uid, f"这个 {mb:.0f}M 太长了 给你看前 {secs} 秒")
                await self._send_video(uid, clip)
            else:
                await self._send_text(uid, f"太大了（{mb:.0f}M）我传不上去诶")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[video] 处理失败: {e}")
            await self._send_text(uid, "下载失败了 等会儿再试")
        finally:
            try:
                if os.path.exists(raw):
                    os.remove(raw)
            except Exception:
                pass
            clip = os.path.join(TMPDIR, f"c_{stamp}.mp4")
            if os.path.exists(clip):
                asyncio.get_event_loop().call_later(300, _rm, clip)

    # ---------- 命令 ----------
    @filter.command("发视频")
    async def cmd_video(self, event, url: str = ""):
        uid = event.get_sender_id()
        text = url or event.message_str
        if not URL_RE.search(text) and not BV_RE.search(text):
            yield event.plain_result("发个链接给我，B站或者 mp4 都行")
            return
        event.stop_event()
        await self._handle(uid, text)

    # ---------- 自动识别 ----------

    # ---------- 视频上下文（她刚发过的那个） ----------
    @filter.on_llm_request()
    async def inject_video_ctx(self, event, req) -> None:
        try:
            if event.get_extra("_video_ctx_injected"):
                return
            text = (event.message_str or "").strip()
            if not text:
                return
            KEY = [
                "视频", "那个", "刚才", "学到", "套话", "你发的", "看了", "这条",
                "链接", "地址", "bv", "BV", "发的", "啥呀", "讲的",
            ]
            if not any(k in text for k in KEY):
                return
            # 这句里带了 BV 号 → 就以那一条为准（他引用的那条）
            m = BV_RE.search(text)
            info = None
            if m:
                info = _bili_info(m.group(1))
                if info:
                    info["ts"] = time.time()
                    info["line"] = ""
                    logger.info(f"[video] 按他引用的 BV 取资料: {m.group(1)}")
            if not info:
                try:
                    info = json.load(open(LAST_VIDEO, encoding="utf-8"))
                except Exception:
                    return
            if time.time() - float(info.get("ts", 0)) > 45 * 60:
                logger.info("[video] 视频上下文过期，不注入")
                return
            # 他这句话里带了链接/BV，说明在说另一条，别串台
            # 带了链接也从链接取，不再跳过
            sub = (info.get("sub") or "").strip()
            desc = (info.get("desc") or "").strip()
            said = (info.get("line") or "").strip()
            block = (
                "【你最近一次接触的视频（就是他问的这条）】\n"
                f"链接：https://www.bilibili.com/video/{info.get('bvid','')}\n"
                f"标题：{info.get('title','')}\n"
                f"UP主：{info.get('up','')}\n"
                + (f"简介：{desc[:300]}\n" if desc else "")
                + (f"字幕节选：{sub[:800]}\n" if sub else "")
                + (f"你当时说的话：{said}\n" if said else "")
                + "\n【怎么接】你只刷到标题、简介和字幕，没完整看完，别编具体画面。\n"
                "字幕有就按字幕说；字幕没拿到就别编方法名和术语，只能说感觉；\n"
                "如果他问你会不会用，就直说你的看法。\n"
                "不要再说「还没看」——你已经说过自己的感觉了，别自相矛盾。\\n"
                "（如果你并不确定视频里讲了什么，就直接说不记得了，别硬编。）"
            )
            # 注入已交给 context 聚合器；这里只把解析出的这条写回 last_video.json
            try:
                info["ts"] = time.time()
                json.dump(info, open(LAST_VIDEO, "w", encoding="utf-8"), ensure_ascii=False)
                logger.info(f"[video] 视频资料已写入 last_video: {info.get('bvid')}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[video] 写 last_video 失败: {e}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[video] 注入失败: {e}")

    @filter.regex(r"https?://\S*(bilibili\.com|b23\.tv|\.mp4|\.mov)")
    async def auto_video(self, event):
        self._load()
        if not self.cfg.get("auto_detect", True) or not self.cfg.get("enabled", True):
            return
        text = event.message_str or ""
        # 看到链接就直接发
        event.stop_event()
        await self._handle(event.get_sender_id(), text)
