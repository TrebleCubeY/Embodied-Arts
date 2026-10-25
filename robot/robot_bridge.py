#!/usr/bin/env python3
"""SO-101 follower → 68 字节状态帧 → WebSocket :8765。WSL 里跑，只读。

    实机读数(get_observation)  ──50Hz──▶  换算成 twin 空间六关节度  ──30Hz──▶  各客户端
         reader 线程                         含零位 offset 与夹爪映射            浏览器 / twin 后端

为什么要单独一个进程：串口只能有一个主人。桥独占 /dev/ttyACM1，twin 后端和浏览器
各当客户端连过来，前端一行不用改（68 字节帧与 protocol.py 完全一致）。

**默认只读**：不调 `send_action`，并且连上之后立刻断掉 follower 的扭矩 —— 不然舵机使能着，
臂会硬得像焊住，S1 阶段根本没法手掰。`--keep-torque` 可以反过来（让它自己撑住姿态）。

要"人掰 leader、follower 跟着动、屏幕同时跟着动"，加 `--teleop`：桥自己开 leader、
每圈读 leader 写给 follower，对外照旧只推 follower 的状态帧。这样就不用（也不能）
再开 `lerobot-teleoperate` —— 两条都独占 follower 串口，同时开必然 `Device or resource busy`。

**读失败冻结在最后一帧连续推送，绝不补零、不退出。**

跑法（WSL，conda env lerobot）：

    conda activate lerobot
    python /mnt/d/CUHKSZ/AIE3001/Project/robot/robot_bridge.py \\
        --port /dev/ttyACM1 --id zyf_follower

先量零位（关键，错了整体偏几十度）：

    python .../robot_bridge.py --capture-zero --port /dev/ttyACM1 --id zyf_follower

把机械臂摆成"数字层的参考姿态"再跑（默认腕部自转 -90、其余五个 0，见 robot/ref_pose.png），
把打印出来的内容存成 robot/offsets.json，之后 bridge 自动读它。要换参考姿态就写 --ref-pose。

没有机械臂时验证链路：加 --fake，读的是正弦扫掠而不是串口。
"""

import argparse
import asyncio
import json
import math
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TWIN_SERVER = HERE.parent / "twin" / "server"
sys.path.insert(0, str(TWIN_SERVER))

import protocol  # noqa: E402  与前端 frame.js 一对的那个编解码；这里只借它打包
import config  # noqa: E402    「回零位」要回的那个静止姿态，S1 直连时前端从 hello 里拿

# 关节顺序与 twin 一致（config.JOINT_ORDER）。前五个是度，第六个是夹爪。
JOINTS = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]

# URDF 夹爪限位 -0.174533..1.74533 rad = -10°..100°。实测 -10° 闭合、100° 张开。
# LeRobot 侧 gripper.pos 是 0-100 的行程，与这两个端点线性对应。
GRIP_LO = -10.0
GRIP_HI = 100.0

DEFAULT_OFFSETS = HERE / "offsets.json"

# 量零位时实机要摆成的那个姿态（度）。默认腕部自转 -90：数字层里那根轴写 -90，
# 对应的就是实机读数的 0（2026-10-24 实测），所以量 offset 时要让实机的腕子也摆到
# 那个"夹具两颚朝向和数字层 -90 一致"的位置。
REF_POSE_DEFAULT = "wrist_roll=-90"


# ---------------------------------------------------------------- 换算


def grip_to_deg(pos: float) -> float:
    """LeRobot 0-100 → URDF 空间的角度（度）。"""
    v = max(0.0, min(100.0, float(pos)))
    return GRIP_LO + (GRIP_HI - GRIP_LO) * v / 100.0


def load_offsets(path: Path):
    """零位偏移。支持数组或按关节名写成对象；文件不在就全 0。

    offset 的含义是 twin_angle = raw + offset，也就是「参考姿态 − 实机读数」。
    参考姿态默认是腕部自转 -90（见 REF_POSE_DEFAULT）。符号必须实测，别按公式推。
    """
    if not path.exists():
        return [0.0] * 6, False
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("offsets", data)
    if isinstance(data, dict):
        vals = [float(data.get(name, 0.0)) for name in JOINTS]
    elif isinstance(data, list) and len(data) == 6:
        vals = [float(v) for v in data]
    else:
        raise ValueError(f"{path} 里的 offsets 既不按关节名写，也不是长度 6 的数组")
    return vals, True


def parse_ref_pose(text: str):
    """--ref-pose → 六个度。写 `wrist_roll=-90,shoulder_lift=0` 或 `0,0,0,0,-90,0`。"""
    text = (text or "").strip()
    ref = [0.0] * 6
    if not text:
        return ref
    if "=" in text:
        for part in text.replace(" ", "").split(","):
            if not part:
                continue
            key, _, value = part.partition("=")
            if key not in JOINTS:
                raise SystemExit(f"--ref-pose 里不认识关节：{key}（可写 {', '.join(JOINTS)}）")
            ref[JOINTS.index(key)] = float(value)
        return ref
    vals = [float(v) for v in text.replace(" ", "").split(",")]
    if len(vals) != 6:
        raise SystemExit("--ref-pose 要么写 k=v，要么写六个度数")
    return vals


def offsets_from(med, ref):
    """参考姿态 − 实机读数 = offset。夹爪恒 0（两端由 -10°..100° 的映射钉死）。"""
    return [round(float(r) - float(v), 3) for r, v in zip(ref[:5], med[:5])] + [0.0]


def to_twin_deg(obs: dict, offsets) -> tuple:
    """get_observation() 的原始读数 → (twin 六关节度, 原始 grip 0-100)。

    前五个关节 LeRobot 已经给的是度，只加零位偏移；夹爪要给 0-100 的行程先映射到
    URDF 空间的角度。夹爪的 offset 通常保持 0——映射公式已经把两端钉死了。
    """
    deg = []
    for i, name in enumerate(JOINTS[:5]):
        key = f"{name}.pos"
        if key not in obs:
            raise KeyError(f"读数里没有 {key}（拿到的是 {sorted(obs)}）")
        deg.append(float(obs[key]) + offsets[i])
    grip = float(obs["gripper.pos"])
    deg.append(grip_to_deg(grip) + offsets[5])
    return deg, grip


# ---------------------------------------------------------------- 读数源


class SerialRobot:
    """lerobot 的 SO101Follower。import 放在函数里，没装 lerobot 的机器也能
    import 本模块（--fake 与单元自检要用）。"""

    def __init__(self, port: str, robot_id: str, cameras: dict, hold_torque: bool = False):
        self.port = port
        self.robot_id = robot_id
        self.cameras = cameras or {}
        self.hold_torque = hold_torque
        self.robot = None

    def connect(self):
        from lerobot.robots.so_follower import SO101Follower, SO101FollowerConfig

        # 只在真给了相机时才传 cameras：只读阶段不带相机，少一个可能的参数名对不上的机会。
        kwargs = {"port": self.port, "id": self.robot_id}
        if self.cameras:
            kwargs["cameras"] = self.cameras
        self.robot = SO101Follower(SO101FollowerConfig(**kwargs))
        self.robot.connect()
        self._set_torque(self.hold_torque)

    def _set_torque(self, on: bool):
        """connect() 之后舵机是使能的，臂会硬得像焊住一样掰不动。
        只读/示教阶段必须关掉；要让它自己撑住姿态时才开着。"""
        bus = getattr(self.robot, "bus", None)
        if bus is None:
            return
        try:
            if on:
                bus.enable_torque()
            else:
                bus.disable_torque()
        except Exception as exc:  # noqa: BLE001
            print(f"  [!] 切换扭矩失败：{exc!r}（臂可能掰不动，可用 --keep-torque 反过来试）")

    def read(self) -> dict:
        return self.robot.get_observation()

    def send_action(self, action: dict):
        return self.robot.send_action(action)

    def close(self):
        if self.robot is not None:
            try:
                self.robot.disconnect()  # 释放扭矩，臂会软下来，记得托住
            finally:
                self.robot = None


class SerialLeader:
    """遥操用的 leader 臂。只在 --teleop 时打开：桥自己读 leader、写 follower，
    对外仍然只推 follower 的状态帧 —— 这样"人手带它动"和"屏幕跟着动"能同时成立。"""

    # lerobot 里 SO 系列 leader 的配置有两层：SOLeaderConfig 是**不带 id 的底座**，
    # 带 id（校准 id）的是 SO101LeaderConfig / SOLeaderTeleopConfig。
    # 传错那个类就是 TypeError: unexpected keyword argument 'id'，2026-10-24 踩过。
    MODULES = ("lerobot.teleoperators.so_leader", "lerobot.teleoperators.so101_leader")
    CONFIGS = ("SO101LeaderConfig", "SOLeaderTeleopConfig", "SO100LeaderConfig", "SOLeaderConfig")

    def __init__(self, port: str, teleop_id: str):
        self.port = port
        self.teleop_id = teleop_id
        self.leader = None

    def _load(self):
        """挑一个能接 id 的配置类。版本之间类名飘过，所以按字段探测而不是写死。"""
        import dataclasses
        import importlib

        tried = []
        for mod_name in self.MODULES:
            try:
                mod = importlib.import_module(mod_name)
            except ImportError as exc:
                tried.append(f"{mod_name}: {exc}")
                continue
            cls = getattr(mod, "SOLeader", None) or getattr(mod, "SO101Leader", None)
            if cls is None:
                tried.append(f"{mod_name}: 没找到 SOLeader 类")
                continue
            for cfg_name in self.CONFIGS:
                cfg = getattr(mod, cfg_name, None)
                if cfg is None or not dataclasses.is_dataclass(cfg):
                    continue
                fields = {f.name for f in dataclasses.fields(cfg)}
                if "port" in fields:
                    return cls, cfg, fields
            tried.append(f"{mod_name}: 没有能用的配置类（{', '.join(self.CONFIGS)}）")
        raise ImportError("找不到 leader 的实现：\n  " + "\n  ".join(tried))

    def connect(self):
        cls, cfg_cls, fields = self._load()

        kwargs = {"port": self.port}
        if "id" in fields:
            kwargs["id"] = self.teleop_id
        else:
            # 走到这里说明版本里没有 id 字段，校准文件只能按默认 id 找
            print(f"  [!] {cfg_cls.__name__} 没有 id 字段，{self.teleop_id} 这份校准可能用不上")

        self.leader = cls(cfg_cls(**kwargs))
        self.leader.connect()
        # leader 要能被人手掰动，扭矩必须关
        bus = getattr(self.leader, "bus", None)
        if bus is not None and hasattr(bus, "disable_torque"):
            try:
                bus.disable_torque()
            except Exception as exc:  # noqa: BLE001
                print(f"  [!] leader 关扭矩失败：{exc!r}（可能掰不动）")

    def get_action(self) -> dict:
        return self.leader.get_action()

    def close(self):
        if self.leader is not None:
            try:
                self.leader.disconnect()
            finally:
                self.leader = None


class FakeRobot:
    """没有机械臂时验证链路用。六个数按不同相位扫掠，夹爪来回开合。"""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.writes = 0

    def connect(self):
        pass

    def read(self) -> dict:
        s = time.perf_counter() - self.t0
        obs = {}
        for i, name in enumerate(JOINTS[:5]):
            obs[f"{name}.pos"] = 25.0 * math.sin(2 * math.pi * (0.10 + 0.03 * i) * s)
        obs["gripper.pos"] = 50.0 + 50.0 * math.sin(2 * math.pi * 0.2 * s)
        return obs

    def send_action(self, action: dict):
        self.writes += 1
        return action

    def close(self):
        pass


class FakeLeader:
    """配合 --fake --teleop 用：假装有人在掰 leader。"""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.reads = 0

    def connect(self):
        pass

    def get_action(self) -> dict:
        self.reads += 1
        s = time.perf_counter() - self.t0
        return {f"{n}.pos": 20.0 * math.sin(2 * math.pi * 0.15 * s) for n in JOINTS[:5]} | {
            "gripper.pos": 40.0
        }

    def close(self):
        pass


# ---------------------------------------------------------------- 最新值


class Latest:
    """reader 线程与广播循环之间的交换点。只存成功读到的那一帧。"""

    def __init__(self, offsets):
        self.offsets = offsets
        self.lock = threading.Lock()
        self.deg = None  # 六关节度；None 表示还没读到过
        self.grip = 0.0
        self.at = None  # 这一帧的时间（monotonic）
        self.reads = 0
        self.fails = 0

    def put(self, obs: dict):
        deg, grip = to_twin_deg(obs, self.offsets)
        with self.lock:
            self.deg = deg
            self.grip = grip
            self.at = time.monotonic()
            self.reads += 1

    def fail(self):
        with self.lock:
            self.fails += 1

    def snapshot(self):
        with self.lock:
            return self.deg, self.grip, self.at, self.reads, self.fails


class Reader(threading.Thread):
    """50Hz 读串口。读失败只记数，不动最新值 —— 冻结就是靠这一点。

    带 leader（`--teleop`）时，这一圈先读 leader 再写给 follower，然后才读 follower 出帧。
    写失败单独计数：写不动不该拖累状态流，屏幕继续跟随 follower 的真实读数。
    """

    def __init__(self, src, latest: Latest, read_hz: float, leader=None):
        super().__init__(daemon=True, name="reader")
        self.src = src
        self.latest = latest
        self.leader = leader
        self.period = 1.0 / read_hz
        self.stop_evt = threading.Event()
        self._last_warn = 0.0
        self._last_write_warn = 0.0
        self.writes = 0
        self.write_fails = 0

    def run(self):
        nxt = time.perf_counter()
        while not self.stop_evt.is_set():
            if self.leader is not None:
                try:
                    self.src.send_action(self.leader.get_action())
                    self.writes += 1
                except Exception as exc:  # noqa: BLE001
                    self.write_fails += 1
                    now = time.perf_counter()
                    if now - self._last_write_warn > 2.0:
                        self._last_write_warn = now
                        print(
                            f"  [!] 写 follower 失败 {self.write_fails} 次：{exc!r}"
                            "（状态流不受影响，但实机不会跟手）",
                            flush=True,
                        )
            try:
                self.latest.put(self.src.read())
            except Exception as exc:  # noqa: BLE001  串口掉线、总线失步都在这里
                self.latest.fail()
                now = time.perf_counter()
                if now - self._last_warn > 2.0:  # 别把终端刷爆
                    self._last_warn = now
                    deg, _, at, _, fails = self.latest.snapshot()
                    frozen = "（还没有可冻结的帧）" if deg is None else "（冻结在最后一帧）"
                    print(f"  [!] 读数失败 {fails} 次：{exc!r} {frozen}", flush=True)
            nxt += self.period
            delay = nxt - time.perf_counter()
            if delay > 0:
                self.stop_evt.wait(delay)
            else:
                nxt = time.perf_counter()  # 落后就重新对齐，不追帧

    def stop(self):
        self.stop_evt.set()


# ---------------------------------------------------------------- WS 服务端


class Bridge:
    def __init__(self, latest: Latest, push_hz: float, teleop=None):
        self.latest = latest
        self.push_hz = push_hz
        self.teleop = teleop
        self.clients = set()
        self.boot_id = f"{int(time.time())}-{id(self) & 0xFFFF}"

    async def handler(self, conn):
        self.clients.add(conn)
        # 前端连上就发 hello，并等一个 hello 回包拿 bootId / hz / frameSize。
        # 状态一律报"未暂停"：桥不播动作，没有暂停这回事。
        try:
            await conn.send(
                json.dumps(
                    {
                        "type": "hello",
                        "bootId": self.boot_id,
                        "paused": False,
                        "hz": self.push_hz,
                        "frameSize": protocol.FRAME_SIZE,
                        # S1 是前端直连桥，没有后端，「回零位」得从这儿拿静止姿态
                        "neutralDeg": config.NEUTRAL_DEG,
                    }
                )
            )
            async for _msg in conn:
                pass  # 只读阶段：上行文本（pause/keyframe 之类）一律忽略
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.clients.discard(conn)

    async def pump(self):
        period = 1.0 / self.push_hz
        t0 = time.perf_counter()
        seq = 0
        nxt = t0
        last_log = t0
        while True:
            deg, grip, _at, reads, fails = self.latest.snapshot()
            if deg is not None:
                seq += 1
                frame = protocol.pack_frame(
                    t=time.perf_counter() - t0,
                    seq=seq,
                    actual=deg,
                    target=deg,  # 桥不算偏离：target 与 actual 相同，偏离在后端算
                    mode=0,
                    blend=255,
                    grip=grip,
                )
                for conn in list(self.clients):
                    try:
                        await conn.send(frame)
                    except Exception:  # noqa: BLE001
                        self.clients.discard(conn)

            now = time.perf_counter()
            if now - last_log >= 5.0:
                last_log = now
                hz = reads / (now - t0) if now > t0 else 0.0
                tail = "" if not fails else f" · 读失败 {fails} 次（冻结）"
                if self.teleop is not None:
                    whz = self.teleop.writes / (now - t0) if now > t0 else 0.0
                    tail += f" · 遥操写入 {whz:.1f}Hz"
                    if self.teleop.write_fails:
                        tail += f"（失败 {self.teleop.write_fails}）"
                print(
                    f"  [{now - t0:7.1f}s] 读数 {hz:5.1f}Hz · "
                    f"{len(self.clients)} 个客户端{tail}",
                    flush=True,
                )

            nxt += period
            delay = nxt - now
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                nxt = time.perf_counter()


async def run(args, src, latest, reader):
    bridge = Bridge(latest, args.push_hz, teleop=reader if args.teleop else None)
    from websockets.asyncio.server import serve

    server = await serve(bridge.handler, args.host, args.ws_port, max_size=1 << 20)
    print(f"桥进程已就绪  ws://{args.host}:{args.ws_port}  （{args.push_hz:g}Hz 推帧）")
    print(f"  读数      {args.read_hz:g}Hz  {'模拟扫掠 --fake' if args.fake else f'{args.port_dev} / {args.id}'}")
    if args.teleop:
        print(f"  遥操      开（leader {'模拟 --fake' if args.fake else f'{args.leader_port} / {args.leader_id}'}）")
    else:
        print("  遥操      关（只读；follower 已断扭矩，可以手掰）"
              if not args.keep_torque else "  遥操      关（--keep-torque：follower 保持当前姿态）")
    print(f"  零位偏移  {args.offsets_used}")
    print("  Ctrl+C 结束（会断开连接、释放扭矩）\n", flush=True)

    reader.start()
    try:
        await bridge.pump()
    finally:
        reader.stop()
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------- 零位测量


def capture_zero(args, src, ref):
    """把实机摆成 --ref-pose 那个姿态后跑一次，打印该存的 offsets。取若干次读数的中位数。

    offset = 参考姿态 − 实机读数。默认参考姿态是腕部自转 -90（动作库的静止姿态）。
    夹爪不参与：它的两端由 -10°..100° 的映射钉死，再叠 offset 会整体偏几十度。
    """
    samples = []
    for _ in range(args.zero_samples):
        obs = src.read()
        deg, grip = to_twin_deg(obs, [0.0] * 6)  # 这里要的是原始读数，不加 offset
        samples.append(deg)
        time.sleep(0.05)
    med = [sorted(s[i] for s in samples)[len(samples) // 2] for i in range(6)]
    off = offsets_from(med, ref)

    print("\n参考姿态（数字层，度，也就是实机现在该摆成的样子）：")
    for name, v in zip(JOINTS, ref):
        print(f"  {name:<16} {v:+9.2f}")
    print("\n实机读数（不带 offset，度）：")
    for name, v in zip(JOINTS[:5], med[:5]):
        print(f"  {name:<16} {v:+9.2f}")
    print(f"\n夹爪：原始行程 {grip:6.2f} → 数字层 {med[5]:+7.2f}°（不参与 offset）")

    odd = [n for n, o in zip(JOINTS[:5], off) if abs(o) > 130]
    if odd:
        print(f"\n[!] {', '.join(odd)} 差得离谱，八成是实机没摆成参考姿态（或者校准 id 用错了）。")

    print("\n照这个存成 robot/offsets.json（twin_angle = raw + offset）：")
    print(
        json.dumps(
            {
                "note": "量这组值的时候实机摆成的姿态（twin 空间，度）。它只作记录，读的时候不看它",
                "reference": [round(float(v), 3) for v in ref],
                "offsets": {name: round(o, 3) for name, o in zip(JOINTS, off)},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.offsets_found and any(abs(a - b) > 1e-6 for a, b in zip(args.offsets_now, off)):
        print("\n注意：robot/offsets.json 里现在是一份不同的值，覆盖掉、重起桥才生效。")


# ---------------------------------------------------------------- 入口


def parse_args():
    ap = argparse.ArgumentParser(description="SO-101 follower 状态桥：串口 → 68 字节帧 → WS")
    ap.add_argument("--port", "--device", dest="port_dev", default="/dev/ttyACM1", help="follower 串口")
    ap.add_argument("--ws-port", dest="ws_port", type=int, default=8765, help="桥的 WS 监听端口（客户端连这个）")
    ap.add_argument("--host", default="0.0.0.0", help="WS 监听地址，默认 0.0.0.0")
    ap.add_argument("--id", default="zyf_follower", help="校准 id，必须与 lerobot-calibrate 时一致")
    ap.add_argument("--read-hz", type=float, default=50.0, help="读串口的频率")
    ap.add_argument("--push-hz", type=float, default=30.0, help="推帧频率")
    ap.add_argument("--offsets", default=str(DEFAULT_OFFSETS), help="零位偏移 JSON")
    ap.add_argument("--cameras", default="", help='可选，JSON，如 \'{"wrist":{"type":"opencv","index_or_path":0}}\'')
    ap.add_argument("--capture-zero", action="store_true", help="量零位：摆成 --ref-pose 的姿态后跑，打印 offsets")
    ap.add_argument(
        "--ref-pose",
        default=REF_POSE_DEFAULT,
        help=f"量零位时实机要摆成的姿态（度）。写 k=v 或六个数。默认 {REF_POSE_DEFAULT}",
    )
    ap.add_argument("--zero-samples", type=int, default=21, help="量零位时取几次读数")
    ap.add_argument(
        "--teleop",
        action="store_true",
        help="桥内嵌遥操：同时读 leader 写给 follower（这样人掰 leader、屏幕跟 follower 同时成立）。"
        "注意 lerobot-teleoperate 不能再开，串口被桥占着",
    )
    ap.add_argument("--leader-port", default="/dev/ttyACM0", help="--teleop 时 leader 的串口")
    ap.add_argument("--leader-id", default="zyf_leader", help="--teleop 时 leader 的校准 id")
    ap.add_argument(
        "--keep-torque",
        action="store_true",
        help="不让 follower 断扭矩（默认断，好让手掰）。臂要自己撑住姿态时才用",
    )
    ap.add_argument("--fake", action="store_true", help="不连串口，用正弦扫掠验证链路")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    offsets, found = load_offsets(Path(args.offsets))
    args.offsets_found = found
    args.offsets_now = offsets
    args.offsets_used = args.offsets if found else f"{args.offsets}（不存在，按全 0）"
    if not found:
        print(f"  [!] 没找到 {args.offsets}，零位偏移按全 0 处理 —— 数字层姿态可能整体偏几十度。")
        print("      先跑 --capture-zero 量一遍。")

    cameras = json.loads(args.cameras) if args.cameras else {}
    # 遥操时 follower 必须有扭矩才跟得动；只读时断掉，不然手掰不动（2026-10-24 踩过）
    hold = args.keep_torque or args.teleop
    src = (
        FakeRobot()
        if args.fake
        else SerialRobot(args.port_dev, args.id, cameras, hold_torque=hold)
    )
    leader = None
    if args.teleop:
        leader = FakeLeader() if args.fake else SerialLeader(args.leader_port, args.leader_id)

    print(f"桥进程  连接 {'模拟源' if args.fake else args.port_dev}")
    try:
        src.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"  连不上：{exc!r}")
        print("  查：设备有没有挂进 WSL（ls -l /dev/ttyACM*）、chmod 666、id 是否与校准一致。")
        print("  若是 Device or resource busy，说明有别的进程占着串口（teleoperate / 另一个桥）。")
        return 1

    if leader is not None:
        print(f"  连接 leader {'模拟 --fake' if args.fake else args.leader_port}（{args.leader_id}）")
        try:
            leader.connect()
        except Exception as exc:  # noqa: BLE001
            print(f"  leader 连不上：{exc!r}")
            print("  查 leader 的串口与校准 id（--leader-port / --leader-id）。")
            src.close()
            return 1

    try:
        if args.capture_zero:
            ref = parse_ref_pose(args.ref_pose)
            capture_zero(args, src, ref)
            return 0

        latest = Latest(offsets)
        reader = Reader(src, latest, args.read_hz, leader=leader)
        try:
            asyncio.run(run(args, src, latest, reader))
        except KeyboardInterrupt:
            print("\n已停止")
        finally:
            reader.stop()
    finally:
        if leader is not None:
            leader.close()
        src.close()  # 释放扭矩
        print("  串口已释放")
    return 0


if __name__ == "__main__":
    sys.exit(main())
