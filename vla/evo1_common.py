"""Evo-1 推理侧装载：把 MINT 发布的权重接到 lerobot 自带的 evo1 policy 上。

权重（MINT-SJTU 官方发布，只做推理）：
    MINT-SJTU/Evo1_SO100        1552MB  mp_rank_00_model_states.pt（DeepSpeed model states）
    OpenGVLab/InternVL3-1B-hf   1876MB  原生 transformers 格式的 InternVL3 骨干

lerobot 0.6.x 自带 `lerobot/policies/evo1/`。它的 Evo1Model 与 MINT 主分支代码的差别只是
一层前缀：MINT 的 `embedder.*` / `action_head.*` → `model.embedder.*` / `model.action_head.*`。
转换因此是纯改名（外加剥掉 `module.` 外壳），没有任何数值变换，不需要反向工程。

归一化按 checkpoint 里的 norm_stats.json：state / action 都走 min-max（映射到 [-1, 1]），
真实的 6 个关节只占前 6 维，后面 18 维是训练时的补齐位，固定补 0 并把 action_mask 置 False。
"""

from __future__ import annotations

import json
from pathlib import Path

import torch

from lerobot.configs.types import FeatureType, NormalizationMode, PolicyFeature
from lerobot.policies.evo1.configuration_evo1 import Evo1Config
from lerobot.policies.evo1.evo1_model import Evo1Model

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = ROOT / "models"

# 训练时两路相机的顺序（config.json 里 input_features 的键名）
VIEW_KEYS = ("side_env", "front_env")
ARM_DIM = 6


def build_config(
    vlm_dir: str | Path,
    *,
    device: str = "cuda",
    chunk_size: int = 50,
    n_action_steps: int = 25,
    num_inference_timesteps: int = 32,
    use_flash_attn: bool = False,
) -> Evo1Config:
    """SO100 checkpoint 的推理配置。

    只写 checkpoint 里能对上的项：chunk 50 / action dim 24 / 8 层动作头 / embed 896 /
    语言层截到 14 层。flash-attn 默认关（本机没装，也编不出来），退到 eager 注意力。
    """
    cfg = Evo1Config(
        vlm_model_name=str(vlm_dir),
        vlm_num_layers=14,
        dtype=torch.bfloat16,
        device=device,
        image_resolution=(448, 448),
        max_views=len(VIEW_KEYS),
        chunk_size=chunk_size,
        n_action_steps=n_action_steps,
        max_state_dim=24,
        max_action_dim=24,
        embed_dim=896,
        hidden_dim=1024,
        state_hidden_dim=1024,
        num_heads=8,
        num_layers=8,
        num_inference_timesteps=num_inference_timesteps,
        num_categories=1,
        return_cls_only=False,
        use_flash_attn=use_flash_attn,
        use_amp=True,
        normalization_mapping={
            "VISUAL": NormalizationMode.IDENTITY,
            "STATE": NormalizationMode.MIN_MAX,
            "ACTION": NormalizationMode.MIN_MAX,
        },
        input_features={
            f"observation.images.{k}": PolicyFeature(
                type=FeatureType.VISUAL, shape=(3, 448, 448)
            )
            for k in VIEW_KEYS
        },
        output_features={"action": PolicyFeature(type=FeatureType.ACTION, shape=(24,))},
        training_stage="stage2",
    )
    cfg.validate_features()
    return cfg


def load_mint_state_dict(ckpt_path: str | Path) -> dict[str, torch.Tensor]:
    """读 DeepSpeed 存盘，改成 lerobot Evo1Model 的键名。

    action_head 两边完全同名；语言层只多一层 `.model.`；视觉塔是两套 InternVL 实现的差异
    （MINT 训练时用的是 OpenGVLab/InternVL3-1B 的 remote-code 版，lerobot 接的是 -hf 原生版）：

        vision_model                    → vision_tower
        embeddings.class_embedding      → embeddings.cls_token
        embeddings.position_embedding   → embeddings.position_embeddings
        embeddings.patch_embedding.*    → embeddings.patch_embeddings.projection.*
        encoder.layers.N                → encoder.layer.N
        ls1 / ls2                       → lambda_1 / lambda_2
        attn.qkv                        → attention.q/k/v_proj（按 q,k,v 顺序切成三份）
        attn.proj                       → attention.projection_layer
        norm1 / norm2                   → layernorm_before / layernorm_after
        mlp1.0 / 1 / 3                  → multi_modal_projector.layer_norm / linear_1 / linear_2
    """
    obj = torch.load(str(ckpt_path), map_location="cpu", weights_only=False)
    sd = obj.get("module", obj) if isinstance(obj, dict) else obj
    if hasattr(sd, "state_dict"):
        sd = sd.state_dict()

    out: dict[str, torch.Tensor] = {}
    for key, value in sd.items():
        if key.startswith("module."):
            key = key[len("module."):]
        key = key.replace("language_model.model.", "language_model.")

        if ".vision_model." in key or key.startswith("vision_model."):
            key = key.replace("vision_model", "vision_tower")
            key = key.replace("embeddings.class_embedding", "embeddings.cls_token")
            key = key.replace("embeddings.position_embedding", "embeddings.position_embeddings")
            key = key.replace("embeddings.patch_embedding", "embeddings.patch_embeddings.projection")
            key = key.replace("encoder.layers.", "encoder.layer.")
            key = key.replace(".ls1", ".lambda_1").replace(".ls2", ".lambda_2")
            key = key.replace(".norm1.", ".layernorm_before.").replace(".norm2.", ".layernorm_after.")
            key = key.replace(".attn.proj.", ".attention.projection_layer.")

            if ".attn.qkv." in key:
                # (3*hidden, hidden) 按 q / k / v 均分；bias 同理
                q, k, v = value.chunk(3, dim=0)
                base = key.replace(".attn.qkv.", ".attention.")
                for name, part in zip(("q_proj", "k_proj", "v_proj"), (q, k, v)):
                    tail = "weight" if key.endswith(".weight") else "bias"
                    out[base.replace(f".attention.{tail}", f".attention.{name}.{tail}")] = part
                continue

        key = key.replace("mlp1.0.", "multi_modal_projector.layer_norm.")
        key = key.replace("mlp1.1.", "multi_modal_projector.linear_1.")
        key = key.replace("mlp1.3.", "multi_modal_projector.linear_2.")
        out[key] = value
    return out


def load_model(
    weights_dir: str | Path = DEFAULT_MODELS / "evo1_so100",
    vlm_dir: str | Path | None = None,
    *,
    device: str = "cuda",
    num_inference_timesteps: int = 32,
    n_action_steps: int = 25,
    strict: bool = False,
):
    """返回 (model, config, report)。report 里是 strict=False 时的缺失/多余键。"""
    weights_dir = Path(weights_dir)
    vlm_dir = Path(vlm_dir) if vlm_dir else weights_dir.parent / "internvl3-1b-hf"
    cfg = build_config(
        vlm_dir,
        device=device,
        num_inference_timesteps=num_inference_timesteps,
        n_action_steps=n_action_steps,
    )
    model = Evo1Model(cfg).to(device)
    model.eval()

    sd = load_mint_state_dict(weights_dir / "mp_rank_00_model_states.pt")
    report = model.load_state_dict(sd, strict=strict)
    return model, cfg, report


class NormStats:
    """norm_stats.json 的 min-max 归一化（lerobot 的 MIN_MAX 把 x 映射到 [-1, 1]）。"""

    def __init__(self, path: str | Path, key: str = "so100_joint_angle"):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))[key]
        self.state_min = torch.tensor(raw["observation.state"]["min"], dtype=torch.float32)
        self.state_max = torch.tensor(raw["observation.state"]["max"], dtype=torch.float32)
        self.action_min = torch.tensor(raw["action"]["min"], dtype=torch.float32)
        self.action_max = torch.tensor(raw["action"]["max"], dtype=torch.float32)

    @staticmethod
    def _norm(x, lo, hi):
        return (x - lo) / (hi - lo).clamp_min(1e-6) * 2.0 - 1.0

    @staticmethod
    def _denorm(x, lo, hi):
        return (x + 1.0) / 2.0 * (hi - lo) + lo

    def norm_state(self, state: torch.Tensor) -> torch.Tensor:
        """(B, 6) 原始关节角 → (B, 24) 归一化 + 补零。"""
        batch = state.shape[0]
        out = torch.zeros(batch, 24, dtype=torch.float32, device=state.device)
        out[:, :ARM_DIM] = self._norm(
            state[:, :ARM_DIM].cpu(), self.state_min, self.state_max
        ).to(state.device)
        return out

    def denorm_action(self, action: torch.Tensor) -> torch.Tensor:
        """(..., 24) 归一化动作 → 取前 6 维还原成原始关节角。"""
        return self._denorm(action[..., :ARM_DIM].cpu(), self.action_min, self.action_max)

    def describe(self) -> str:
        return (
            "state[min] " + " ".join(f"{v:7.2f}" for v in self.state_min.tolist()) + "\n"
            "state[max] " + " ".join(f"{v:7.2f}" for v in self.state_max.tolist()) + "\n"
            "action[min]" + " ".join(f"{v:7.2f}" for v in self.action_min.tolist()) + "\n"
            "action[max]" + " ".join(f"{v:7.2f}" for v in self.action_max.tolist())
        )


class Evo1Runner:
    """一次前向 = 一段 50 步动作块。输入是 [0,1] 的 HWC 图像列表 + 6 维原始关节角。"""

    def __init__(self, weights_dir=None, *, vlm_dir=None, device="cuda",
                 num_inference_timesteps=32, prompt="", n_action_steps=25):
        weights_dir = Path(weights_dir) if weights_dir else DEFAULT_MODELS / "evo1_so100"
        self.model, self.config, self.report = load_model(
            weights_dir, vlm_dir, device=device,
            num_inference_timesteps=num_inference_timesteps, n_action_steps=n_action_steps,
        )
        self.stats = NormStats(weights_dir / "norm_stats.json")
        self.prompt = prompt
        self.device = device

    @torch.inference_mode()
    def predict(self, images: dict[str, torch.Tensor], state6: torch.Tensor) -> torch.Tensor:
        """images: 键为 `observation.images.<视角名>`，值 (1, 3, H, W) 且值域 [0, 1]。
        state6: (1, 6) 原始关节角。返回 (chunk, 6) 原始关节角（已反归一化）。
        """
        ordered = [images[f"observation.images.{k}"].to(self.device, torch.float32) for k in VIEW_KEYS]
        state6 = torch.as_tensor(state6, dtype=torch.float32, device="cpu").reshape(1, ARM_DIM)
        norm_state = self.stats.norm_state(state6).to(self.device)
        mask = torch.zeros(1, self.config.max_action_dim, dtype=torch.bool, device=self.device)
        mask[:, :ARM_DIM] = True

        image_mask = torch.ones(1, len(VIEW_KEYS), dtype=torch.bool, device=self.device)

        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=self.device == "cuda"):
            tokens, ctx_mask = self.model.get_vl_embeddings(
                ordered, image_mask, [self.prompt], return_cls_only=False
            )
            flat = self.model.action_head.get_action(
                tokens,
                state=norm_state,
                embodiment_id=torch.zeros(1, dtype=torch.long, device=self.device),
                action_mask=mask,
                context_mask=ctx_mask,
            )
        chunk = flat.view(1, self.config.chunk_size, self.config.max_action_dim)[0]
        return self.stats.denorm_action(chunk)


class LeRobotEvo1:
    """LeRobot 原生格式的 Evo-1 checkpoint（model.safetensors + policy_preprocessor.json）。

    和 `Evo1Runner` 的区别：归一化、padding、图像缩放全在 checkpoint 自带的
    pre/post processor 里，状态是真实的 6 维，不需要我们从 norm_stats 手搓。
    """

    def __init__(self, path: str | Path, *, device: str = "cuda", prompt: str = ""):
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.policies.evo1.modeling_evo1 import Evo1Policy
        from lerobot.policies.factory import make_pre_post_processors

        self.cfg = PreTrainedConfig.from_pretrained(str(path))
        self.policy = Evo1Policy.from_pretrained(str(path)).to(device).eval()
        self.pre, self.post = make_pre_post_processors(self.cfg, pretrained_path=str(path))
        self.image_keys = [k for k in self.cfg.input_features if k.startswith("observation.images.")]
        self.prompt = prompt
        self.device = device
        self.report = None

    @torch.inference_mode()
    def predict(self, images: dict[str, torch.Tensor], state6: torch.Tensor) -> torch.Tensor:
        batch = {"observation.state": torch.as_tensor(state6, dtype=torch.float32).reshape(1, -1)}
        for key in self.image_keys:
            batch[key] = images[key].to(torch.float32)
        batch["task"] = [self.prompt]
        prepared = self.pre(batch)
        chunk = self.policy.predict_action_chunk(prepared)
        out = self.post(chunk)
        act = out["action"] if isinstance(out, dict) else out
        return act.reshape(-1, act.shape[-1]).float().cpu()


def prepare_image(frame, size: int = 448, bgr: bool = False) -> torch.Tensor:
    """uint8 图像 → (1, 3, 448, 448)，值域 [0, 1]，RGB、不裁剪、直接缩放（与训练一致）。

    lerobot 的相机默认已经给 RGB（`ColorMode.RGB`），所以 bgr 默认 False。
    """
    import numpy as np

    rgb = frame[:, :, ::-1] if bgr else frame
    tensor = torch.from_numpy(np.ascontiguousarray(rgb)).float().div_(255.0)
    tensor = tensor.permute(2, 0, 1).unsqueeze(0)
    if tensor.shape[-1] != size or tensor.shape[-2] != size:
        tensor = torch.nn.functional.interpolate(
            tensor, size=(size, size), mode="bicubic", antialias=True
        ).clamp_(0, 1)
    return tensor
