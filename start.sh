#!/usr/bin/env bash
# 一键启动 harness 前后端（开发模式）。
# 前置：pip install -r requirements.txt && cd frontend && npm install
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# 优先使用隔离 venv 的 python（含 fastapi/langgraph/uvicorn）
if [ -x "/Users/xjh/.workbuddy/binaries/python/envs/default/bin/python" ]; then
  PYTHON="/Users/xjh/.workbuddy/binaries/python/envs/default/bin/python"
else
  PYTHON="$(command -v python3 || command -v python)"
fi
# 优先使用隔离 node（含 npm）
if [ -d "/Users/xjh/.workbuddy/binaries/node/versions/22.22.2/bin" ]; then
  export PATH="/Users/xjh/.workbuddy/binaries/node/versions/22.22.2/bin:$PATH"
fi

# 注入网络代理：确保后端进程及其 exec 子进程能走代理访问外部服务（如 wttr.in）。
# 若环境变量已显式设置则沿用；否则在 macOS 上尝试从系统代理（VPN / 网络偏好）读取。
if [ -z "$HTTP_PROXY" ] && [ -z "$HTTPS_PROXY" ]; then
  if command -v scutil >/dev/null 2>&1; then
    _pi="$(scutil --proxy 2>/dev/null)"
    _hp="$(printf '%s' "$_pi" | awk -F': ' '/HTTPProxy /{print $2}' | head -1)"
    _hpo="$(printf '%s' "$_pi" | awk -F': ' '/HTTPPort /{print $2}' | head -1)"
    if [ -n "$_hp" ] && [ -n "$_hpo" ]; then
      export HTTP_PROXY="http://$_hp:$_hpo"
      export HTTPS_PROXY="http://$_hp:$_hpo"
      export http_proxy="$HTTP_PROXY"
      export https_proxy="$HTTPS_PROXY"
      echo "==> 检测到系统代理，已注入后端环境: $HTTP_PROXY"
    fi
  fi
else
  echo "==> 沿用已有代理环境变量: ${HTTP_PROXY:-$HTTPS_PROXY}"
fi
echo "==> Python: $($PYTHON --version 2>&1) | Node: $(node -v 2>&1)"

# 清理旧实例：必须先杀掉端口 8123 上已有的后端，以及上一轮遗留的 vite 前端。
# 否则每次启动都会新开一个 uvicorn，旧进程变孤儿并持续持有
# ~/.agent-harness/checkpoints.sqlite —— 多实例共享同一 sqlite 会造成跨进程锁竞争，
# 表现为聊天间歇性卡死（几十秒无响应）。这是历史卡顿的根因。
echo "==> 清理已有实例（按端口/进程）..."
for p in 8123 5173 5174 5175 5176 5177 5178 5179 5180 5181 5182 5183 5184 5185; do
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:$p -sTCP:LISTEN 2>/dev/null | xargs -r kill -9 2>/dev/null
  fi
done
pkill -9 -f "uvicorn src.server.app" 2>/dev/null || true
pkill -9 -f "node.*vite" 2>/dev/null || true
sleep 1

echo "==> 启动后端 (http://127.0.0.1:8123)"
PYTHONPATH="$ROOT" "$PYTHON" -m uvicorn src.server.app:app --host 127.0.0.1 --port 8123 --log-level warning &
BACK_PID=$!

echo "==> 启动前端 (http://localhost:5173)"
cd "$ROOT/frontend"
npm run dev &
FRONT_PID=$!

echo ""
echo "后端 PID=$BACK_PID  前端 PID=$FRONT_PID"
echo "打开 http://localhost:5173  （后端 API: http://127.0.0.1:8123）"
echo "Ctrl+C 退出"
trap "kill $BACK_PID $FRONT_PID 2>/dev/null" EXIT INT TERM
wait
