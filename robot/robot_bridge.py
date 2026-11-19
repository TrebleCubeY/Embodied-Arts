#!/usr/bin/env python3
"""SO-101 follower → 68 字节状态帧 → WebSocket :8765。WSL 里跑，常驻。

    实机读数(get_observation)  ──50Hz──▶  换算成 twin 空间六关节度  ──30Hz──▶  各客户端
         reader 线程                         含零位 offset 与夹爪映射            浏览器 / twin 后端

**三种驱动档，进程内切换**（`--drive` 选初始档，默认 hold）：

    teleop     读 leader → 写 follower          人手带它动
    keyframes  按 twin/server/motions 回放 → 写  预设动作
    hold       不写指令                          停住（舵机自己保持）

为什么合并成一个进程：串口只能有一个主人。以前 teleop 要 `--teleop`、预设动作要另起
后端、推理要 `rollout_bridge.py`，切一次就杀进程一次，臂会失力塌下去。现在三档都在
这里切，串口从头到尾只被开一次。

档位切换由上行文本控制（`{"type":"drive","mode":"teleop"}`），页面上的按钮、键盘 1/2/3、
USB 小键盘发的都是这同一条消息。切档时有平滑过渡（`RAMP_SEC`），不会瞬间甩过去。

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
from motions import Keyframes  # noqa: E402  预设动作回放（算法复用 twin/server/library.py）

# 三档驱动。字符串直接进 WS 消息，别改成中文
DRIVE_TELEOP = "teleop"
DRIVE_KEYFRAMES = "keyframes"
DRIVE_HOLD = "hold"
DRIVE_MODES = (DRIVE_TELEOP, DRIVE_KEYFRAMES, DRIVE_HOLD)
DRIVE_LABELS = {DRIVE_TELEOP: "遥操跟随", DRIVE_KEYFRAMES: "自动 · 预设", DRIVE_HOLD: "停住"}

# 切档时的过渡时长（秒）。从"当前实际位置"平滑走到新目标，
# 不然进 keyframes 会从蜷缩姿态直接甩到零位。2 秒约 100° 时是 50°/s，看着不吓人。
RAMP_SEC = 2.0

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


def twin_to_raw6(twin6, offsets) -> list:
    """twin 六关节度 → 要写给 follower 的六元组 `[5 个度 + 夹爪 0-100]`。

    两件事必须同时做对：

    - **减** offset（`twin = raw + offset` 的逆）。方向反了整体偏几十度，
      屏幕和实物差一大截，而且动作看着还挺顺，最难发现。
    - 先在 twin 空间按 `config.JOINT_LIMITS_DEG` clip。动作文件只按 twin 限位校验，
      实机还有校准误差，直接写会顶到机械限位。

    夹爪不参与 offset：它的两端由 -10°..100° ↔ 0..100 钉死。
    """
    limits = [config.JOINT_LIMITS_DEG[name] for name in JOINTS[:5]]
    out = []
    for i, (lo, hi) in enumerate(limits):
        v = min(float(hi), max(float(lo), float(twin6[i])))
        out.append(v - offsets[i])
    grip_deg = min(GRIP_HI, max(GRIP_LO, float(twin6[5])))
    pos = (grip_deg - GRIP_LO) / (GRIP_HI - GRIP_LO) * 100.0
    out.append(min(100.0, max(0.0, pos)))
    return out


def raw6_from_action(action: dict) -> list:
    """leader.get_action() 或 follower 的观测 → 六元组 `[5 个度 + 夹爪 0-100]`。

    两侧都是"实机读数空间"，所以 leader 的读数可以原样写给 follower（官方 teleop
    就是这么做的），中间不掺 twin 空间的换算。
    """
    out = [float(action[f"{name}.pos"]) for name in JOINTS[:5]]
    out.append(float(action["gripper.pos"]))
    return out


def action_from_raw6(raw6) -> dict:
    action = {f"{name}.pos": float(v) for name, v in zip(JOINTS[:5], raw6[:5])}
    action["gripper.pos"] = float(raw6[5])
    return action


def smoothstep(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3.0 - 2.0 * u)


# ---------------------------------------------------------------- 驱动


class Drive:
    """决定"这一圈写什么给 follower"。三档共用一条写路径，只是目标来源不同。

    线程模型：Reader 线程调 `tick()`；WS 处理线程调 `set_mode()` / `set_paused()`。
    状态读写在 `self.lock` 里，写串口不在锁里（那是 Reader 独占的）。

    过渡（ramp）：切档后 `RAMP_SEC` 秒内，输出从"切换时的实际位置"平滑走到新目标。
    目标每圈重新取（teleop 时人手还在动），起点固定，所以过渡既平滑又跟手。
    """

    def __init__(self, offsets, keyframes=None, leader=None, mode=DRIVE_HOLD):
        self.offsets = list(offsets)
        self.kf = keyframes
        self.leader = leader
        self.lock = threading.Lock()
        self.mode = mode if mode in DRIVE_MODES else DRIVE_HOLD
        self.paused = False
        # 最近一次真实读数（raw 六元组），由 Reader 每圈回填。ramp 的起点靠它
        self._reading = None
        # 最近一次写出去的目标（raw 六元组）
        self._written = None
        self._ramp = None
        self._last_tick = None
        self.writes = 0
        self.write_fails = 0
        self.last_error = None
        self._listeners = []
        # 现在在演哪个预设动作。数字层靠它决定红块出不出现，所以要和帧一起发出去。
        # 变化时才广播（不是每帧），和状态帧解耦。
        self._action_listeners = []
        self._last_action = None

    # ------------------------------------------------------------ 对外接口

    def on_change(self, fn):
        """档位变化时回调（Broadcaster 用它广播给页面）。"""
        self._listeners.append(fn)

    def on_action(self, fn):
        """预设动作切换时回调，参数是动作名或 None。"""
        self._action_listeners.append(fn)

    def _emit_action(self, name):
        for fn in list(self._action_listeners):
            try:
                fn(name)
            except Exception:  # noqa: BLE001
                pass

    def state(self) -> dict:
        with self.lock:
            return {
                "type": "drive",
                "mode": self.mode,
                "paused": self.paused,
                "label": DRIVE_LABELS.get(self.mode, self.mode),
            }

    def _notify(self, state: dict):
        for fn in list(self._listeners):
            try:
                fn(state)
            except Exception:  # noqa: BLE001  广播失败不能影响驱动
                pass

    def set_mode(self, mode: str) -> bool:
        if mode not in DRIVE_MODES:
            return False
        with self.lock:
            prev = self.mode
            changed = mode != prev
            if changed:
                # 过渡起点：上一圈真写出去的值；还没写过就用手上的读数
                start = self._written or self._reading
                self._ramp = {"t": 0.0, "dur": RAMP_SEC, "from": list(start)} if start else None
                self.mode = mode
                self.paused = False
                if mode == DRIVE_KEYFRAMES and self.kf is not None:
                    self.kf.rewind()
                    self.kf.paused = False
            state = {
                "type": "drive",
                "mode": mode,
                "paused": self.paused,
                "label": DRIVE_LABELS.get(mode, mode),
                "prev": prev,
            }
        if changed:
            print(f"  驱动切换到 {DRIVE_LABELS.get(mode, mode)}"
                  f"{'（' + format(RAMP_SEC, 'g') + 's 过渡）' if self._ramp else ''}", flush=True)
        # 幂等的重复点击也回一次：小键盘按下去要有回应，否则不知道有没有生效
        self._notify(state)
        return True

    def set_paused(self, paused: bool) -> bool:
        """暂停只对 keyframes 有意义：不推进播放时钟，目标停在当前采样值，
        舵机继续写同一个位置 —— 所以是"停住"，不是松扭矩。"""
        paused = bool(paused)
        with self.lock:
            changed = self.paused != paused
            self.paused = paused
            if self.kf is not None:
                self.kf.paused = paused
            state = {
                "type": "drive",
                "mode": self.mode,
                "paused": paused,
                "label": DRIVE_LABELS.get(self.mode, self.mode),
            }
        if changed:
            print(f"  预设动作{'已暂停' if paused else '继续'}", flush=True)
        self._notify(state)
        return True

    def note_reading(self, raw6):
        with self.lock:
            self._reading = list(raw6)

    @property
    def action_name(self):
        """当前在演的预设动作名（没有就是 None）。hello 里要带上，后连的页面才知道。"""
        return self._last_action

    # ------------------------------------------------------------ 每圈

    def tick(self, now: float):
        """返回这一圈要写给 follower 的 action；None 表示这一档不写。"""
        with self.lock:
            mode = self.mode
            ramp = self._ramp
            dt = 0.0 if self._last_tick is None else max(0.0, now - self._last_tick)
            self._last_tick = now
            reading = self._reading

        # 先把"现在在演哪个动作"报出去（与这一档写不写指令无关）：
        # 数字层的红块只在夹取动作里出现，靠的就是这条。
        if mode == DRIVE_KEYFRAMES and self.kf is not None:
            name, _phase = self.kf.lib.current(self.kf.t)
            if name != self._last_action:
                self._last_action = name
                self._emit_action(name)
        elif self._last_action is not None:
            self._last_action = None
            self._emit_action(None)

        # 还没读到过姿态就先不写：第一圈的 ramp 起点只能来自实测读数，
        # 用 0 当起点会让臂先蹿一下再回来
        if reading is None or mode == DRIVE_HOLD:
            return None

        if mode == DRIVE_TELEOP:
            if self.leader is None:
                return None
            # leader 的读数本身就是实机空间，直接透传（官方 teleop 的做法）
            target = raw6_from_action(self.leader.get_action())
        else:
            if self.kf is None:
                return None
            self.kf.advance(dt)
            target = twin_to_raw6(self.kf.sample(), self.offsets)

        if ramp is not None:
            ramp["t"] += dt
            k = smoothstep(ramp["t"] / ramp["dur"])
            target = [a + (b - a) * k for a, b in zip(ramp["from"], target)]
            if ramp["t"] >= ramp["dur"]:
                with self.lock:
                    if self._ramp is ramp:
                        self._ramp = None

        with self.lock:
            self._written = list(target)
        return action_from_raw6(target)


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
    """没有机械臂时验证链路用。

    **写什么就读什么**：一旦收到过 send_action，之后的 read 就返回那份 action。
    真实的 follower 就是这样（舵机跟到指令位置，编码器读回来是一致的），
    所以 keyframes 档在 --fake 下也能看到整套动作 —— 否则屏幕上是正弦扫掠，
    预设动作根本演不出来。
    从来没被写过（比如 hold 档）才退回六路正弦扫掠，用来确认"读数在动"。
    """

    def __init__(self):
        self.t0 = time.perf_counter()
        self.writes = 0
        self._action = None

    def connect(self):
        pass

    def read(self) -> dict:
        if self._action is not None:
            return dict(self._action)
        s = time.perf_counter() - self.t0
        obs = {}
        for i, name in enumerate(JOINTS[:5]):
            obs[f"{name}.pos"] = 25.0 * math.sin(2 * math.pi * (0.10 + 0.03 * i) * s)
        obs["gripper.pos"] = 50.0 + 50.0 * math.sin(2 * math.pi * 0.2 * s)
        return obs

    def send_action(self, action: dict):
        self.writes += 1
        self._action = dict(action)
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
    """50Hz 干活：先按当前档位写 follower（如果这一档要写），再读回来出帧。

    写失败单独计数：写不动不该拖累状态流，屏幕继续跟随 follower 的真实读数。
    读失败只记数、不动最新值 —— 冻结就是靠这一点。
    """

    def __init__(self, src, latest: Latest, read_hz: float, drive=None):
        super().__init__(daemon=True, name="reader")
        self.src = src
        self.latest = latest
        self.drive = drive
        self.period = 1.0 / read_hz
        self.stop_evt = threading.Event()
        self._last_warn = 0.0
        self._last_write_warn = 0.0
        self.writes = 0
        self.write_fails = 0

    def run(self):
        nxt = time.perf_counter()
        while not self.stop_evt.is_set():
            if self.drive is not None:
                try:
                    action = self.drive.tick(time.perf_counter())
                    if action is not None:
                        self.src.send_action(action)
                        self.writes += 1
                        # 桥的进度行读的是 Drive 上的计数，这里同步一份
                        self.drive.writes = self.writes
                except Exception as exc:  # noqa: BLE001  写不进去不该拖垮读
                    self.write_fails += 1
                    self.drive.write_fails = self.write_fails
                    self.drive.last_error = repr(exc)
                    now = time.perf_counter()
                    if now - self._last_write_warn > 2.0:
                        self._last_write_warn = now
                        print(
                            f"  [!] 写 follower 失败 {self.write_fails} 次：{exc!r}"
                            "（状态流不受影响，但实机不会跟手）",
                            flush=True,
                        )
            try:
                obs = self.src.read()
                self.latest.put(obs)
                if self.drive is not None:
                    self.drive.note_reading(raw6_from_action(obs))
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
    def __init__(self, latest: Latest, push_hz: float, drive=None):
        self.latest = latest
        self.push_hz = push_hz
        self.drive = drive
        self.clients = set()
        self.boot_id = f"{int(time.time())}-{id(self) & 0xFFFF}"
        # 待广播的文本事件。Drive 的回调可能在别的线程里，所以只往里塞，
        # 真正的发送在 pump() 这一个线程里做。
        self.pending = []
        if drive is not None:
            drive.on_change(self._on_drive_change)
            drive.on_action(self._on_action)

    def _on_drive_change(self, state: dict):
        self.pending.append(state)

    def _on_action(self, name):
        """预设动作切换。只报名字，关键帧由后端从自己那份动作库里补 ——
        桥和后端读的是同一批 JSON，不必在链路上再搬一遍 poses。"""
        self.pending.append({"type": "action", "name": name})

    async def _send_text(self, text: str):
        for conn in list(self.clients):
            try:
                await conn.send(text)
            except Exception:  # noqa: BLE001
                self.clients.discard(conn)

    def _hello(self) -> str:
        return json.dumps(
            {
                "type": "hello",
                "bootId": self.boot_id,
                "paused": bool(self.drive.paused) if self.drive else False,
                "hz": self.push_hz,
                "frameSize": protocol.FRAME_SIZE,
                # S1 是前端直连桥，没有后端，「回零位」得从这儿拿静止姿态
                "neutralDeg": config.NEUTRAL_DEG,
                # 当前驱动档。页面后开也能立刻显示对的状态
                "drive": self.drive.state() if self.drive else None,
                # 当前在演哪个预设动作。数字层靠它决定红块出不出现
                "action": self.drive.action_name if self.drive else None,
            },
            ensure_ascii=False,
        )

    async def handler(self, conn):
        self.clients.add(conn)
        try:
            await conn.send(self._hello())
            async for msg in conn:
                if isinstance(msg, str):
                    self.handle_control(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.clients.discard(conn)

    def handle_control(self, raw: str):
        """上行只有两条：换档、暂停。页面 / 键盘 / USB 小键盘发的都是这两条。"""
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict) or self.drive is None:
            return
        kind = msg.get("type")
        if kind == "drive":
            self.drive.set_mode(str(msg.get("mode", "")))
        elif kind == "play":
            self.drive.set_paused(bool(msg.get("paused", False)))

    async def pump(self):
        period = 1.0 / self.push_hz
        t0 = time.perf_counter()
        seq = 0
        nxt = t0
        last_log = t0
        while True:
            while self.pending:
                await self._send_text(json.dumps(self.pending.pop(0), ensure_ascii=False))

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
                if self.drive is not None:
                    label = DRIVE_LABELS.get(self.drive.mode, self.drive.mode)
                    if self.drive.paused:
                        label += "·已暂停"
                    tail += f" · {label}"
                    whz = self.drive.writes / (now - t0) if now > t0 else 0.0
                    tail += f" · 写入 {whz:.1f}Hz"
                    if self.drive.write_fails:
                        tail += f"（失败 {self.drive.write_fails}）"
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


async def run(args, src, latest, reader, drive):
    bridge = Bridge(latest, args.push_hz, drive=drive)
    from websockets.asyncio.server import serve

    server = await serve(bridge.handler, args.host, args.ws_port, max_size=1 << 20)
    print(f"桥进程已就绪  ws://{args.host}:{args.ws_port}  （{args.push_hz:g}Hz 推帧）")
    print(f"  读数      {args.read_hz:g}Hz  {'模拟扫掠 --fake' if args.fake else f'{args.port_dev} / {args.id}'}")
    print(f"  驱动      {DRIVE_LABELS.get(drive.mode, drive.mode)}（页面按钮 / 键盘 1·2·3 可切）")
    if drive.leader is not None:
        print(f"  遥操源    {'模拟 --fake' if args.fake else f'{args.leader_port} / {args.leader_id}'}")
    else:
        print("  遥操源    未打开（切到「遥操跟随」也不会动，先查 leader 串口）")
    print(f"  预设动作  {' → '.join(config.PLAYLIST)}，一圈 {drive.kf.cycle:.1f}s" if drive.kf else "  预设动作  未加载")
    print(f"  零位偏移  {args.offsets_used}")
    print("  Ctrl+C 结束（会断开连接、释放扭矩，记得托住臂）\n", flush=True)

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
        "--drive",
        default=DRIVE_HOLD,
        choices=DRIVE_MODES,
        help="起手档位。hold 停住（默认，最安全）/ keyframes 自动播预设动作 / teleop 遥操跟随。"
        "运行中由页面上的按钮、键盘 1·2·3 或 USB 小键盘切换",
    )
    ap.add_argument("--motions", default="", help="动作库目录，默认 twin/server/motions")
    ap.add_argument("--no-leader", action="store_true", help="不打开 leader（只演示预设动作时用）")
    ap.add_argument(
        "--teleop",
        action="store_true",
        help="旧写法，等价于 --drive teleop（页面里照样能切）",
    )
    ap.add_argument("--leader-port", default="/dev/ttyACM0", help="leader 串口")
    ap.add_argument("--leader-id", default="zyf_leader", help="leader 的校准 id")
    ap.add_argument(
        "--release-torque",
        action="store_true",
        help="起手就断开 follower 扭矩（只读看姿态用，臂可以手掰）。默认保持扭矩，"
        "因为三档里没有一档是松开的",
    )
    ap.add_argument("--fake", action="store_true", help="不连串口，用正弦扫掠验证链路")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.teleop:
        args.drive = DRIVE_TELEOP

    offsets, found = load_offsets(Path(args.offsets))
    args.offsets_found = found
    args.offsets_now = offsets
    args.offsets_used = args.offsets if found else f"{args.offsets}（不存在，按全 0）"
    if not found:
        print(f"  [!] 没找到 {args.offsets}，零位偏移按全 0 处理 —— 数字层姿态可能整体偏几十度。")
        print("      先跑 --capture-zero 量一遍。")

    cameras = json.loads(args.cameras) if args.cameras else {}
    # 三档都要求 follower 使能（hold 靠舵机自己撑住姿态），所以默认保持扭矩。
    # 只有 --release-torque 才断开，那是"纯看姿态"的用法。
    hold = not args.release_torque
    src = (
        FakeRobot()
        if args.fake
        else SerialRobot(args.port_dev, args.id, cameras, hold_torque=hold)
    )
    leader = None
    if not args.no_leader:
        leader = FakeLeader() if args.fake else SerialLeader(args.leader_port, args.leader_id)

    print(f"桥进程  连接 {'模拟源' if args.fake else args.port_dev}")
    try:
        src.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"  连不上：{exc!r}")
        print("  查：设备有没有挂进 WSL（ls -l /dev/ttyACM*）、chmod 666、id 是否与校准一致。")
        print("  若是 Device or resource busy，说明有别的进程占着串口（teleoperate / 另一个桥）。")
        return 1

    leader_error = None
    if leader is not None:
        print(f"  连接 leader {'模拟 --fake' if args.fake else args.leader_port}（{args.leader_id}）")
        try:
            leader.connect()
        except Exception as exc:  # noqa: BLE001
            # 不退出：预设动作与停住都不需要 leader，切到遥操时自然不动，页面会看到
            leader_error = repr(exc)
            print(f"  [!] leader 连不上：{leader_error}")
            print("      预设动作与停住照常可用；切到「遥操跟随」时臂不会动。")
            print(f"      查 --leader-port（现在 {args.leader_port}）与 --leader-id（现在 {args.leader_id}）。")
            leader = None

    try:
        if args.capture_zero:
            ref = parse_ref_pose(args.ref_pose)
            capture_zero(args, src, ref)
            return 0

        keyframes = Keyframes(Path(args.motions) if args.motions else None)
        drive = Drive(offsets, keyframes=keyframes, leader=leader, mode=args.drive)
        latest = Latest(offsets)
        reader = Reader(src, latest, args.read_hz, drive=drive)
        try:
            asyncio.run(run(args, src, latest, reader, drive))
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
