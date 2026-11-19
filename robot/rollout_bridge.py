#!/usr/bin/env python3
"""推理时的状态桥：把 `lerobot-rollout` 跑出来的实机动作实时推给数字层。

问题：`lerobot-rollout` 独占 follower 串口，`robot_bridge.py` 拿不到。而且串口是点对点的，
没法旁听。所以只能**在推理进程里面**取数 —— 这个脚本就是这么干的：

    自己起一个 WS 服务（默认 :8765，68 字节帧，与 robot_bridge 完全一致）
    → 给 lerobot 的 SO101Follower.get_observation 挂一个钩子
    → 直接调 lerobot 的 rollout 入口，参数原样透传

推理逻辑一行没改：钩子只读观测、转帧、推出去，失败不抛给推理。

用法（WSL，conda env lerobot）——把原来 `lerobot-rollout` 的整行参数照抄过来，只把命令名换掉：

    python /mnt/d/CUHKSZ/AIE3001/Project/robot/rollout_bridge.py \\
        --strategy.type=base \\
        --policy.path=TrebleY/act_pickplace_v2 --policy.device=cuda \\
        --robot.type=so101_follower --robot.port=/dev/ttyACM0 --robot.id=zyf_follower \\
        --robot.cameras='{"overhead":{...},"wrist":{...}}' \\
        --task="pick and place" --duration=10

本脚本自己的参数（都在前面，与 rollout 的不冲突）：

    --ws-port 8765        推帧端口（twin 连这个）
    --push-hz 30          推帧频率
    --offsets <path>      零位偏移，默认 robot/offsets.json
    --repeat 1            连续跑几轮（每轮 = 一次完整 rollout）。想反复演示就写 --repeat 10
    --repeat-gap 1        两轮之间停几秒
    --quiet               不每 5 秒打进度

想让它一直跑不停：用 `--duration=0`（lerobot 的语义是无限跑），或者 `--repeat 10 --duration=10`。

`--robot.cameras` 必须是**合法 JSON**，别把示例里的占位符直接跑；写错会被本脚本先拦下来并给正确样例
（lerobot 自己的报错只会说 "robot: ... got None"，指不到是哪一项）。

页面：`http://127.0.0.1:8000/`（后端 `--source remote --remote-url ws://localhost:8765`）。
"""

import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import robot_bridge as rb  # noqa: E402  复用换算、Latest、Bridge、offsets 读取

# 同一个观测被两条路径各读一次时（包装类 + 具体类），只推一次
PUSH_GAP = 0.003

# 候选类名。版本之间 so101 可能是 so100 的子类或别名，靠 _twin_hooked 标记去重
CANDIDATES = ("SO101Follower", "SO100Follower")


class Publisher:
    """钩子状态。独立成类是为了让测试和不装 lerobot 的机器也能 import 本模块。"""

    def __init__(self, latest, quiet=False):
        self.latest = latest
        self.quiet = quiet
        self.frames = 0
        self._last = 0.0
        self._warned = False

    def __call__(self, obs):
        now = time.monotonic()
        if now - self._last < PUSH_GAP:
            return
        self._last = now
        try:
            self.latest.put(obs)
            self.frames += 1
        except Exception as exc:  # noqa: BLE001  观测结构变了也不能把推理带崩
            if not self._warned:
                self._warned = True
                print(f"  [!] 观测转帧失败（推理不受影响）：{exc!r}", flush=True)


# 钩子只装一次，但推给谁由 main() 决定 —— 同进程里重跑也能换到新的 publisher
_ACTIVE = {"publisher": None}


def _dispatch(obs):
    publisher = _ACTIVE["publisher"]
    if publisher is not None:
        publisher(obs)


def install_hook():
    """给 lerobot 的 follower 类挂 get_observation 钩子。返回挂上的类名列表。

    已经挂过（`_twin_hooked`）的算挂上，不再套第二层。
    """
    import importlib

    try:
        mod = importlib.import_module("lerobot.robots.so_follower.so_follower")
    except ImportError:
        mod = importlib.import_module("lerobot.robots.so_follower")

    done = []
    for name in CANDIDATES:
        cls = getattr(mod, name, None)
        if cls is None or not hasattr(cls, "get_observation"):
            continue
        if cls in done:
            continue
        orig = cls.get_observation
        if getattr(orig, "_twin_hooked", False):
            done.append(cls)  # 之前就挂过了
            continue

        def get_observation(self, *a, _orig=orig, **kw):
            obs = _orig(self, *a, **kw)
            _dispatch(obs)
            return obs

        get_observation._twin_hooked = True
        cls.get_observation = get_observation
        done.append(cls)
    return [cls.__name__ for cls in done]


def start_server(latest, host, port, push_hz, timeout=5.0):
    """后台线程跑桥的 WS 服务。和 robot_bridge 用的是同一个 Bridge，帧格式一致。"""
    ready = threading.Event()
    holder = {}

    def runner():
        from websockets.asyncio.server import serve

        async def serve_forever():
            bridge = rb.Bridge(latest, push_hz)
            server = await serve(bridge.handler, host, port, max_size=1 << 20)
            holder["bridge"] = bridge
            ready.set()
            try:
                await bridge.pump()
            finally:
                server.close()
                await server.wait_closed()

        try:
            asyncio.run(serve_forever())
        except Exception as exc:  # noqa: BLE001  端口被占之类
            holder["error"] = exc
            ready.set()

    t = threading.Thread(target=runner, name="bridge-ws", daemon=True)
    t.start()
    ready.wait(timeout)
    if "error" in holder:
        raise holder["error"]
    return t


def parse_own_args(argv):
    """只认自己的参数，其余原样留给 lerobot 的 rollout 解析器。"""
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--ws-port", dest="ws_port", type=int, default=8765)
    ap.add_argument("--ws-host", dest="ws_host", default="0.0.0.0")
    ap.add_argument("--push-hz", dest="push_hz", type=float, default=30.0)
    ap.add_argument("--offsets", default=str(rb.DEFAULT_OFFSETS))
    ap.add_argument("--repeat", type=int, default=1, help="连续跑几轮推理（每轮就是一次完整的 rollout）")
    ap.add_argument("--repeat-gap", dest="repeat_gap", type=float, default=1.0, help="两轮之间停几秒")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("-h", "--help", action="store_true", dest="help_own")
    ours, rest = ap.parse_known_args(argv)
    return ours, rest


CAMERAS_HINT = (
    '{"overhead":{"type":"opencv","index_or_path":0,"fps":30,"width":640,"height":480,"fourcc":"MJPG"},'
    '"wrist":{"type":"opencv","index_or_path":2,"fps":30,"width":640,"height":480,"fourcc":"MJPG"}}'
)


def check_cameras(rest):
    """`--robot.cameras` 必须是合法 JSON。

    lerobot 的解析器遇到坏值只会说 "robot: Could not decode ... got None"，
    完全指不到是哪一项 —— 2026-10-27 就是因为把示例里的 `{...原样...}` 直接跑了一遍。
    """
    for i, arg in enumerate(rest):
        if not arg.startswith("--robot.cameras"):
            continue
        value = arg.split("=", 1)[1] if "=" in arg else (rest[i + 1] if i + 1 < len(rest) else "")
        try:
            json.loads(value)
        except Exception:  # noqa: BLE001
            print("  [!] --robot.cameras 不是合法 JSON：")
            print(f"      {value[:120]}")
            print("      相机名必须是 overhead / wrist，索引按「相机确认」那步的结果填。例如：")
            print(f"      --robot.cameras='{CAMERAS_HINT}'")
            return False
        return True
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ours, rest = parse_own_args(argv)

    if ours.help_own:
        print(__doc__)
        return 0

    if not check_cameras(rest):
        return 2

    offsets, found = rb.load_offsets(Path(ours.offsets))
    if not found:
        print(f"  [!] 没找到 {ours.offsets}，零位偏移按全 0（数字层姿态可能整体偏几十度）", flush=True)

    latest = rb.Latest(offsets)
    try:
        start_server(latest, ours.ws_host, ours.ws_port, ours.push_hz)
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] WS 服务起不来（{exc!r}）。端口被占？先停掉 robot_bridge 或换 --ws-port")
        return 1

    publisher = Publisher(latest, ours.quiet)
    _ACTIVE["publisher"] = publisher
    hooked = install_hook()
    if not hooked:
        print("  [!] 没找到 SO101Follower / SO100Follower，钩子挂不上（lerobot 版本变了？）")
        return 1

    print(f"推理桥就绪  ws://{ours.ws_host}:{ours.ws_port}  （{ours.push_hz:g}Hz 推帧）")
    print(f"  钩子      {' / '.join(hooked)}.get_observation")
    print(f"  零位偏移  {ours.offsets if found else '全 0（文件不存在）'}")
    print(f"  轮次      {ours.repeat} 轮{'（每轮之间停 %g 秒）' % ours.repeat_gap if ours.repeat > 1 else ''}")
    print(f"  接下来把 lerobot-rollout 的参数原样传进来（已收 {len(rest)} 个）\n", flush=True)

    from lerobot.scripts.lerobot_rollout import main as rollout_main

    sys.argv = [sys.argv[0]] + rest  # 交给 lerobot 自己的解析器

    rounds = max(1, ours.repeat)
    done = 0
    try:
        for i in range(1, rounds + 1):
            if rounds > 1:
                print(f"──── 第 {i}/{rounds} 轮 ────", flush=True)
            before = publisher.frames
            try:
                rollout_main()
            except KeyboardInterrupt:
                print("\n  被 Ctrl+C 打断，不继续下一轮", flush=True)
                break
            except SystemExit as exc:
                # rollout 遇到致命故障会 raise SystemExit(1)；这种不该继续硬跑
                print(f"\n  第 {i} 轮异常退出（code={exc.code}），停止后续轮次", flush=True)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"\n  第 {i} 轮抛错（{exc!r}），停止后续轮次", flush=True)
                break
            done += 1
            print(f"  第 {i} 轮结束，本轮推 {publisher.frames - before} 帧", flush=True)
            if i < rounds and ours.repeat_gap > 0:
                time.sleep(ours.repeat_gap)
    finally:
        print(f"\n  推理结束：跑完 {done}/{rounds} 轮，共推 {publisher.frames} 帧", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
