"""看一眼实机：读六关节角 + 存两张相机图，用来确认单位、机位和零点。

    conda activate lerobot
    python /mnt/d/CUHKSZ/AIE3001/Project/vla/probe_scene.py --port /dev/ttyACM0

只在 connect 期间通电，读完就 disconnect（默认断扭矩），不动舵机。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / ".tmp"
JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--id", default="zyf_follower")
    ap.add_argument("--front", type=int, default=0)
    ap.add_argument("--side", type=int, default=2)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    args = ap.parse_args()

    from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig
    from lerobot.robots.so_follower.config_so_follower import SOFollowerRobotConfig
    from lerobot.robots.so_follower.so_follower import SOFollower

    cams = {
        "front": OpenCVCameraConfig(fps=args.fps, width=args.width, height=args.height,
                                    index_or_path=args.front, fourcc="MJPG"),
        "side": OpenCVCameraConfig(fps=args.fps, width=args.width, height=args.height,
                                   index_or_path=args.side, fourcc="MJPG"),
    }
    robot = SOFollower(SOFollowerRobotConfig(port=args.port, id=args.id, cameras=cams, use_degrees=True))
    robot.connect()
    try:
        obs = robot.get_observation()
        print("关节角（度；gripper 是 0-100）：")
        for j in JOINTS:
            print(f"  {j:14s} {obs[f'{j}.pos']:8.2f}")
        state = np.array([obs[f"{j}.pos"] for j in JOINTS])
        print("  →", np.round(state, 2).tolist())
        print(f"\n相机键：{[k for k in obs if not k.endswith('.pos')]}")
        OUT.mkdir(parents=True, exist_ok=True)
        from PIL import Image

        for key in [k for k in obs if not k.endswith(".pos")]:
            arr = obs[key]
            Image.fromarray(arr).save(OUT / f"probe_{key}.png")
            print(f"  {key}: {arr.shape} {arr.dtype} → .tmp/probe_{key}.png")
    finally:
        robot.disconnect()
        print("\n已断开（扭矩已释放）")


if __name__ == "__main__":
    main()
