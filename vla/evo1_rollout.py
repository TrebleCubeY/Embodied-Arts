"""在实臂上跑 Evo-1 推理，并把动作与跟随画面同时推给数字层。

和 `robot/rollout_bridge.py` 是同一个思路：推理进程独占 follower 串口，所以只能在这个进程里
取观测 → 顺手转 68 字节帧 → 推给 twin 的 `:8765`。区别是不再套 lerobot 的 rollout 入口，
自己写执行循环（Evo-1 的动作块 50 步，跑满再重规划，`--replan` 控制实际执行多少步）。

    conda activate lerobot
    python /mnt/d/CUHKSZ/AIE3001/Project/vla/evo1_rollout.py --front 0 --side 2 --dry-run

`--dry-run` 只打印模型给的目标角、不写舵机 —— 先确认方向对不对再上真机。
确认无误去掉 `--dry-run`，同时别开 `robot_bridge.py`（抢同一个串口）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "robot"))

import robot_bridge as rb  # noqa: E402
import rollout_bridge as rbr  # noqa: E402
from evo1_common import DEFAULT_MODELS, Evo1Runner, LeRobotEvo1, prepare_image  # noqa: E402

JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


def obs_to_state(obs: dict) -> np.ndarray:
    return np.array([float(obs[f"{j}.pos"]) for j in JOINTS], dtype=np.float32)


def build_robot(args):
    from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig
    from lerobot.robots.so_follower.config_so_follower import SOFollowerRobotConfig
    from lerobot.robots.so_follower.so_follower import SOFollower

    cams = {}
    if args.scene >= 0:
        cams["scene"] = OpenCVCameraConfig(fps=args.fps, width=args.width, height=args.height,
                                           index_or_path=args.scene, fourcc=args.fourcc)
    if args.wrist >= 0:
        cams["wrist"] = OpenCVCameraConfig(fps=args.fps, width=args.width, height=args.height,
                                           index_or_path=args.wrist, fourcc=args.fourcc)
    cfg = SOFollowerRobotConfig(
        port=args.port, id=args.id, cameras=cams, use_degrees=True, disable_torque_on_disconnect=True
    )
    return SOFollower(cfg)


def parse_views(text: str) -> dict[str, str]:
    """`top=scene,wrist=wrist` → {模型视角: 相机名}。视角名可以是简化名也可以是完整特征键。"""
    mapping = {}
    for part in text.split(","):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and value != "none":
            mapping[key] = value
    return mapping


def feature_key(view: str) -> str:
    return view if view.startswith("observation.") else f"observation.images.{view}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Evo-1 实臂推理 + 数字层推帧")
    ap.add_argument("--port", default="/dev/ttyACM0", help="follower 串口（注意不是 leader）")
    ap.add_argument("--id", default="zyf_follower")
    ap.add_argument("--scene", type=int, default=2, help="第三人称视角相机索引（本机实测 2），-1 不用")
    ap.add_argument("--wrist", type=int, default=0, help="腕部相机索引（本机实测 0），-1 不用")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fourcc", default="MJPG")
    ap.add_argument("--views", default="",
                    help="模型视角 → 相机名（wrist/scene）。不给就按 checkpoint 的输入特征自动配："
                         "side_env/top→scene，front_env/wrist→wrist")
    ap.add_argument("--prompt", default="Pick up the red block and put it in the black area.")
    ap.add_argument("--policy", default="",
                    help="LeRobot 格式 checkpoint 目录（推荐 models/evo1_so101_cube）")
    ap.add_argument("--weights", default="",
                    help="MINT DeepSpeed 权重目录（models/evo1_so100），不给 --policy 时才用")
    ap.add_argument("--vlm", default=str(DEFAULT_MODELS / "internvl3-1b-hf"))
    ap.add_argument("--steps", type=int, default=0,
                    help="flow matching 积分步数；0 = 用 checkpoint 自带的值")
    ap.add_argument("--hz", type=float, default=30.0, help="动作下发频率")
    ap.add_argument("--replan", type=int, default=25, help="每块 50 步里实际执行多少步后重规划")
    ap.add_argument("--duration", type=float, default=10.0, help="跑几秒，0 = 一直跑")
    ap.add_argument("--max-step-deg", type=float, default=8.0, help="单步允许的最大关节变化（度）")
    ap.add_argument("--margin", type=float, default=10.0, help="在数据集动作范围外再放开的度数")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写舵机")
    ap.add_argument("--ws-port", type=int, default=8765)
    ap.add_argument("--push-hz", type=float, default=30.0)
    ap.add_argument("--offsets", default=str(rb.DEFAULT_OFFSETS))
    ap.add_argument("--no-twin", action="store_true", help="不推帧给数字层")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not args.policy and not args.weights:
        args.policy = str(DEFAULT_MODELS / "evo1_so101_cube")

    if not args.no_twin:
        offsets, found = rb.load_offsets(Path(args.offsets))
        if not found:
            print(f"[!] 没找到 {args.offsets}，零位偏移按 0")
        latest = rb.Latest(offsets)
        try:
            rbr.start_server(latest, "0.0.0.0", args.ws_port, args.push_hz)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] WS 起不来（{exc!r}），先停掉 robot_bridge 或换 --ws-port")
            return 1
        rbr._ACTIVE["publisher"] = rbr.Publisher(latest, args.quiet)
        rbr.install_hook()
        print(f"数字层推帧 ws://0.0.0.0:{args.ws_port}（{args.push_hz:g}Hz）")

    robot = build_robot(args)

    print("装载 Evo-1 …", flush=True)
    if args.policy:
        runner = LeRobotEvo1(args.policy, device="cuda", prompt=args.prompt)
        if args.steps:
            runner.cfg.num_inference_timesteps = args.steps
            runner.policy.config.num_inference_timesteps = args.steps
        if args.replan:
            runner.cfg.n_action_steps = args.replan
            runner.policy.config.n_action_steps = args.replan
        views = parse_views(args.views) or {
            "top": "scene", "wrist": "wrist", "side_env": "scene", "front_env": "wrist",
        }
        lo = np.array([-120.0, -110.0, -60.0, 10.0, -180.0, -10.0])
        hi = np.array([40.0, 70.0, 120.0, 110.0, 60.0, 105.0])
        print(f"  checkpoint 视角 {runner.image_keys}")
    else:
        weights = Path(args.weights)
        runner = Evo1Runner(weights, vlm_dir=args.vlm, device="cuda",
                            num_inference_timesteps=args.steps or 10, prompt=args.prompt)
        print(f"  缺失键 {len(runner.report.missing_keys)} / 多余键 {len(runner.report.unexpected_keys)}")
        views = parse_views(args.views) or {"side_env": "wrist", "front_env": "scene"}
        lo = (runner.stats.action_min - args.margin).numpy()
        hi = (runner.stats.action_max + args.margin).numpy()
    print(f"  视角映射 {views}")
    print(f"  动作包络 {np.round(lo, 1).tolist()} .. {np.round(hi, 1).tolist()}")

    robot.connect()
    try:
        robot.bus.enable_torque()   # lerobot 的 connect() 不开扭矩，不写这一句 send_action 静默无效
        print("扭矩已开（臂会撑住当前姿态）")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 开扭矩失败（{exc!r}），舵机会不响应")
    print(f"已连 {args.port}；{args.hz:g}Hz 下发，每块执行 {args.replan} 步"
          f"{'（dry-run，不写舵机）' if args.dry_run else ''}\n", flush=True)

    deadline = None if args.duration <= 0 else time.monotonic() + args.duration
    last_sent = None
    sent_total = 0
    chunks = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            tick = time.monotonic()
            obs = robot.get_observation()
            state = obs_to_state(obs)

            for view, cam in views.items():
                if cam not in obs:
                    print(f"[!] 观测里没有相机 {cam}（有 {[k for k in obs if not k.endswith('.pos')]}）")
                    return 1

            images = {feature_key(view): prepare_image(obs[cam]) for view, cam in views.items()}
            t_inf = time.monotonic()
            chunk = runner.predict(images, torch.from_numpy(state).reshape(1, 6)).numpy()
            t_inf = time.monotonic() - t_inf

            queue = []
            for row in chunk[: max(1, args.replan)]:
                prev = last_sent if last_sent is not None else state
                safe = np.clip(row, prev - args.max_step_deg, prev + args.max_step_deg)
                safe = np.clip(safe, lo, hi)
                queue.append(safe)
                last_sent = safe

            chunks += 1
            raw0 = chunk[0]
            print(f"  块 {chunks:3d}  推理 {t_inf*1000:4.0f}ms  Δ(首动作−当前) "
                  + " ".join(f"{v:+7.1f}" for v in (raw0 - state)), flush=True)

            for act in queue:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if not args.dry_run:
                    robot.send_action({f"{j}.pos": float(v) for j, v in zip(JOINTS, act)})
                obs = robot.get_observation()   # 触发推帧
                sent_total += 1
                gap = 1.0 / args.hz - (time.monotonic() - tick)
                if gap > 0:
                    time.sleep(gap)
                tick = time.monotonic()
    except KeyboardInterrupt:
        print("\n被 Ctrl+C 打断")
    finally:
        robot.disconnect()
        print(f"\n结束：{chunks} 块，下发 {sent_total} 步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
