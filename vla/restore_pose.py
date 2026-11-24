"""把 follower 平滑挪回指定姿态（默认是 VLA 试跑前的姿态）。

    conda activate lerobot
    python /mnt/d/CUHKSZ/AIE3001/Project/vla/restore_pose.py --port /dev/ttyACM1

用 smoothstep 插值，4 秒走完，不会甩。走完再断扭矩，臂会软下来（手托住）。
"""

from __future__ import annotations

import argparse
import time

import numpy as np

JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
DEFAULT_TARGET = "-4.97,-109.58,96.09,75.74,49.36,1.15"


def smoothstep(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM1", help="follower 串口（12V 那个）")
    ap.add_argument("--id", default="zyf_follower")
    ap.add_argument("--target", default=DEFAULT_TARGET, help="六个数：五个关节角度 + 夹爪 0-100")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--hz", type=float, default=50.0)
    ap.add_argument("--release", action="store_true", help="到位后断扭矩（默认保持）")
    args = ap.parse_args()

    from lerobot.robots.so_follower.config_so_follower import SOFollowerRobotConfig
    from lerobot.robots.so_follower.so_follower import SOFollower

    target = np.array([float(v) for v in args.target.split(",")], dtype=np.float64)

    robot = SOFollower(SOFollowerRobotConfig(port=args.port, id=args.id, cameras={}, use_degrees=True))
    robot.connect()
    try:
        bus = robot.bus
        volts = bus.read("Present_Voltage", "shoulder_pan", normalize=False)
        print(f"端口 {args.port} 电压 {volts/10:.1f}V" + ("（这是 leader，没有 12V 供电）" if volts < 80 else ""))
        robot.bus.enable_torque()

        obs = robot.get_observation()
        start = np.array([float(obs[f"{j}.pos"]) for j in JOINTS])
        print("当前 " + " ".join(f"{v:8.2f}" for v in start))
        print("目标 " + " ".join(f"{v:8.2f}" for v in target))
        print("差值 " + " ".join(f"{v:+8.2f}" for v in (target - start)))

        steps = max(1, int(args.seconds * args.hz))
        t0 = time.monotonic()
        for i in range(1, steps + 1):
            a = smoothstep(i / steps)
            pos = start + (target - start) * a
            robot.send_action({f"{j}.pos": float(v) for j, v in zip(JOINTS, pos)})
            gap = t0 + i / args.hz - time.monotonic()
            if gap > 0:
                time.sleep(gap)

        time.sleep(0.4)
        obs = robot.get_observation()
        end = np.array([float(obs[f"{j}.pos"]) for j in JOINTS])
        print("到位 " + " ".join(f"{v:8.2f}" for v in end))
        print("残差 " + " ".join(f"{v:+8.2f}" for v in (end - target)))
        if args.release:
            robot.bus.disable_torque()
            print("扭矩已断（手托住）")
    finally:
        robot.disconnect()


if __name__ == "__main__":
    main()
