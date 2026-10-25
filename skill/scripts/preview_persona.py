#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线预览人格：不经过 QQ，直接调模型看效果。

为什么要离线预览：走 QQ 测试一轮要几十秒，而且看不到「她到底收到了什么提示」。
这个脚本把 人格 + 示例 + 待测句子 拼起来直接打模型，一次看十条。

用法：
    python preview_persona.py --base https://api.deepseek.com/v1 --key sk-xxx \
        --model deepseek-chat --persona persona.json

    # 或者直接从 AstrBot 的库里读
    python preview_persona.py --db data_v4.db \
        --base https://api.deepseek.com/v1 --key sk-xxx --model deepseek-chat

persona.json 格式（--db 时不需要）：
    {"system_prompt": "...", "begin_dialogs": ["用户第一句", "她第一句", ...]}

也可以把额外注入（情绪状态 / 记忆）追加进去：
    --extra state.txt     # 每轮都追加到 system
"""
import argparse
import json
import sys
import urllib.request

DEFAULT_TESTS = [
    "在干嘛呢",
    "亲亲",
    "亲亲",
    "我今天好累啊",
    "你怎么又忘了",
    "对不起嘛",
    "晚安",
]


def load_persona(args):
    if args.db:
        import sqlite3
        con = sqlite3.connect(args.db)
        row = con.execute(
            "select system_prompt, begin_dialogs from personas where persona_id=?",
            (args.persona_id,),
        ).fetchone()
        con.close()
        if not row:
            sys.exit(f"库里没有 persona_id={args.persona_id}")
        return row[0], json.loads(row[1] or "[]")
    data = json.load(open(args.persona, encoding="utf-8"))
    return data["system_prompt"], data.get("begin_dialogs") or []


def chat(base, key, model, messages, temperature=0.9, max_tokens=400):
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="OpenAI 兼容的 base_url")
    ap.add_argument("--key", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--persona", help="persona.json")
    ap.add_argument("--db", help="AstrBot 的 data_v4.db")
    ap.add_argument("--persona-id", default="default")
    ap.add_argument("--extra", help="额外注入的文本文件（情绪状态/记忆等）")
    ap.add_argument("--tests", nargs="*", help="自定义测试句")
    ap.add_argument("--temperature", type=float, default=0.9)
    args = ap.parse_args()

    prompt, begin = load_persona(args)
    extra = ""
    if args.extra:
        extra = "\n\n" + open(args.extra, encoding="utf-8").read().strip()

    tests = args.tests or DEFAULT_TESTS
    print(f"人格 {len(prompt)} 字 | 示例 {len(begin)} 条 | 测试 {len(tests)} 句\n")

    for t in tests:
        msgs = [{"role": "system", "content": prompt + extra}]
        for i, d in enumerate(begin):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": d})
        msgs.append({"role": "user", "content": t})
        try:
            print(f"你: {t}")
            print(f"  她: {chat(args.base, args.key, args.model, msgs, args.temperature)}\n")
        except Exception as e:  # noqa: BLE001
            print(f"你: {t}\n  失败: {e}\n")


if __name__ == "__main__":
    main()
