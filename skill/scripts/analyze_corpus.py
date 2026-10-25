#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语料统计：把一个「时间 + 说话人 + 内容」的聊天记录导出，量化成写人设要用的数字。

用法：
    python analyze_corpus.py chat.txt --me 我 --her 她
    python analyze_corpus.py chat.txt --me 我 --her 她 --json out.json

输入格式（每行一条）：
    [2024-03-15 21:04] 她: 嗯嗯
    [2024-03-15 21:04] 我: 早点睡

输出：
    句长分布 / 结尾字 / 语气词 / 称呼 / 高频词 / 情绪与冷淡句 / 冲突片段
"""
import argparse
import json
import re
import sys
from collections import Counter

LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\]\s*([^:：]{1,20})[:：]\s*(.*)$")

# 负面 / 冲突
NEG = [
    "生气", "不理", "滚", "烦", "讨厌", "吵", "分手", "算了", "失望", "凭什么",
    "敷衍", "冷战", "不想说", "伤心", "难过", "哭", "委屈", "累", "没意思",
    "随便", "呵呵", "别找我", "拉黑", "删除",
]
# 冷淡短句（很多人不高兴时不吵架，只是变短）
COLD = ["哦", "好哦", "哦哦", "没什么", "算了", "还是算了", "太累了", "好累",
        "不来了", "不想说", "睡了", "不聊了", "随便"]
# 亲密
INTIMATE = ["想你", "爱你", "喜欢", "抱", "亲", "宝", "老公", "老婆", "么么",
            "乖", "晚安", "舍不得", "惦记", "抱抱", "贴贴"]


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
    ap.add_argument("path")
    ap.add_argument("--me", default="我", help="你的显示名")
    ap.add_argument("--her", default="她", help="对方显示名")
    ap.add_argument("--json", help="把结果另存为 json")
    args = ap.parse_args()

    msgs = parse(args.path)
    hers = [m for m in msgs if m["who"] == args.her]
    if not hers:
        print(f"没找到说话人「{args.her}」，实际出现的名字：",
              Counter(m["who"] for m in msgs).most_common(5))
        sys.exit(1)

    txts = [m["text"] for m in hers if m["text"].strip()]
    lens = [len(t) for t in txts]
    res = {"total": len(msgs), "hers": len(hers)}

    print(f"总消息 {len(msgs)}，她 {len(hers)}")
    print(f"\n=== 句长 ===")
    avg = sum(lens) / len(lens)
    p4 = sum(1 for l in lens if l <= 4) / len(lens) * 100
    p10 = sum(1 for l in lens if 4 < l <= 10) / len(lens) * 100
    p_long = sum(1 for l in lens if l > 10) / len(lens) * 100
    print(f"平均 {avg:.1f} 字 | ≤4字 {p4:.0f}% | 5-10字 {p10:.0f}% | >10字 {p_long:.0f}%")
    res.update(avg_len=round(avg, 1), p_le4=round(p4), p_mid=round(p10), p_long=round(p_long))

    endp = sum(1 for t in txts if t.rstrip()[-1:] in "。！？!?…") / len(txts) * 100
    print(f"带句末标点：{endp:.0f}%")

    tails = Counter(t.rstrip()[-1] for t in txts if t.strip())
    print("结尾字 top10:", " ".join(f"{c}({n})" for c, n in tails.most_common(10)))

    parts = Counter()
    for t in txts:
        for p in re.findall(r"[啦喽呀哦噢呗嘛咯么哈呢吧嘞嗷~～]", t):
            parts[p] += 1
    print("语气词:", " ".join(f"{p}({n})" for p, n in parts.most_common(12)))

    words = Counter()
    for t in txts:
        for w in re.findall(r"[\u4e00-\u9fa5]{2,4}", t):
            words[w] += 1
    print("高频词 top25:", " ".join(f"{w}({n})" for w, n in words.most_common(25)))

    calls = Counter()
    for name in ["宝", "宝宝", "宝贝", "小宝", "亲爱的", "老公", "老婆", "你"]:
        c = sum(1 for t in txts if name in t)
        if c:
            calls[name] = c
    print("称呼出现次数:", calls.most_common())

    neg = [m for m in hers if any(k in m["text"] for k in NEG)]
    print(f"\n=== 情绪 ===")
    print(f"负面相关 {len(neg)} 条 ({len(neg)/len(hers)*100:.1f}%)")
    if neg:
        nl = [len(m["text"]) for m in neg]
        print(f"  负面句平均 {sum(nl)/len(nl):.1f} 字，≤4字占 {sum(1 for l in nl if l<=4)/len(nl)*100:.0f}%")
        nt = Counter(m["text"].rstrip()[-1] for m in neg if m["text"].strip())
        print("  负面句结尾:", " ".join(f"{c}({n})" for c, n in nt.most_common(6)))

    cold = [m["text"].strip() for m in hers if m["text"].strip() in COLD]
    if cold:
        print(f"冷淡短句 {len(cold)} 次:", " ".join(f"{k}×{v}" for k, v in Counter(cold).most_common(10)))
        print("  ↑ 如果这个数字不小，说明她生气时是「变短变冷」而不是吵架")

    inti = [m for m in hers if any(k in m["text"] for k in INTIMATE)]
    print(f"亲密相关 {len(inti)} 条 ({len(inti)/len(hers)*100:.1f}%)")

    # 冲突片段
    segs, i = [], 0
    while i < len(msgs):
        win = msgs[i:i + 6]
        bad = sum(1 for x in win if x["who"] == args.her and any(k in x["text"] for k in NEG))
        if bad >= 2:
            segs.append(win)
            i += 6
        else:
            i += 1
    print(f"\n=== 冲突/低落片段 {len(segs)} 段（看前 8 段）===")
    for seg in segs[:8]:
        print("─" * 36)
        for x in seg:
            who = "你" if x["who"] == args.me else "她"
            print(f"  {x['t'][5:]} {who}: {x['text'][:52]}")

    if args.json:
        res["tail_chars"] = tails.most_common(10)
        res["particles"] = parts.most_common(12)
        res["top_words"] = words.most_common(25)
        res["calls"] = calls.most_common()
        res["neg_count"] = len(neg)
        res["cold_lines"] = Counter(cold).most_common(10)
        json.dump(res, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n已写入 {args.json}")


if __name__ == "__main__":
    main()
