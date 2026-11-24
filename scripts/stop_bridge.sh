#!/bin/sh
# 停桥：先 SIGINT，停不下来再 SIGTERM，最后才 SIGKILL。
#
# 桥把 SIGINT / SIGTERM 都接成了"正常收尾"（reader 停 → 断开 follower 释放扭矩 →
# 关串口），所以前两步不会留下使能的舵机；只有 SIGKILL 才会。
#
# 为什么不能只发 SIGINT：**nohup 起的后台作业会继承「忽略 SIGINT」**
# （/proc/<pid>/status 里 SigIgn 有 0x2），kill -INT 发了等于没发。
# 以前只发 SIGINT，看上去停过，其实桥还在跑、串口还占着，
# 下一次 start_demo 就以 "Device or resource busy" 收场。
#
# 用法：bash /mnt/d/CUHKSZ/AIE3001/Project/scripts/stop_bridge.sh

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PIDFILE="$ROOT/.tmp/bridge.pid"

# 托住机械臂的提醒先打出来，别等臂软了才看见
echo "  发包前先托住机械臂 —— 桥退出会释放扭矩，臂会软下来"

alive() {
  out=""
  for p in $1; do
    if kill -0 "$p" 2>/dev/null; then out="$out $p"; fi
  done
  echo "$out" | sed 's/^ *//'
}

# pid 文件可能过期（桥重启过就换 pid 了），所以两条路都走：文件 + 按命令行找。
# 按命令行找要锚行首，否则 pgrep 会连调用它的那个 shell 一起匹配。
PIDS=""
if [ -f "$PIDFILE" ]; then
  P=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then PIDS="$P"; fi
  rm -f "$PIDFILE"
fi

NAMED=$(pgrep -f '^[^ ]*python -u robot/robot_bridge.py' 2>/dev/null || true)
for p in $NAMED; do
  case " $PIDS " in
    *" $p "*) ;;
    *) PIDS="$PIDS $p" ;;
  esac
done
PIDS=$(echo "$PIDS" | sed 's/^ *//')

if [ -z "$PIDS" ]; then
  echo "  没有在跑的桥"
  exit 0
fi

echo "  要停的桥：$PIDS"

for p in $PIDS; do kill -INT "$p" 2>/dev/null || true; done
LEFT=""
i=0
while [ $i -lt 25 ]; do
  LEFT=$(alive "$PIDS")
  if [ -z "$LEFT" ]; then break; fi
  sleep 0.2
  i=$((i + 1))
done

if [ -n "$LEFT" ]; then
  echo "  [!] SIGINT 没停下（$LEFT），改用 SIGTERM"
  TARGETS="$LEFT"
  for p in $TARGETS; do kill -TERM "$p" 2>/dev/null || true; done
  i=0
  while [ $i -lt 25 ]; do
    LEFT=$(alive "$TARGETS")
    if [ -z "$LEFT" ]; then break; fi
    sleep 0.2
    i=$((i + 1))
  done

  if [ -n "$LEFT" ]; then
    echo "  [!] 还在（$LEFT），最后用 SIGKILL —— 舵机可能留在使能状态，臂会硬"
    for p in $LEFT; do kill -KILL "$p" 2>/dev/null || true; done
    sleep 0.5
  fi
fi

echo "  桥已停"
