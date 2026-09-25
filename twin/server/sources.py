"""数据源。可插拔：同一套后端既能读动作库，也能读串口（技术方案第 1.3 节）。

这是线 1 与线 2 解耦的关键：期中前用动作库当"实机侧的替身"跑完整链路，
期末把数据源换成串口，前端一行都不用改。
"""


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


class RealSerialSource:
    """串口读 SO-101 follower。期末联调时才实现，这里只留接口。

    实现时要注意的（技术方案第 3、8 节）：
      - 用 lerobot 的 SOFollower / FeetechMotorsBus.sync_read，不要自己写串口协议
      - 单位用度，与状态帧一致（use_degrees=True）
      - 约 50Hz；读失败重试（num_read_retries 默认就是 2），重试仍失败就冻结在最后一帧
      - 500ms 指令看门狗 + max_relative_target
      - 零位偏移由 Calibrator 负责，不混在这里
    """

    kind = "real-serial"

    def __init__(self, port: str = None, robot=None):
        raise NotImplementedError(
            "实机数据源还没实现。期中之前两条线不联调，网络与排期见 plan/项目时间线_v0.8.md。"
        )

    def sample(self, t: float) -> list:
        raise NotImplementedError


def make(kind: str, lib=None, **kw):
    if kind == "motion-library":
        return MotionLibrarySource(lib)
    if kind == "real-serial":
        return RealSerialSource(**kw)
    raise ValueError(f"不认识的数据源：{kind}（目前只有 motion-library 与 real-serial）")
