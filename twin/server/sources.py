"""数据源。可插拔：同一套后端既能读动作库，也能读串口（技术方案第 1.3 节）。

这是线 1 与线 2 解耦的关键：期中前用动作库当"实机侧的替身"跑完整链路，
期末把数据源换成串口，前端一行都不用改。

三种：
  motion-library  动作库（默认，没有机械臂时的替身）
  remote          WSL 里的桥进程推过来的实机读数（robot/robot_bridge.py → WS :8765）
  real-serial     本进程独占串口直接读（未实现，留给"机械臂接本机"那种形式）
"""

import threading
import time

import protocol


class MotionLibrarySource:
    """本期唯一真实现。动作库产生的就是状态帧里的 actual，
    数字层以跟随模式忠实呈现它。

    等偏离模式做出来之后，动作库还会兼一个角色：当数字层要"无关运动"时的素材
    （技术方案第 5.3 节，用真实动作轨迹而不是正弦波）。
    """

    kind = "motion-library"

    def __init__(self, lib):
        self.lib = lib

    def sample(self, t: float) -> list:
        return self.lib.sample(t)

    def describe(self, t: float):
        return self.lib.current(t)


class RemoteStateSource:
    """读 WSL 里桥进程推来的实机状态帧（robot/robot_bridge.py，WS :8765）。

    帧格式就是本仓库那 68 字节状态帧 —— 桥只负责把串口读数换成度、组帧、推出去，
    偏离仍然在这里算。所以这边只取 actual 六个数，target / mode / blend / grip
    全部忽略（grip 由 Calibrator 从 actual[5] 反算，保持"actual 是渲染权威"）。

    后台线程一条 WS 连接，加锁存最新一帧：
      - 断流（连接断了）不换值，sample 返回最后一帧 —— 数字层冻住，不跳零
      - 重连退避 0.5→1→2→5 秒，一直试，不退出
      - 桥进程没起来也能启动后端：先出零位并打一行警告，等桥起来自动接上
    """

    kind = "remote"

    def __init__(self, url: str = "ws://localhost:8765", wait_first: float = 3.0):
        self.url = url
        self._lock = threading.Lock()
        self._actual = None  # 最近一帧的六关节度
        self._at = None  # 这一帧到达的时刻（monotonic）
        self._frames = 0
        self._error = None
        self._stop = threading.Event()

        self._thread = threading.Thread(target=self._run, name="remote-state", daemon=True)
        self._thread.start()
        self._wait_first(wait_first)

    # ------------------------------------------------------------ 后台收帧

    def _run(self):
        backoff = 0.5
        while not self._stop.is_set():
            try:
                with self._connect() as conn:
                    self._error = None
                    backoff = 0.5
                    while not self._stop.is_set():
                        msg = conn.recv()
                        if isinstance(msg, str):
                            continue  # hello 之类的文本事件，桥那边发的，用不上
                        frame = protocol.unpack_frame(msg)
                        with self._lock:
                            self._actual = list(frame["actual"])
                            self._at = time.monotonic()
                            self._frames += 1
            except Exception as exc:  # noqa: BLE001  连不上、断开、帧长不对都算
                self._error = repr(exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2.0, 5.0)

    def _connect(self):
        """连桥进程。**必须绕开 HTTP 代理**：websockets 默认读 http_proxy / https_proxy
        环境变量，一旦机器上挂着代理，连 localhost 也会被送进代理并 502。"""
        from websockets.sync.client import connect

        opts = dict(open_timeout=3.0, max_size=1 << 20)
        try:
            return connect(self.url, proxy=None, **opts)  # websockets >= 15
        except TypeError:
            return connect(self.url, **opts)

    def _wait_first(self, timeout: float):
        """等第一帧。等不到不算失败 —— 先按零位出图，桥起来之后自然跟上。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                if self._actual is not None:
                    return
            time.sleep(0.02)
        print(f"  [!] {timeout:g}s 内没收到 {self.url} 的帧，先按零位出图（桥起来后自动跟上）")

    # ------------------------------------------------------------ 接口

    def sample(self, t: float) -> list:
        with self._lock:
            return list(self._actual) if self._actual is not None else [0.0] * 6

    def describe(self, t: float):
        with self._lock:
            at, frames, err = self._at, self._frames, self._error
        if at is None:
            return "real", f"未连上 {self.url}"
        age = time.monotonic() - at
        if age > 3.0:
            why = f" · {err[:40]}" if err else ""
            return "real", f"断流 {age:.1f}s（冻结在最后一帧）{why}"
        return "real", f"跟随 · {frames} 帧"

    def close(self):
        self._stop.set()


class RealSerialSource:
    """串口读 SO-101 follower。留给"机械臂直接接本机"那种形式，**没有实现**。

    现在走的是 remote：串口由 WSL 里的 robot/robot_bridge.py 独占，这边只收帧。
    真要在这边直接读串口，实现时注意（技术方案第 3、8 节）：
      - 用 lerobot 的 SO101Follower / FeetechMotorsBus.sync_read，不要自己写串口协议
      - 单位用度，与状态帧一致（use_degrees=True）
      - 约 50Hz；读失败重试（num_read_retries 默认就是 2），重试仍失败就冻结在最后一帧
      - 500ms 指令看门狗 + max_relative_target
      - 零位偏移由 Calibrator 负责，不混在这里
    """

    kind = "real-serial"

    def __init__(self, port: str = None, robot=None):
        raise NotImplementedError(
            "本进程直读串口还没实现。接实机走 --source=remote（桥进程在 robot/robot_bridge.py）。"
        )

    def sample(self, t: float) -> list:
        raise NotImplementedError


def make(kind: str, lib=None, **kw):
    if kind == "motion-library":
        return MotionLibrarySource(lib)
    if kind == "remote":
        return RemoteStateSource(url=kw.get("url", "ws://localhost:8765"), wait_first=kw.get("wait_first", 3.0))
    if kind == "real-serial":
        return RealSerialSource(**kw)
    raise ValueError(
        f"不认识的数据源：{kind}（只有 motion-library / remote / real-serial）"
    )
