# AstrBot 运维速查

> 只写「实战里真的会用到的」，不重复官方文档。

## 1. 目录结构（Docker 部署）

```
/root/astrbot-deploy/
├── docker-compose.yml
├── data/                      ← 挂到容器的 /AstrBot/data
│   ├── cmd_config.json        ← 主配置（provider / 平台 / 人格绑定）
│   ├── data_v4.db             ← 人格 + 会话（SQLite）
│   ├── config/
│   │   └── <插件名>_config.json   ← 插件配置（有 _conf_schema.json 才会生成）
│   ├── plugins/               ← 插件目录
│   └── stickers/              ← 表情包（本 skill 的约定）
├── napcat/config/             ← NapCat 配置（onebot11 / webui）
└── ntqq/                      ← QQ 登录态（删了就要重新扫码）
```

## 2. 人格操作

人格存在 `data_v4.db` 的 `personas` 表。**改完必须重启容器**。

```bash
# 看当前人格字数
docker exec astrbot python -c "
import sqlite3
c=sqlite3.connect('/AstrBot/data/data_v4.db')
print(c.execute(\"select length(system_prompt), length(begin_dialogs) from personas\").fetchone())
"

# 备份（改之前一定要做）
cp /root/astrbot-deploy/data/data_v4.db /root/persona-backup-$(date +%H%M).db

# 重启（人格重新加载）
cd /root/astrbot-deploy && docker restart astrbot
```

⚠️ **不要用 `docker cp` 直接覆盖 db 文件**——容器里是 WAL 模式，正在写。
要么停容器再覆盖，要么用 sqlite 命令改。

## 3. 插件热重载（不重启）

在面板改完插件代码后，用 API 重载单个插件，**不影响 QQ 连接**：

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:6185/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"astrbot","password":"<你的密码>"}' | jq -r .data.token)

curl -s -X POST http://127.0.0.1:6185/api/plugin/reload \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"astrbot_plugin_xxx"}'
```

**安装新插件**（不用重启）：

```bash
# 关键：zip 里必须用正斜杠！Windows 的 Compress-Archive 会写成反斜杠，服务器解压会失败
python - <<'EOF'
import zipfile, os
name = "astrbot_plugin_xxx"
with zipfile.ZipFile(f"{name}.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for fn in os.listdir(name):
        z.write(os.path.join(name, fn), arcname=f"{name}/{fn}")
EOF

curl -s -X POST http://127.0.0.1:6185/api/plugin/install-upload \
  -H "Authorization: Bearer $TOKEN" -F "file=@astrbot_plugin_xxx.zip"
```

## 4. 日志

```bash
docker logs -f astrbot                      # 跟日志
docker logs astrbot 2>&1 | grep -E "ERROR|Traceback"
docker logs astrbot 2>&1 | grep "适配器已连接" | tail -1   # 确认连上 QQ 的时间
```

日志里的 `[插件名]` 前缀是你自己 `logger.info` 打的——**每个插件都应该在关键路径打点**，
否则出问题时只能靠猜。

## 5. 常见现象对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 消息不回复 | 适配器没连上 | 看 `适配器已连接`，没有就重启 napcat |
| 回复变长变生硬 | 上下文里旧回复污染 | `delete from conversations` + 调小 `max_turns` |
| 人格没变化 | 只改库没重启 | `docker restart astrbot` |
| 插件改了没生效 | 只上传没 reload | 调 `/api/plugin/reload` |
| 插件重复回消息 | 多次 reload 留下旧后台循环 | 见 pitfalls「循环泄漏」 |

## 6. 关键配置项

`data/cmd_config.json`：

| 路径 | 作用 |
|---|---|
| `provider[].key` / `api_base` | 模型服务（**注意 key 可能在 provider 层，不在 source 层**） |
| `provider[].modalities` | 设 `["text"]` 才是纯文本模型；空数组会被当成支持图片 |
| `agent_runner.config.persona.persona_id` | 用哪个人格 |
| `agent_runner.config.compression.max_turns` | 带多少轮上下文 |
| `platform[].ws_reverse_host` | 容器里必须 `0.0.0.0` |
| `dashboard.host` | 同上 |

## 7. 面板密码

```bash
docker exec -it astrbot astrbot password            # 改密码
docker exec -it astrbot astrbot password --username <新用户名>
```

改完要重启容器才生效。
