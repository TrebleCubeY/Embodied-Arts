"""预设动作播放器（给桥用，twin 空间，单位度）。

**不复制插值算法**：直接复用 `twin/server/library.py` 的 Action / MotionLibrary。
代码层的关键帧比对用的是 0.05° 容差，两份实现哪怕只差一点，面板上那个
"停在第 N 个关键帧"就会永远对不上。
"""

import sys
from pathlib import Path

TWIN_SERVER = Path(__file__).resolve().parent.parent / "twin" / "server"
if str(TWIN_SERVER) not in sys.path:
    sys.path.insert(0, str(TWIN_SERVER))

import config  # noqa: E402
import library  # noqa: E402


class Keyframes:
    """按播放时钟在动作库里走。暂停 = 不推进时钟，输出停在当前采样值 ——
    所以"暂停"在实臂上表现为停住不动，而不是松扭矩。
    """

    def __init__(self, motions_dir=None):
        self.lib = library.load(motions_dir)
        self.t = 0.0
        self.paused = False

    @property
    def cycle(self) -> float:
        return self.lib.cycle

    def advance(self, dt: float):
        if self.paused or dt <= 0:
            return
        self.t = (self.t + dt) % self.lib.cycle

    def sample(self) -> list:
        return self.lib.sample(self.t)

    def rewind(self):
        self.t = 0.0

    def info(self) -> dict:
        """给代码层用的当前动作信息。字段与后端 action 事件一致。"""
        name, phase = self.lib.current(self.t)
        act = self.lib.actions.get(name) if name else None
        if act is None:
            return {"name": None, "title": "停顿", "phase": phase,
                    "poses": None, "path": None, "total": None}
        return {
            "name": name,
            "title": act.title,
            "phase": phase,
            # 路径要写成仓库相对路径，代码层直接照着显示
            "path": f"twin/server/motions/{act.path.name}",
            "poses": act.poses,
            "total": round(act.total, 3),
        }


if __name__ == "__main__":
    # 单独跑一遍看播放列表与时长，不连机械臂
    kf = Keyframes()
    print(f"播放列表 {' → '.join(config.PLAYLIST)}，一圈 {kf.cycle:.2f}s")
    for name, act in kf.lib.actions.items():
        print(f"  {name:<16} {act.title:<8} {len(act.poses):>2} 帧 {act.total:6.2f}s")
    print("\n采样抽查（每 0.5s 一次，前 12 个）：")
    for i in range(12):
        kf.t = i * 0.5
        print(f"  t={kf.t:4.1f}s  {kf.info()['title']:<8} {[round(v, 1) for v in kf.sample()]}")
