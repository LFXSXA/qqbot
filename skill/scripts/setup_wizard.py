#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置向导：5 个问题，把 10 个插件的配置一次写好。

用法：
    python3 setup_wizard.py --data /AstrBot/data           # 交互式
    python3 setup_wizard.py --data /AstrBot/data --defaults-only   # 只写默认值

它做的事：
    1. 写入 speak_up 的 target_user（他/她的 QQ 号）
    2. 写入人格到 data_v4.db
    3. 写入表情包/语音/搜索的配置
    4. 检查 emo 文件夹（表情包目录）
"""
import argparse
import json
import os
import sqlite3
import sys

DEFAULTS = {
    "target_user": "",
    "interval_min": 15,
    "active_gap_min": 12,
    "voice": "zh-CN-XiaoyiNeural",
    "auto_prob": 0.22,
    "sticker_cooldown_s": 90,
    "auto_chance": 0.2,
    "ggif_ratio": 0.6,
}


def ask(prompt, default=""):
    if default:
        s = input(f"{prompt}（默认 {default}）: ").strip()
        return s or default
    return input(f"{prompt}: ").strip()


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="AstrBot 的 data 目录")
    ap.add_argument("--defaults-only", action="store_true", help="不交互，全用默认")
    args = ap.parse_args()

    data = os.path.abspath(args.data)
    plugins = os.path.join(data, "plugins")
    cfgdir = os.path.join(data, "config")
    os.makedirs(plugins, exist_ok=True)
    os.makedirs(cfgdir, exist_ok=True)

    cfg = dict(DEFAULTS)
    persona_text = ""

    if not args.defaults_only:
        print("\n=== 配置向导（回车用默认值）===\n")
        cfg["target_user"] = ask("1) 你的 QQ 号（她主动搭话会发给他）", "")

        p = ask("2) 人格提示词文件路径（txt/md，留空则跳过）", "")
        if p and os.path.exists(p):
            persona_text = open(p, encoding="utf-8").read().strip()
            print(f"   读到人格 {len(persona_text)} 字")
        elif p:
            print("   文件不存在，跳过")

        cfg["interval_min"] = int(ask("3) 多久掷一次骰子（分钟）", "15") or 15)
        cfg["active_gap_min"] = int(ask("   刚聊过多少分钟内不插嘴", "12") or 12)

        print("4) 语音音色：")
        print("   1 = zh-CN-XiaoyiNeural（年轻）")
        print("   2 = zh-CN-XiaoxiaoNeural（温柔）")
        print("   3 = zh-CN-liaoning-XiaobeiNeural（东北）")
        sel = ask("   选哪个", "1")
        cfg["voice"] = {
            "1": "zh-CN-XiaoyiNeural",
            "2": "zh-CN-XiaoxiaoNeural",
            "3": "zh-CN-liaoning-XiaobeiNeural",
        }.get(sel, "zh-CN-XiaoyiNeural")

        cfg["auto_prob"] = float(ask("5) 每条回复配表情包的概率（0~1）", "0.22") or 0.22)

    # ---- 写 speak_up 配置 ----
    sp_path = os.path.join(plugins, "astrbot_plugin_speak_up", "config.json")
    sp = {
        "enable": bool(cfg["target_user"]),
        "dry_run": False,
        "target_user": cfg["target_user"],
        "interval_min": cfg["interval_min"],
        "cooldown_min": 12,
        "hit_peak": 55,
        "hit_off": 60,
        "miss_bonus": 8,
        "miss_bonus_cap": 25,
        "active_gap_min": cfg["active_gap_min"],
        "recent_min": 20,
        "recent_factor": 0.1,
        "cold_min": 240,
        "cold_factor": 1.6,
        "bili_chance": 0.25,
        "quiet_start": 1,
        "quiet_end": 7,
    }
    write_json(sp_path, sp)
    print(f"\n写入 {sp_path}")

    # ---- 写表情包配置 ----
    st_path = os.path.join(cfgdir, "astrbot_plugin_sticker_out_config.json")
    st = {
        "auto_prob": cfg["auto_prob"],
        "sticker_cooldown_s": cfg["sticker_cooldown_s"],
        "image_ratio": 0.75,
        "gif_ratio": cfg["ggif_ratio"],
        "online_enabled": True,
        "search_interval_min": 45,
        "max_new_per_round": 6,
        "min_pool_size": 5,
        "max_file_kb": 900,
        "repeat_cooldown_hours": 6,
    }
    if os.path.exists(st_path):
        try:
            st.update(json.load(open(st_path, encoding="utf-8-sig")))
        except Exception:
            pass
    write_json(st_path, st)
    print(f"写入 {st_path}")

    # ---- 写语音配置 ----
    vo_path = os.path.join(cfgdir, "astrbot_plugin_voice_config.json")
    write_json(vo_path, {
        "enabled": True,
        "auto_chance": cfg["auto_chance"],
        "voice": cfg["voice"],
        "rate": "+8%",
        "max_chars": 60,
        "keep_text": False,
    })
    print(f"写入 {vo_path}")

    # ---- 写人格 ----
    db = os.path.join(data, "data_v4.db")
    if persona_text and os.path.exists(db):
        try:
            con = sqlite3.connect(db)
            row = con.execute(
                "select count(*) from personas where persona_id='default'"
            ).fetchone()[0]
            if row:
                con.execute(
                    "update personas set system_prompt=? where persona_id='default'",
                    (persona_text,),
                )
            else:
                con.execute(
                    "insert into personas (persona_id, system_prompt, begin_dialogs) "
                    "values ('default', ?, '[]')",
                    (persona_text,),
                )
            con.commit()
            con.close()
            print(f"人格已写入数据库（{len(persona_text)} 字）")
        except Exception as e:  # noqa: BLE001
            print(f"写人格失败: {e}")
    elif persona_text:
        print(f"找不到 {db}，人格没写进去（请先启动一次 AstrBot 生成库）")

    # ---- 检查表情包目录 ----
    sd = os.path.join(data, "stickers")
    if os.path.isdir(sd):
        n = len([f for f in os.listdir(sd) if os.path.isfile(os.path.join(sd, f))])
        print(f"表情包目录: {sd}（{n} 张）")
        if n == 0:
            print("  !! 还没有表情包。文件名要用「情绪名」，例如 无语.gif、亲亲.png")
    else:
        print(f"!! 没有表情包目录 {sd}，表情功能会空转")

    print("\n完成。重启 AstrBot 后按 references/acceptance.md 验收。")


if __name__ == "__main__":
    main()
