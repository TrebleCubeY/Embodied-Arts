"""加载 LeRobot 格式的 Evo-1 checkpoint，用两张实拍图跑一次推理，打印动作块。

    conda activate lerobot
    python /mnt/d/CUHKSZ/AIE3001/Project/vla/evo1_lerobot_check.py

用来回答"这个 checkpoint 在我们的场景里给出什么动作"，不碰串口、不动舵机。
图片默认读 `.tmp/probe_*.png`（vla/probe_scene.py 存的）。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = ROOT / "models" / "evo1_so101_cube"


def load_image(path: Path) -> torch.Tensor:
    from PIL import Image

    arr = np.asarray(Image.open(path).convert("RGB"))
    t = torch.from_numpy(arr).float().div_(255.0).permute(2, 0, 1).unsqueeze(0)
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default=str(DEFAULT_POLICY))
    ap.add_argument("--top", default=str(ROOT / ".tmp" / "probe_side.png"))
    ap.add_argument("--wrist", default=str(ROOT / ".tmp" / "probe_front.png"))
    ap.add_argument("--prompt", default="put the cube into the box")
    ap.add_argument("--state", default="-6.02,-77.85,112.26,46.20,-87.52,0.0")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.policies.evo1.modeling_evo1 import Evo1Policy
    from lerobot.policies.factory import make_pre_post_processors

    cfg = PreTrainedConfig.from_pretrained(args.policy)
    print(f"type={cfg.type}  chunk={cfg.chunk_size}  n_action_steps={cfg.n_action_steps}  "
          f"steps={cfg.num_inference_timesteps}  vlm={cfg.vlm_model_name}")
    print(f"输入特征 {list(cfg.input_features)}")

    t0 = time.time()
    policy = Evo1Policy.from_pretrained(args.policy).to(args.device).eval()
    pre, post = make_pre_post_processors(cfg, pretrained_path=args.policy)
    print(f"装载 {time.time()-t0:.1f}s   显存 {torch.cuda.memory_allocated()/1e9:.2f} GB")

    state = torch.tensor([[float(v) for v in args.state.split(",")]], dtype=torch.float32)
    batch = {
        "observation.state": state,
        "observation.images.top": load_image(Path(args.top)),
        "observation.images.wrist": load_image(Path(args.wrist)),
        "task": [args.prompt],
    }
    prepared = pre(batch)
    print("预处理后：")
    for k, v in prepared.items():
        if isinstance(v, torch.Tensor):
            print(f"  {k:28s} {tuple(v.shape)} {v.dtype}  min {v.min().item():.3f} max {v.max().item():.3f}")
        else:
            print(f"  {k:28s} {v!r}")

    for i in range(2):
        t0 = time.time()
        with torch.inference_mode():
            chunk = policy.predict_action_chunk(prepared)
            out = post(chunk)
        dt = time.time() - t0
        print(f"\n前向 {dt*1000:.0f} ms  输出类型 {type(out)}")
        act = out["action"] if isinstance(out, dict) else out
        act = act.reshape(-1, act.shape[-1]).float()
        print(f"  形状 {tuple(act.shape)}")
        print("  前 3 行 / 末行：")
        for row in torch.cat([act[:3], act[-1:]]):
            print("    " + " ".join(f"{v:8.2f}" for v in row.tolist()))
        if i == 0:
            d = (act[0] - state[0]).tolist()
            print("  第一动作 − 当前姿态：" + " ".join(f"{v:+7.2f}" for v in d))
            print("  前 10 步各关节变化量：" + " ".join(
                f"{v:7.2f}" for v in (act[:10].max(0).values - act[:10].min(0).values).tolist()))

    print(f"\n峰值显存 {torch.cuda.max_memory_allocated()/1e9:.2f} GB / "
          f"{torch.cuda.get_device_properties(0).total_memory/1e9:.1f} GB")


if __name__ == "__main__":
    main()
