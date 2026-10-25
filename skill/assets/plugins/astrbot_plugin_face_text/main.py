# -*- coding: utf-8 -*-
"""把 QQ 原生表情编号翻译成名称。

AstrBot 把 QQ 表情渲染成 `[表情:317]`，模型看到的是数字，无法理解。
本插件在请求 LLM 前，把 `[表情:317]` 替换成 `[表情:菜汪]`。
映射表来自 NapCat 自带的 QSid → QDes（329 条）。
"""

import json
import os
import re

from astrbot.api import star
from astrbot.api.event import filter
from astrbot.api.provider import ProviderRequest

FACE_RE = re.compile(r"\[表情:(\d+)\]")


class FaceTextPlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "face_map.json")
        with open(path, encoding="utf-8") as f:
            self.face_map = json.load(f)

    def _translate(self, text: str) -> str:
        if not text or "[表情:" not in text:
            return text

        def _rep(m):
            name = self.face_map.get(m.group(1))
            return f"[表情:{name}]" if name else m.group(0)

        return FACE_RE.sub(_rep, text)

    @filter.on_llm_request()
    async def translate_faces(self, event, req: ProviderRequest) -> None:
        try:
            prompt = getattr(req, "prompt", None)
            if isinstance(prompt, str):
                req.prompt = self._translate(prompt)

            contexts = getattr(req, "contexts", None)
            if isinstance(contexts, list):
                for msg in contexts:
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content")
                    if isinstance(content, str):
                        msg["content"] = self._translate(content)
                    elif isinstance(content, list):
                        for seg in content:
                            if isinstance(seg, dict) and seg.get("type") == "text":
                                seg["text"] = self._translate(seg.get("text") or "")
        except Exception:  # 插件不能影响主流程
            pass
