#!/bin/sh
# 在 WSL 里起桥。由 launch.ps1 调用，也可以手工跑：
#
#   bash /mnt/d/CUHKSZ/AIE3001/Project/scripts/_bridge.sh hold
#
# 参数：hold（默认）/ teleop / keyframes
#
# 为什么要这一个包装层：PowerShell 往 wsl 里塞带引号的长命令行很容易被转义吃掉，
# 写成文件最省事；而且 setsid/nohup 的用法只在这一处出现。
#
# 桥自己会一直跑，直到 stop_bridge.sh 或 kill -INT。

set -e

MODE=${1:-hold}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=$HOME/miniconda3/envs/lerobot/bin/python
LOG="$ROOT/.tmp/bridge.log"

# 两个口是固定的（口不变，编号就不变）。换了线就改这两行。
FOLLOWER=${FOLLOWER:-/dev/ttyACM1}
LEADER=${LEADER:-/dev/ttyACM0}
FOLLOWER_ID=${FOLLOWER_ID:-zyf_follower}
LEADER_ID=${LEADER_ID:-zyf_leader}

mkdir -p "$ROOT/.tmp"

if [ ! -x "$PY" ]; then
  echo "找不到 $PY —— 检查 WSL 里那个 conda 环境（应该叫 lerobot）" >&2
  exit 2
fi
if [ ! -e "$FOLLOWER" ]; then
  echo "找不到 $FOLLOWER —— 串口没挂进 WSL（usbipd attach），或者编号变了" >&2
  exit 3
fi

cd "$ROOT" || exit 1
# nohup + 后台：wsl.exe 这个外壳退出后，桥要继续活着。
# stdin 明确给 /dev/null：桥里连 leader 时 lerobot 会问「用已有校准文件还是重新校准」，
# 没有 tty 又没有重定向的话，那句提问能把整个启动卡住。
nohup "$PY" -u robot/robot_bridge.py \
  --port "$FOLLOWER" --id "$FOLLOWER_ID" \
  --leader-port "$LEADER" --leader-id "$LEADER_ID" \
  --drive "$MODE" \
  < /dev/null > "$LOG" 2>&1 &

echo $! > "$ROOT/.tmp/bridge.pid"
PID=$(cat "$ROOT/.tmp/bridge.pid")

# 等它把两个串口都连上再放行。只测 8765 通不通是不够的：
# leader 没连上时桥照样占着端口、照常推帧，页面一切正常，只有遥操不动 —— 那种故障查起来最费劲。
i=0
while [ $i -lt 40 ]; do
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  if grep -q "遥操源" "$LOG" 2>/dev/null; then break; fi
  sleep 0.2
  i=$((i + 1))
done

if ! kill -0 "$PID" 2>/dev/null; then
  echo "桥进程退出了（pid $PID）。日志最后几行：" >&2
  tail -15 "$LOG" >&2
  exit 4
fi

if grep -q "遥操源.*未打开" "$LOG" 2>/dev/null; then
  echo "桥起来了，但 leader 没连上：屏幕能演，遥操档不会动。" >&2
  grep -E "leader 连不上|\[!\]|查 --leader-port" "$LOG" 2>/dev/null | tail -3 >&2 || true
  exit 5
fi

echo "bridge pid $PID  mode=$MODE  log=$LOG"
grep -E "遥操源|驱动" "$LOG" 2>/dev/null | sed 's/^/  /' || true
