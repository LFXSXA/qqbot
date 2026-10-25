#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从聊天记录里自动挖出「场景修复」需要的素材，生成三个可编辑的草稿：

    scenes.json          —— 什么话触发 / 她当时说过什么 / 上次错在哪
    quarrel_topics.json  —— 吵架由头（她真正在意什么 / 三句质问）
    breakup.json         —— 分手/关门时刻 + 如果重来说什么

用法：
    python mine_scenes.py chat.txt --me 我 --her 她 --out ./outdir

输入格式（每行一条，和 analyze_corpus.py 一致）：
    [2024-03-15 21:04] 她: 晚安
    [2024-03-15 21:04] 我: 早点睡

输出：outdir/ 下三个 json，都是「半成品」——用户要自己改 evidence 和 hint，
但结构、触发词、占位说明都替用户填好了。
"""
import argparse
import json
import os
import re
from collections import Counter

LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\]\s*([^:：]{1,20})[:：]\s*(.*)$")

# 场景分类：关键词 → 场景 id（触发判断用）
SCENE_KEYWORDS = {
    "still_like_me": ["还喜欢我", "不喜欢我了", "还爱我", "是不是不要我"],
    "apology": ["对不起", "我错了", "抱歉", "别生气", "原谅我"],
    "breakup": ["分手", "只是朋友", "别联系", "做回朋友"],
    "missing": ["想你了", "好想你", "想我没"],
    "sad": ["好累", "难过", "不开心", "撑不住", "想哭"],
    "distance": ["忙", "晚点回", "不说了", "先这样"],
    "quarrel": ["你怎么又", "你就是不", "你根本", "算了不说了", "随便你"],
    "family_debt": ["我爸", "还钱", "信用卡", "逾期", "欠", "心寒"],
    "left_on_read": ["没回", "不回消息", "冷暴力", "不理我", "忙"],
}

# 深情/和解信号（用来挑 evidence）
TENDER = ["想你", "抱抱", "亲", "爱你", "晚安", "别气", "没事", "对不起", "我错了",
          "舍不得", "想你", "在", "陪你", "乖"]


def parse(path):
    out = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            m = LINE_RE.match(raw.rstrip("\n"))
            if m:
                out.append({"t": m.group(1), "who": m.group(2).strip(), "text": m.group(3)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chat")
    ap.add_argument("--me", default="我")
    ap.add_argument("--her", default="她")
    ap.add_argument("--out", default="scenes_out")
    args = ap.parse_args()

    msgs = parse(args.chat)
    hers = [m for m in msgs if m["who"] == args.her]
    if not hers:
        print(f"没找到说话人「{args.her}」，实际出现：",
              Counter(m["who"] for m in msgs).most_common(5))
        return

    os.makedirs(args.out, exist_ok=True)

    # ---------- scenes.json ----------
    scenes = []
    for sid, kws in SCENE_KEYWORDS.items():
        # 找她在这个场景下的发言
        ev = []
        for i, m in enumerate(msgs):
            if m["who"] == args.her and any(k in m["text"] for k in kws):
                t = m["text"].strip()
                if 2 <= len(t) <= 20 and t not in ev:
                    ev.append(t)
        scenes.append({
            "id": sid,
            "name": f"（请改成你的描述）关键词 {'/'.join(kws[:3])}",
            "trigger": kws,
            "evidence": ev[:6] if ev else ["<她在这个场景真实说过的原话>"],
            "avoid": "<记录里那次她做错的说法，写在这里>",
            "hint": "<这次应该怎么做，一两句>",
        })
    json.dump({"scenes": scenes}, open(os.path.join(args.out, "scenes.json"), "w",
                                        encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---------- quarrel_topics.json ----------
    # 找冲突：她连发多条负面短句的段落
    neg = ["生气", "不理", "滚", "烦", "算了", "失望", "凭什么", "敷衍", "冷战",
           "不想说", "心寒", "委屈", "难过", "哭", "别联系", "受够"]
    topics = []
    i = 0
    while i < len(msgs):
        win = msgs[i:i + 10]
        hit = [m for m in win if m["who"] == args.her and any(k in m["text"] for k in neg)]
        if len(hit) >= 2:
            # 取这一段里她最强的几句话
            strong = [m["text"].strip() for m in hit if 2 <= len(m["text"].strip()) <= 24][:3]
            topics.append({
                "id": f"topic_{len(topics) + 1}",
                "weight": 1,
                "subject": "<简述他做了什么>",
                "core": "<她真正在意的不是表面这件事，而是……>",
                "opener": strong[0] if strong else "<她第一句质问>",
                "follow": strong[1] if len(strong) > 1 else "<第二句加压>",
                "pressure": strong[2] if len(strong) > 2 else "<第三句戳到点子上>",
                "real": " / ".join(strong) if strong else "<她当时真实说过的话>",
                "when": hit[0]["t"][:10],
            })
            i += 10
        else:
            i += 1
    json.dump({"topics": topics or [{
        "id": "example_1", "weight": 5, "subject": "<他做过的事>",
        "core": "<她真正在意什么>", "opener": "<第一句>", "follow": "<第二句>",
        "pressure": "<第三句>", "real": "<真实反应>", "when": "<日期>",
    }]}, open(os.path.join(args.out, "quarrel_topics.json"), "w",
             encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---------- breakup.json ----------
    # 找「关门」时刻：她说到 分手/只是朋友/别想这些/就这样
    close = ["分手", "只是朋友", "别想这些", "就这样", "别联系", "不想知道"]
    moments = []
    for i, m in enumerate(msgs):
        if m["who"] == args.her and any(k in m["text"] for k in close):
            ctx = msgs[max(0, i - 4): i + 4]
            moments.append({
                "when": m["t"][:16],
                "transcript": [{"who": "你" if x["who"] == args.me else "她",
                                "text": x["text"][:40]} for x in ctx],
            })
    breakout = [{
        "id": f"moment_{n + 1}",
        "when": mo["when"],
        "title": "（请改成一句话）",
        "transcript": mo["transcript"],
        "real": "<她当时真实的那句关门的话>",
        "banned": ["<她当时用来回避/堵死话的词>"],
        "repair_hint": "<如果重来一次，她应该怎么做，写出必须做到的两点>",
    } for n, mo in enumerate(moments[:4])]
    if not breakout:
        breakout = [{
            "id": "example", "when": "<日期>", "title": "<那一刻>",
            "transcript": [{"who": "你", "text": "<他说的话>"}, {"who": "她", "text": "<她说的>"}],
            "real": "<她当时的回应>", "banned": ["<回避词>"],
            "repair_hint": "<如果重来该怎么做>",
        }]
    json.dump({"scenes": breakout}, open(os.path.join(args.out, "breakup.json"), "w",
                                          encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"已生成到 {args.out}/：")
    print(f"  scenes.json         {len(scenes)} 个场景（evidence 已按关键词捞出候选）")
    print(f"  quarrel_topics.json {len(topics)} 个吵架候选（真实台词已填入）")
    print(f"  breakup.json        {len(breakout)} 个关门时刻（原话上下文已填）")
    print("\n下一步：打开这三个文件，把 <...> 占位符改掉，evidence/real 已经帮你从记录里捞了候选。")


if __name__ == "__main__":
    main()
