"""Evo-1 冒烟测试：不接机械臂，只验证权重装得上、显存够、出得来合理的动作块。

    conda activate lerobot
    python /mnt/d/CUHKSZ/AIE3001/Project/vla/evo1_smoke.py

判据不是"动作好看"，而是三件事：
  1. 键名全部对上（缺失 0 / 多余 0，或缺失项都是可忽略的 buffer）；
  2. 4GB 显存不炸；
  3. 输出的 50×6 关节角落在数据集的 min/max 附近（差一个数量级就说明归一化或单位错了）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evo1_common import DEFAULT_MODELS, Evo1Runner, NormStats  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=str(DEFAULT_MODELS / "evo1_so100"))
    ap.add_argument("--vlm", default=str(DEFAULT_MODELS / "internvl3-1b-hf"))
    ap.add_argument("--prompt", default="Pick up the red block and put it in the black area.")
    ap.add_argument("--steps", type=int, default=32, help="flow matching 积分步数")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    print(f"CUDA  {torch.cuda.get_device_name(0)}  {torch.cuda.get_device_properties(0).total_memory/1e9:.1f} GB")

    t0 = time.time()
    runner = Evo1Runner(
        args.weights, vlm_dir=args.vlm, device=args.device,
        num_inference_timesteps=args.steps, prompt=args.prompt,
    )
    print(f"装载完成 {time.time()-t0:.1f}s")
    print(f"  缺失键 {len(runner.report.missing_keys)}  多余键 {len(runner.report.unexpected_keys)}")
    for k in runner.report.missing_keys[:6]:
        print(f"    missing  {k}")
    for k in runner.report.unexpected_keys[:6]:
        print(f"    extra    {k}")

    stats = NormStats(Path(args.weights) / "norm_stats.json")
    print("\n数据集里的关节角范围（训练单位）：")
    print(stats.describe())

    n_param = sum(p.numel() for p in runner.model.parameters())
    print(f"\n参数量 {n_param/1e6:.0f}M")
    if args.device == "cuda":
        torch.cuda.synchronize()
        print(f"装载后显存 {torch.cuda.memory_allocated()/1e9:.2f} GB  "
              f"(峰值 {torch.cuda.max_memory_allocated()/1e9:.2f} GB)")

    # 用数据集范围内的中间姿态 + 中灰图像，只为了跑通前向
    mid = (stats.state_min + stats.state_max) / 2
    state = mid.reshape(1, 6)
    images = {
        "observation.images.side_env": torch.full((1, 3, 448, 448), 0.5),
        "observation.images.front_env": torch.full((1, 3, 448, 448), 0.4),
    }

    for i in range(args.runs):
        t0 = time.time()
        chunk = runner.predict(images, state)
        dt = time.time() - t0
        if i == 0:
            print(f"\n前向 {dt*1000:.0f} ms/块  （{args.steps} 步积分，50 步动作块）")
        else:
            print(f"前向 {dt*1000:.0f} ms/块")
        if i == 0:
            print("\n动作块头 3 行、末行（原始关节角，单位同 norm_stats）：")
            for row in torch.cat([chunk[:3], chunk[-1:]]):
                print("  " + " ".join(f"{v:8.2f}" for v in row.tolist()))
            lo, hi = stats.action_min, stats.action_max
            out_of_range = ((chunk < lo - 5) | (chunk > hi + 5)).float().mean().item()
            print(f"\n超出数据集动作范围 5 以上的比例：{out_of_range*100:.1f}%")
            print(f"前 10 步变化量（每关节）："
                  + " ".join(f"{v:7.2f}" for v in (chunk[:10].max(0).values - chunk[:10].min(0).values).tolist()))

    if args.device == "cuda":
        print(f"\n峰值显存 {torch.cuda.max_memory_allocated()/1e9:.2f} GB / "
              f"{torch.cuda.get_device_properties(0).total_memory/1e9:.1f} GB")


if __name__ == "__main__":
    main()
