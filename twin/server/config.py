"""数字层后端的可调参数。要调效果就改这里，别散到各处。"""

from pathlib import Path

HERE = Path(__file__).resolve().parent

MOTIONS_DIR = HERE / "motions"
# JSONL 留档落这里（项目根下 twin/run/），不放 C 盘
RUN_DIR = HERE.parent / "run"
WEB_DIR = HERE.parent / "web"

# 关节顺序，与技术方案第 2.1 节一致，与 web/src/frame.js 的 JOINT_ORDER 一一对应
JOINT_ORDER = [
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
]

# 六个关节的限位（度）。由 URDF 的弧度值换算，见 tech 文档第 14 节
JOINT_LIMITS_DEG = {
    "shoulder_pan": (-110.0, 110.0),
    "shoulder_lift": (-100.0, 100.0),
    "elbow_flex": (-96.8, 96.8),
    "wrist_flex": (-95.0, 95.0),
    "wrist_roll": (-157.2, 162.8),
    "gripper": (-10.0, 100.0),
}

# 每个动作的首尾都必须落在这个姿态上，动作之间才接得自然。面板上「回零位」也回这里。
#
# 腕部自转（第 5 个）是 -90°，**这不是随手定的**：2026-10-24 实测，实机静止时腕部读数
# 在 0 附近，而屏幕上要让夹爪朝向和实机一致，数字层那根轴就得是 -90°。
# 换句话说 数字层 -90° ≡ 实机 0°，两者差的那 90° 由 robot/offsets.json 里腕部的
# offset（≈ -87°）承担，别在这里改成 0 —— 改成 0 屏幕和实物就差 90°。
# 抓取那一帧仍留在 -60°（不是 -90°）：那个角度两颚才在水平面里开合。
# 实测开合方向的竖直分量：-60° ≈ 0.083（水平）、-90° ≈ 0.272（偏 16°）、-150° ≈ 0.868（上下）。
NEUTRAL_DEG = [0.0, 0.0, 0.0, 0.0, -90.0, 0.0]

# 播放列表：按这个顺序循环
PLAYLIST = ["wave", "pick_red_block", "bow"]
# 一个动作播完到下一个动作开始的停顿（秒）。
# 每个动作自己首尾都有一小段停在零位，加在一起够读作"做完了一个动作"。
ACTION_GAP = 1.0

HOST = "127.0.0.1"
HTTP_PORT = 8000
WS_PORT = 8001

# 数据源。默认还是动作库，接实机时用 --source=remote。
# remote 连的是 WSL 里的桥进程（robot/robot_bridge.py），它独占 follower 串口。
SOURCE = "motion-library"
REMOTE_URL = "ws://localhost:8765"

# 后端广播频率。定频，不跟着数据源抖（技术方案第 1.2 节）
BROADCAST_HZ = 30.0

MODE_FOLLOW = 0
MODE_MIRROR = 1
MODE_IDLE = 2
MODE_NAMES = {MODE_FOLLOW: "跟随", MODE_MIRROR: "反向", MODE_IDLE: "无关运动"}

# 夹爪角度（度）与 LeRobot 0-100 的换算。
# 方向实测过：URDF -10° 是闭合、100° 是张开，与官方 README 的「0 全闭 / 100 全开」一致。
# 但实机的电机零位还没核对，所以 grip 只用于显示，前端不得拿它驱动关节。
GRIP_DEG_CLOSED = -10.0
GRIP_DEG_OPEN = 100.0

# 没有任何客户端连着，且已经暂停超过这么久，就自动恢复播放。
# 防止"谁按了暂停然后关了页面，下一个人打开发现它一动不动"。
IDLE_RESUME_SEC = 30.0

# 每 N 帧写一行 JSONL 留档
RECORD_EVERY = 1
