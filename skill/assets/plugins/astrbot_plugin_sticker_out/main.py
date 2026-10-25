# -*- coding: utf-8 -*-
"""表情包 v5：会自己上网攒表情包，行为全部可在面板里调

配置在两个地方（都能改）：
  1. AstrBot 面板 → 插件 → 表情 → 配置（_conf_schema.json 生成的表单，推荐）
     → 实际存 /AstrBot/data/config/astrbot_plugin_sticker_out_config.json
  2. 插件目录 config.json（旧版遗留，作为兜底）

能力：
  - 按对话内容识别 17 种情绪，70% 概率配表情（可调）
  - 图片表情包 / QQ 原生表情混着发
  - 后台自动上网补货（Bing 图片搜索），同一张图有冷却
  - /表情库存 /表情删除 /补表情 /表情包 /表情测试
"""

import asyncio
import glob
import hashlib
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request

from astrbot.api import logger, star
from astrbot.api.event import filter
from astrbot.api.message_components import Face, Image, Plain

TOKEN_RE = re.compile(r"\[(face|sticker):([^\[\]]{1,20})\]")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", ".."))
PLUGIN_CONF = os.path.join(DATA, "config", "astrbot_plugin_sticker_out_config.json")
LOCAL_CFG = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "usage.json")

DEFAULT_CFG = {
    "auto_prob": 0.22,
    "sticker_cooldown_s": 90,
    "image_ratio": 0.75,
    "gif_ratio": 0.6,
    "online_enabled": True,
    "search_interval_min": 45,
    "max_new_per_round": 6,
    "min_pool_size": 5,
    "max_file_kb": 900,
    "repeat_cooldown_hours": 6,
}

NEGATIVE_WORDS = [
    "难受", "不开心", "难过", "伤心", "委屈", "想哭", "呜呜", "哭",
    "生气", "烦", "讨厌", "气死", "不理", "算了", "无语", "服了",
    "心累", "累", "失望", "算了", "凭什么", "为什么",
]
NEGATIVE_EMOS = ["委屈", "难过", "生气", "无语", "哭"]
QUARREL_HINT = ["委屈", "生气", "无语", "难过"]

EMOTION_ALIAS = {
    "白眼": "无语", "翻白眼": "无语", "冷漠": "无语", "呵呵": "无语",
    "鄙视": "无语", "嫌弃": "无语", "无语子": "无语",
    "窃喜": "得意", "坏笑": "得意", "得意洋洋": "得意",
    "哭": "难过", "流泪": "难过", "想哭": "委屈",
    "笑笑": "笑死", "大笑": "笑死", "狂笑": "笑死",
    "饿": "馋", "想吃": "馋", "流口水": "馋",
    "惊": "震惊", "震惊了": "震惊",
    "问": "疑惑", "困惑": "疑惑",
    "喜欢": "亲亲", "爱你": "亲亲",
}

DEFAULT_RULES = [
    (["哈哈", "笑死", "好笑", "好玩", "逗", "嘿嘿", "233"], ["笑死"]),
    (["撒娇", "要亲亲", "要抱抱", "贴贴", "么么", "mua"], ["撒娇", "亲亲", "抱抱"]),
    (["想你", "想我", "好想你", "想我没"], ["想你"]),
    (["开心", "高兴", "太好了", "好棒", "真好"], ["开心", "笑死"]),
    (["难过", "不开心", "伤心", "难受", "想哭", "呜呜"], ["难过", "委屈"]),
    (["哼", "生气", "讨厌", "气死", "烦", "不理你"], ["生气", "无语"]),
    (["委屈", "可怜", "冤枉", "欺负"], ["委屈"]),
    (["无语", "离谱", "服了", "算了", "真是", "害"], ["无语"]),
    (["睡", "困", "晚安", "熬夜", "累了"], ["晚安"]),
    (["吃", "馋", "饿", "饭", "外卖", "好吃"], ["馋"]),
    (["厉害", "牛", "强", "棒", "赞", "加油", "冲", "赢了"], ["加油", "得意"]),
    (["怕", "吓", "天啊", "居然", "竟然"], ["震惊"]),
    (["为什么", "?", "？", "吗", "怎么", "啥", "嗯"], ["疑惑"]),
    (["宝", "爱", "喜欢", "亲", "害羞", "脸红"], ["害羞", "亲亲"]),
]

DEFAULT_WORDS = {
    "笑死": "搞笑 表情包 动图",
    "撒娇": "撒娇 表情包 动图",
    "亲亲": "亲亲 表情包",
    "抱抱": "抱抱 表情包",
    "想你": "想你 表情包",
    "开心": "开心 表情包 动图",
    "难过": "难过 表情包",
    "生气": "生气 表情包",
    "委屈": "委屈 表情包",
    "无语": "无语 表情包",
    "晚安": "晚安 表情包",
    "馋": "馋 好吃 表情包",
    "得意": "得意 表情包",
    "加油": "加油 表情包",
    "震惊": "震惊 表情包",
    "疑惑": "疑问 表情包",
    "害羞": "害羞 表情包",
}

FALLBACK_EMO = ["撒娇", "害羞", "亲亲", "笑死", "抱抱", "想你"]
FACE_FALLBACK = ["偷笑", "呲牙", "害羞", "捂脸", "吃瓜", "爱心", "微笑"]
STICKER_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def parse_pairs(text: str, sep: str = ":") -> dict:
    """把 '情绪: a,b,c' 的多行文本解析成 {情绪: [a,b,c]}"""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or sep not in line:
            continue
        key, val = line.split(sep, 1)
        key = key.strip()
        items = [x.strip() for x in re.split(r"[,，|]", val) if x.strip()]
        if key and items:
            out[key] = items
    return out


class StickerOutPlugin(star.Star):
    PREFIXES = ("鲸", "耄耋", "猫", "狗", "仓鼠")

    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        import astrbot.api as _api

        _api._sticker_gen = getattr(_api, "_sticker_gen", 0) + 1
        self._gen = _api._sticker_gen

        self._conf_mtime = 0.0
        self.cfg = dict(DEFAULT_CFG)
        self.rules = list(DEFAULT_RULES)
        self.words = dict(DEFAULT_WORDS)
        self._reload_cfg(force=True)

        self.sticker_dir = os.path.join(DATA, "stickers")
        os.makedirs(self.sticker_dir, exist_ok=True)

        self.name2id = {}
        fj = os.path.join(HERE, "face_map.json")
        if os.path.exists(fj):
            with open(fj, encoding="utf-8") as f:
                for fid, name in json.load(f).items():
                    self.name2id.setdefault(name, int(fid))

        self.usage = {}
        try:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, encoding="utf-8") as f:
                    self.usage = json.load(f)
        except Exception:
            self.usage = {}

        self.groups = self._scan()
        self._stats = {"token": 0, "auto_img": 0, "auto_face": 0, "fetched": 0}
        self._fetching = False
        logger.info(
            "[sticker_out] v5：%d 张 / %d 情绪 | 自动 %.0f%%（图片 %.0f%%）| 补货 %s",
            sum(len(v) for v in self.groups.values()),
            len(self.groups),
            self.cfg["auto_prob"] * 100,
            self.cfg["image_ratio"] * 100,
            "开" if self.cfg["online_enabled"] else "关",
        )
        self._task = asyncio.get_event_loop().create_task(self._loop())

    # ---------------- 配置 ----------------
    def _reload_cfg(self, force: bool = False):
        """读面板配置（有变化才重读）"""
        try:
            mtime = os.path.getmtime(PLUGIN_CONF) if os.path.exists(PLUGIN_CONF) else 0
        except OSError:
            mtime = 0
        if not force and mtime and mtime == self._conf_mtime:
            return
        self._conf_mtime = mtime

        raw = {}
        for path in (PLUGIN_CONF, LOCAL_CFG):
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        raw.update(json.load(f))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[sticker_out] 读配置失败 {path}: {e}")

        for k in ("auto_prob", "image_ratio", "gif_ratio", "sticker_cooldown_s"):
            if k in raw:
                try:
                    raw[k] = max(0.0, min(1.0, float(raw[k])))
                except Exception:
                    raw.pop(k, None)
        self.cfg = {**DEFAULT_CFG, **{k: v for k, v in raw.items() if k in DEFAULT_CFG}}

        rules_txt = raw.get("emotion_rules")
        if isinstance(rules_txt, str) and rules_txt.strip():
            parsed = parse_pairs(rules_txt)
            if parsed:
                self.rules = [(v, [k]) for k, v in parsed.items()]
        words_txt = raw.get("search_words")
        if isinstance(words_txt, str) and words_txt.strip():
            parsed = parse_pairs(words_txt)
            if parsed:
                self.words = {k: " ".join(v) for k, v in parsed.items()}

    # ---------------- 池 ----------------
    @classmethod
    def _emotion_of(cls, stem: str) -> str:
        s = stem
        for pre in cls.PREFIXES:
            if s.startswith(pre):
                s = s[len(pre):]
                break
        s = s.split("__")[0]
        return re.sub(r"[_\-]?\d+$", "", s)

    def _scan(self) -> dict:
        groups = {}
        for fp in glob.glob(os.path.join(self.sticker_dir, "*")):
            stem, ext = os.path.splitext(os.path.basename(fp))
            if ext.lower() not in STICKER_EXTS:
                continue
            emo = self._emotion_of(stem)
            if emo:
                groups.setdefault(emo, []).append(fp)
        return groups

    def _is_negative(self, text: str) -> bool:
        return any(w in text for w in NEGATIVE_WORDS)

    def _pick_emotion(self, text: str):
        # 负面语境：只许从负面池里挑，绝不发搞笑/馋/开心
        if self._is_negative(text):
            alive = [e for e in NEGATIVE_EMOS if e in self.groups]
            if alive:
                return random.choice(alive)
            return None
        for keys, emos in self.rules:
            if any(k and k in text for k in keys):
                alive = [e for e in emos if e in self.groups]
                if alive:
                    return random.choice(alive)
        # 没命中任何情绪就不发，绝不随机凑一张（之前因此发错过图）
        return None

    def _pick_file(self, emo: str):
        files = self.groups.get(emo) or []
        if not files:
            # 没这个情绪：走别名，比如「白眼」→「无语」
            alias = EMOTION_ALIAS.get(emo)
            if alias:
                files = self.groups.get(alias) or []
        if not files:
            return None
        # 有动图时优先发动图（gif_ratio 控制比例）
        gifs = [f for f in files if f.lower().endswith(".gif")]
        if gifs and random.random() < float(self.cfg.get("gif_ratio", 0.6)):
            files = gifs
        now = time.time()
        cool = float(self.cfg["repeat_cooldown_hours"]) * 3600
        fresh = [f for f in files if now - float(self.usage.get(f, 0)) > cool]
        return random.choice(fresh or files)

    def _mark_used(self, path: str):
        self.usage[path] = time.time()
        if len(self.usage) > 800:
            self.usage = dict(sorted(self.usage.items(), key=lambda x: -x[1])[:400])
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.usage, f, ensure_ascii=False)
        except Exception:
            pass

    def _pick_face(self):
        alive = [n for n in FACE_FALLBACK if n in self.name2id]
        return random.choice(alive) if alive else None

    def _menu(self) -> str:
        lines = ["【表情】可以在回复末尾带一个标记，一条最多一个："]
        if self.groups:
            lines.append(f"  [sticker:情绪] 发表情包，可选：{'、'.join(sorted(self.groups))}")
        faces = [n for n in ["偷笑", "白眼", "呲牙", "捂脸", "吃瓜", "爱心", "害羞"] if n in self.name2id]
        lines.append(f"  [face:表情名] 发 QQ 表情，可用：{'、'.join(faces)}")
        lines.append("  想带就带，不带也正常。")
        return "\n".join(lines)

    # ---------------- 联网补货 ----------------
    async def _loop(self):
        while True:
            try:
                import astrbot.api as _api

                if self._gen != getattr(_api, "_sticker_gen", 0):
                    logger.info(f"[sticker_out] 旧实例 gen={self._gen} 退出")
                    return
                await asyncio.sleep(max(300, int(self.cfg["search_interval_min"]) * 60))
                self._reload_cfg()
                if self.cfg.get("online_enabled", True):
                    await self._topup()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[sticker_out] 补货循环异常: {e}")

    async def _topup(self, only_emo: str = ""):
        if self._fetching:
            return
        self._fetching = True
        try:
            need = [
                (emo, words)
                for emo, words in self.words.items()
                if (not only_emo or emo == only_emo)
                and len(self.groups.get(emo, [])) < int(self.cfg["min_pool_size"])
            ]
            if not need:
                logger.info("[sticker_out] 库存充足，本轮不补货")
                return
            random.shuffle(need)
            budget = int(self.cfg["max_new_per_round"])
            for emo, words in need:
                if budget <= 0:
                    break
                budget -= await asyncio.to_thread(self._fetch_one, emo, words, budget)
            self.groups = self._scan()
            logger.info(
                "[sticker_out] 补货完成，现有 %d 张 / %d 情绪",
                sum(len(v) for v in self.groups.values()),
                len(self.groups),
            )
        finally:
            self._fetching = False

    def _fetch_one(self, emo: str, words: str, budget: int) -> int:
        try:
            q = urllib.parse.quote(words)
            url = f"https://cn.bing.com/images/search?q={q}&form=HDRSC2&first=1"
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                html = r.read().decode("utf-8", "ignore")
            urls = re.findall(r'murl&quot;:&quot;(.*?)&quot;', html)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_out] 搜索失败 {emo}: {e}")
            return 0

        got = 0
        for u in urls:
            if got >= budget:
                break
            u = u.replace("\\/", "/")
            try:
                req = urllib.request.Request(
                    u, headers={"User-Agent": _UA, "Referer": "https://cn.bing.com/"}
                )
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = r.read()
            except Exception:
                continue
            if not (3000 < len(data) and len(data) / 1024 <= float(self.cfg["max_file_kb"])):
                continue
            head = data[:8]
            ext = (
                ".gif" if head.startswith(b"GIF8")
                else ".png" if head.startswith(b"\x89PNG")
                else ".jpg" if head[:2] == b"\xff\xd8"
                else None
            )
            if not ext:
                continue
            h = hashlib.md5(data).hexdigest()[:8]
            path = os.path.join(self.sticker_dir, f"{emo}__{h}{ext}")
            if os.path.exists(path):
                continue
            try:
                with open(path, "wb") as f:
                    f.write(data)
            except Exception:
                continue
            got += 1
            self._stats["fetched"] += 1
            logger.info(f"[sticker_out] 新表情 {emo} <- {os.path.basename(path)} ({len(data)//1024}KB)")
        return got

    # ---------------- 钩子 ----------------
    @filter.on_llm_request()
    async def inject_menu(self, event, req) -> None:
        try:
            if event.get_extra("_sticker_menu_injected"):
                return
            req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + self._menu()
            event.set_extra("_sticker_menu_injected", True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_out] 注入失败: {e}")

    def _convert(self, text: str):
        parts, pos, hit = [], 0, 0
        for m in TOKEN_RE.finditer(text):
            if m.start() > pos:
                parts.append(Plain(text[pos:m.start()]))
            kind, key = m.group(1), m.group(2).strip()
            if kind == "face" and key in self.name2id:
                parts.append(Face(id=self.name2id[key]))
                hit += 1
            elif kind == "sticker":
                # _pick_file 内部会走别名（白眼→无语）
                p = self._pick_file(key)
                if p:
                    parts.append(Image.fromFileSystem(p))
                    self._mark_used(p)
                    hit += 1
                else:
                    logger.info(f"[sticker_out] 没有这个情绪的表情: {key}，跳过")
            else:
                parts.append(Plain(m.group(0)))
            pos = m.end()
        if pos < len(text):
            parts.append(Plain(text[pos:]))
        return parts, hit

    @filter.on_decorating_result()
    async def decorate(self, event) -> None:
        try:
            self._reload_cfg()
            result = event.get_result()
            if not result or not getattr(result, "chain", None):
                return
            new_chain, hit, texts = [], 0, []
            for comp in result.chain:
                if isinstance(comp, Plain) and TOKEN_RE.search(comp.text or ""):
                    parts, h = self._convert(comp.text)
                    new_chain.extend(parts)
                    hit += h
                else:
                    new_chain.append(comp)
                    if isinstance(comp, Plain):
                        texts.append(comp.text or "")
            has_media = any(isinstance(c, (Face, Image)) for c in new_chain)
            body = "".join(texts).strip()
            if not has_media and hit == 0 and body:
                import astrbot.api as _api

                _qu = float(getattr(_api, "_quarrel_until", 0) or 0)
                in_quarrel = _qu > time.time()
                if in_quarrel:
                    # 吵架中：只有一半概率配一张，且只能负面
                    if random.random() < 0.5:
                        alive = [e for e in QUARREL_HINT if e in self.groups]
                        if alive:
                            emo = random.choice(alive)
                            fp = self._pick_file(emo)
                            if fp:
                                chain.append(Image.fromFileSystem(fp))
                                self._mark_used(fp)
                                self.usage["__last__"] = time.time()
                    return
                now_ts = time.time()
                if now_ts - float(self.usage.get("__last__", 0)) < float(
                    self.cfg.get("sticker_cooldown_s", 90)
                ):
                    return
                if random.random() < float(self.cfg["auto_prob"]):
                    use_img = self.groups and random.random() < float(self.cfg["image_ratio"])
                    if use_img:
                        emo = self._pick_emotion(body)
                        p = self._pick_file(emo) if emo else None
                        if p:
                            new_chain.append(Image.fromFileSystem(p))
                            self._mark_used(p)
                            self._stats["auto_img"] += 1
                            logger.info(f"[sticker_out] 配图 {emo} -> {os.path.basename(p)}（{body[:18]}）")
                    else:
                        name = self._pick_face()
                        if name:
                            new_chain.append(Face(id=self.name2id[name]))
                            self._stats["auto_face"] += 1
            if hit:
                self._stats["token"] += 1
            result.chain[:] = new_chain
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[sticker_out] 转换失败: {e}")

    # ---------------- 命令 ----------------
    @filter.command("补表情")
    async def cmd_topup(self, event, emo: str = ""):
        yield event.plain_result("开始补货，稍等…")
        self._reload_cfg()
        await self._topup(emo.strip())
        total = sum(len(v) for v in self.groups.values())
        yield event.plain_result(f"补货完成，现有 {total} 张 / {len(self.groups)} 种情绪")

    @filter.command("表情库存")
    async def cmd_stock(self, event):
        lines = [
            f"{emo}: {len(files)} 张（动图 {sum(1 for f in files if f.lower().endswith('.gif'))}）"
            for emo, files in sorted(self.groups.items())
        ]
        total = sum(len(v) for v in self.groups.values())
        tg = sum(1 for v in self.groups.values() for f in v if f.lower().endswith(".gif"))
        yield event.plain_result(f"共 {total} 张，动图 {tg} 张\n" + "\n".join(lines[:20]))

    @filter.command("表情删除")
    async def cmd_delete(self, event, emo: str = "", idx: str = ""):
        self.groups = self._scan()
        files = self.groups.get(emo.strip(), [])
        if not files:
            yield event.plain_result(f"「{emo}」这个情绪没有表情包")
            return
        try:
            i = int(idx) - 1
            path = files[i]
        except Exception:
            yield event.plain_result(f"要删第几张？比如：表情删除 {emo} 1（现在有 {len(files)} 张）")
            return
        try:
            os.remove(path)
            self.groups = self._scan()
            yield event.plain_result(f"已删除 {os.path.basename(path)}，「{emo}」还剩 {len(self.groups.get(emo, []))} 张")
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"删除失败：{e}")

    @filter.command("表情测试")
    async def cmd_face(self, event):
        names = [n for n in ["偷笑", "白眼", "呲牙", "捂脸", "吃瓜", "爱心"] if n in self.name2id]
        chain = [Plain("原生表情：")]
        for n in names:
            chain.append(Face(id=self.name2id[n]))
            chain.append(Plain(f"({n})"))
        yield event.chain_result(chain)

    @filter.command("表情包")
    async def cmd_pack(self, event):
        self.groups = self._scan()
        if not self.groups:
            yield event.plain_result("还没有表情包素材")
            return
        chain = [Plain("表情包：")]
        for e in random.sample(sorted(self.groups), min(6, len(self.groups))):
            p = self._pick_file(e)
            if p:
                chain.append(Image.fromFileSystem(p))
                chain.append(Plain(f"({e})"))
        yield event.chain_result(chain)
