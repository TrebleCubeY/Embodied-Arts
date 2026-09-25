"""数据通路中间那几环：播放时钟、零位标定、模式调度。

    library.sample(播放时钟)  → 六关节角(度)   ← 动作库当前姿态，即"实机"
    calibrator.apply(raw)     → 度             ← 加零位偏移
    scheduler.step(actual)    → (target, mode, blend)

本期 ModeScheduler 只实现跟随，反向与无关运动留空分支。
"""

import config


class PlayClock:
    """播放时钟。暂停就是冻结它，恢复后从原处继续。

    注意它和状态帧里的 t 是两回事：t 是后端单调时钟、暂停也不停，
    这样前端"丢弃更旧的帧"的规则和延迟统计不会因为暂停而失效。
    """

    def __init__(self):
        self.t = 0.0
        self.paused = False
        self._last = None

    def tick(self, now: float) -> float:
        if self._last is None:
            self._last = now
            return self.t
        dt = now - self._last
        self._last = now
        if not self.paused:
            self.t += max(0.0, dt)
        return self.t

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


class Calibrator:
    """零位偏移 + 夹爪值域换算。

    本期零位偏移全是 0：动作库本来就是数字层空间的角度，没有"实机原始读数"这回事。
    等接了串口，offsets 才是从实机标定算出来的（技术方案第 4.1 节），
    这一步没做完之前数字层姿态会整体偏几十度，所以别跳。
    """

    def __init__(self, offsets=None):
        self.offsets = list(offsets) if offsets else [0.0] * 6

    def apply(self, raw: list) -> list:
        return [v + o for v, o in zip(raw, self.offsets)]

    def to_grip(self, gripper_deg: float) -> float:
        """夹爪角度（度）→ LeRobot 0-100。只用于 grip 字段显示。

        方向 2026-09-24 实测：URDF -10° 闭合、100° 张开。
        实机电机零位尚未核对，所以前端不得拿这个值驱动关节（技术方案第 4.2 节）。
        """
        lo, hi = config.GRIP_DEG_CLOSED, config.GRIP_DEG_OPEN
        if hi == lo:
            return 0.0
        v = (gripper_deg - lo) / (hi - lo) * 100.0
        return max(0.0, min(100.0, v))


class ModeScheduler:
    """跟随 / 反向 / 无关运动 + 过渡插值。

    本期只做跟随：target = actual 直通，blend 恒为 255（没有过渡在进行）。
    反向模式的公式（技术方案第 5.2 节，别写成直接取负）：
        q' = lo + hi - q      # 在关节限位区间内做反射，天然不越界
    夹爪不参与反向。各模式的驻留时间与过渡曲线见第 5.5 节。
    """

    def __init__(self):
        self.mode = config.MODE_FOLLOW

    def step(self, actual: list):
        if self.mode == config.MODE_FOLLOW:
            return list(actual), config.MODE_FOLLOW, 255
        # 反向与无关运动还没做
        return list(actual), config.MODE_FOLLOW, 255
