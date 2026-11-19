#!/bin/sh
# 停桥：发 SIGINT 而不是 SIGKILL。
#
# 桥收到 KeyboardInterrupt 会走 finally：断开 follower（释放扭矩）、断开 leader、
# 关串口。直接 kill -9 会留下使能的舵机，下一次 connect 可能报 "enable torque timeout"。
#
# 用法：bash /mnt/d/CUHKSZ/AIE3001/Project/scripts/stop_bridge.sh

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PIDFILE="$ROOT/.tmp/bridge.pid"

# 托住机械臂的提醒先打出来，别等臂软了才看见
echo "  发包前先托住机械臂 —— 桥退出会释放扭矩，臂会软下来"

if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill -INT "$PID" 2>/dev/null || true
    # 等它自己收尾（最多 5 秒），再兜底
    i=0
    while kill -0 "$PID" 2>/dev/null && [ $i -lt 25 ]; do
      sleep 0.2
      i=$((i + 1))
    done
    if kill -0 "$PID" 2>/dev/null; then
      echo "  [!] $PID 没在 5 秒内退出，改用 SIGTERM"
      kill -TERM "$PID" 2>/dev/null || true
    else
      echo "  桥已退出（pid $PID）"
    fi
  else
    echo "  pid $PID 不在了"
  fi
  rm -f "$PIDFILE"
else
  # 没有 pid 文件（比如手工起的桥）：按名字找，仍然先 SIGINT
  PIDS=$(pgrep -f "robot/robot_bridge.py" || true)
  if [ -n "$PIDS" ]; then
    echo "  没有 pid 文件，按进程名停：$PIDS"
    for p in $PIDS; do kill -INT "$p" 2>/dev/null || true; done
    sleep 1
  else
    echo "  没有在跑的桥"
  fi
fi
