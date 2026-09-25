"""状态帧广播 + 上行控制 + JSONL 留档。

前端和后端走同一条 socket：二进制消息是状态帧，文本消息是事件与控制。
处理函数里先按类型分流，所以重连逻辑只有一份。
（顺序不能反：文本被喂进 DataView 会直接抛异常。）
"""

import json
import os
import time
from pathlib import Path

from websockets.asyncio.server import serve

import config
import protocol


# 调试期间后端会被反复起停，每起一次就多一个留档文件 —— 跑一天能积几十个，
# 内容是同一段动作的重放，没有价值。所以启动时只留最近几个。
# 要完整留存就加 --no-record，自己管文件。
KEEP_RUNS = 5


def prune_runs(run_dir: Path, keep: int = KEEP_RUNS) -> int:
    """按文件名（时间戳）倒序，超出的删掉，返回删掉的个数。"""
    if keep <= 0:
        return 0
    runs = sorted(run_dir.glob("*.jsonl"), key=lambda p: p.name, reverse=True)
    removed = 0
    for old in runs[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


class Recorder:
    """每帧写一行 JSONL 留档。将来可以拿去当回放素材。"""

    def __init__(self, enabled=True, run_dir: Path = None):
        self.enabled = enabled
        self.path = None
        self._fh = None
        if not enabled:
            return
        run_dir = run_dir or config.RUN_DIR
        run_dir.mkdir(parents=True, exist_ok=True)
        prune_runs(run_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = run_dir / f"{stamp}.jsonl"
        self._fh = self.path.open("w", encoding="utf-8")

    def write(self, frame: dict):
        if self._fh:
            self._fh.write(json.dumps(frame, separators=(",", ":")) + "\n")

    def flush(self):
        if self._fh:
            self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


class Broadcaster:
    """WS 服务端。权威状态在后端，前端只反映它，不做本地乐观勾选。"""

    def __init__(self, clock, recorder: Recorder = None, port: int = None):
        self.clock = clock
        self.recorder = recorder
        self.port = port or config.WS_PORT
        self.clients = set()
        self.paused = False
        self.boot_id = f"{int(time.time())}-{os.getpid()}"
        self.on_keyframe = None
        self.last_client_left = None
        self._events = []
        self._server = None

    # ------------------------------------------------------------ 事件

    def queue_event(self, ev: dict):
        self._events.append(ev)

    async def _send_text(self, payload: str, only=None):
        targets = [only] if only is not None else list(self.clients)
        for conn in targets:
            try:
                await conn.send(payload)
            except Exception:  # noqa: BLE001
                self.clients.discard(conn)

    # ------------------------------------------------------------ 连接

    async def handler(self, conn):
        self.clients.add(conn)
        self.last_client_left = None
        await self._send_text(
            json.dumps(
                {
                    "type": "hello",
                    "bootId": self.boot_id,
                    "paused": self.paused,
                    "hz": config.BROADCAST_HZ,
                    "frameSize": protocol.FRAME_SIZE,
                }
            ),
            only=conn,
        )
        try:
            async for msg in conn:
                # 上行只收文本控制消息；二进制一律忽略
                if isinstance(msg, str):
                    self.handle_control(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.clients.discard(conn)
            self.last_client_left = time.monotonic()

    def handle_control(self, raw: str):
        msg = protocol.parse_control(raw)
        if not msg:
            return
        kind = msg["type"]
        if kind == "hello":
            # 页面连上时会上报它期望的状态（默认 running）。
            # 不然"暂停之后刷新页面"会看起来像坏了。
            self.set_paused(msg.get("expect") == "paused")
        elif kind == "pause":
            self.set_paused(True)
        elif kind == "resume":
            self.set_paused(False)
        elif kind == "keyframe":
            if self.on_keyframe:
                self.on_keyframe(msg)

    # ------------------------------------------------------------ 暂停

    def set_paused(self, value: bool):
        if value == self.paused:
            return
        self.paused = value
        if value:
            self.clock.pause()
        else:
            self.clock.resume()
        self.queue_event({"type": "state", "paused": value, "playhead": round(self.clock.t, 3)})

    def maybe_auto_resume(self, now: float):
        """没有客户端还停在暂停态超过 IDLE_RESUME_SEC，就自己恢复。
        否则有人按了暂停然后关掉页面，下一个人打开会以为它坏了。"""
        if not self.paused or self.clients or self.last_client_left is None:
            return
        if now - self.last_client_left > config.IDLE_RESUME_SEC:
            print(f"  没有客户端，暂停已超过 {config.IDLE_RESUME_SEC:.0f}s，自动恢复播放")
            self.set_paused(False)

    # ------------------------------------------------------------ 广播

    async def broadcast(self, frame: bytes):
        for conn in list(self.clients):
            try:
                await conn.send(frame)
            except Exception:  # noqa: BLE001
                self.clients.discard(conn)

    async def flush_events(self):
        if not self._events:
            return
        events, self._events = self._events, []
        for ev in events:
            await self._send_text(json.dumps(ev, ensure_ascii=False))

    async def start(self, port: int = None):
        self._server = await serve(
            self.handler, config.HOST, port or self.port, max_size=1 << 20
        )

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
