#!/usr/bin/env bash
# ============================================================
# persona-from-chatlogs 一键安装
#
# 用法：
#   bash install.sh                    # 默认装到 /AstrBot/data
#   bash install.sh /path/to/astrbot/data
#
# 前提：AstrBot 已经在跑（docker 或裸机都行）
# ============================================================
set -euo pipefail

DATA_DIR="${1:-/AstrBot/data}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUG_SRC="$HERE/../assets/plugins"
PLUG_DST="$DATA_DIR/plugins"
SITE="$DATA_DIR/site-packages"

echo "==> 数据目录: $DATA_DIR"
if [ ! -d "$DATA_DIR" ]; then
  echo "!! 找不到 $DATA_DIR，请先启动一次 AstrBot 或用参数指定"
  exit 1
fi

echo "==> 检查 Python"
PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  echo "!! 没找到 python3"
  exit 1
fi
echo "    $($PY -V)"

echo "==> 安装依赖到 $SITE（容器重启不丢）"
mkdir -p "$SITE"
$PY -m pip install --quiet --target "$SITE" edge-tts \
  -i https://pypi.tuna.tsinghua.edu.cn/simple || {
  echo "!! edge-tts 安装失败，语音功能将不可用（其它功能不受影响）"
}
$PY -c "import sys; sys.path.insert(0,'$SITE'); import edge_tts; print('    edge-tts OK')" || true

echo "==> 拷贝插件"
mkdir -p "$PLUG_DST"
for d in "$PLUG_SRC"/*/; do
  name=$(basename "$d")
  rm -rf "$PLUG_DST/$name"
  cp -r "$d" "$PLUG_DST/$name"
  echo "    + $name"
done

echo "==> 生成默认配置"
$PY "$HERE/setup_wizard.py" --data "$DATA_DIR" --defaults-only

cat <<'EOF'

============================================================
装完了。下一步：
  1. 跑配置向导（交互式，5 个问题）：
       python3 setup_wizard.py --data /AstrBot/data
  2. 重启 AstrBot：
       docker restart astrbot
  3. 看日志确认 10 个插件都加载了：
       docker logs --since 2m astrbot 2>&1 | grep "Loading plugin"
  4. 按 references/acceptance.md 逐条验收
============================================================
EOF
