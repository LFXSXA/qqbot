# 现成的 11 个插件

这套插件是「把人格跑起来」的完整实现。全部去掉了具体人的信息，只保留机制。

## 装法

```bash
# 一条命令（推荐）
bash install.sh /AstrBot/data

# 或者手动：把每个目录拷进 data/plugins/
cp -r assets/plugins/* /AstrBot/data/plugins/
```

装完**必须重启**：`docker restart astrbot`

看日志确认全部加载：

```bash
docker logs --since 2m astrbot 2>&1 | grep "Loading plugin"
# 应该出现 11 次
```

## 十一个插件分别干什么

| 插件 | 作用 | 需要什么 |
|---|---|---|
| `context` | **上下文聚合器**：统一拼情绪/记忆/视频/时间/日程，避免打架 | 无（读其它插件的状态文件） |
| `face_text` | QQ 表情编号 → 名字 | 无 |
| `sticker_out` | 按情绪配表情包，GIF 优先，联网补货 | `data/stickers/` 下有图 |
| `her_state` | 心情/精力/今天的事/陪伴模式/睡眠/日程 | 无（纯本地状态机）+ `schedule.json` |
| `memory` | 长期记忆：记他和记她自己 | 一个 LLM provider |
| `reconcile` | 9 个场景 + 吵架/分手再现 | 填 `scenes.json` 等三个数据文件 |
| `search` | 问正事时联网搜索再回答 | 能访问 bing.com |
| `video` | 发 B 站链接 → 发视频 | ffmpeg（AstrBot 镜像自带） |
| `voice` | 文字转语音（edge-tts） | `edge-tts` 装在 `data/site-packages` |
| `speak_up` | 主动搭话 + 早安 + 记住自己说过的话 | 填 `target_user` |
| `bili` | 关键词触发刷 B 站、读字幕 | 可选登录 cookie |

## 装完必须做的事

### 1. 填 `target_user`

`speak_up` 不知道给谁发消息，等于废的。

```bash
python3 scripts/setup_wizard.py --data /AstrBot/data
```

向导会问你 5 个问题，一次写好所有配置。

### 2. 准备表情包

`data/stickers/` 下的**文件名就是情绪**：

```
无语.gif
亲亲.png
亲亲2.png        ← 同情绪多张加数字后缀
想你__a1b2.gif   ← 也可以带哈希
```

插件按情绪分组，同一情绪里随机挑，有动图优先发动图。

**没有表情包时表情功能会空转**（不报错，只是不发）。

### 3. 填场景数据（`reconcile`）

三个文件都在 `plugins/astrbot_plugin_reconcile/`：

| 文件 | 填什么 |
|---|---|
| `scenes.json` | 9 个场景：什么话触发 / 她当时说过什么 / 上次错在哪 / 这次该怎么做 |
| `quarrel_topics.json` | 吵架由头：她真正在意什么 / 三句质问 / 真实反应 |
| `breakup.json` | 两处「把门关上」的时刻 + 如果重来说什么 |

**这三个文件决定了「她像不像那个人」**。空着的话场景功能只是不触发，不会报错。

## 音色

`voice` 用微软 Edge 的免费接口，四种中文音色：

```json
"voice": "zh-CN-XiaoyiNeural"
"voice": "zh-CN-XiaoxiaoNeural"
"voice": "zh-CN-liaoning-XiaobeiNeural"
"voice": "zh-CN-shaanxi-XiaoniNeural"
```

## 关键参数速查

| 想要的效果 | 改哪个 |
|---|---|
| 她主动说话更频繁 | `speak_up.interval_min` 调小、`hit_off` 调大 |
| 别刚聊完就插嘴 | `speak_up.active_gap_min` 调大 |
| 表情包少一点 | `sticker_out.auto_prob` 调小 |
| 她更常发语音 | `voice.auto_chance` 调大 |
| 主动提自己状态的次数 | `her_state.mention_chance` 调小 |
| 记忆提取频率 | `memory.extract_every` |

## 写自己的插件时

最小骨架：

```python
from astrbot.api import logger, star
from astrbot.api.event import filter

class MyPlugin(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)

    @filter.command("hello")
    async def hello(self, event):
        yield event.plain_result("hi")
```

三个最常用的钩子：

| 钩子 | 时机 | 能干什么 |
|---|---|---|
| `on_llm_request` | 调模型前 | 改 `req.system_prompt`、注入上下文 |
| `on_decorating_result` | 发出前 | 改 `result.chain`（加图片、换成语音） |
| `on_llm_response` | 模型回完 | ⚠️ 星型 pipeline 下**不触发**，别用它驱动主逻辑 |

最后一条是踩过的坑：记忆插件一开始用 `on_llm_response`，跑了几个小时一次都没触发。
