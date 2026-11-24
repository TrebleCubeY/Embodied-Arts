# vla · Evo-1 推理接实臂

只跑开源权重，不训练。两个 checkpoint 都验证过（2026-11-24）：
官方 `MINT-SJTU/Evo1_SO100`（pick-place，需转换）与社区 `bigdra/evo1_so101_cube_put_take`（SO101，LeRobot 原生格式）。

细节与踩坑见 `preparation/tech/数字层技术方案_v0.2.md` 第 24 节。

## 一次性准备

WSL，`conda activate lerobot`，然后：

```bash
python -m pip install 'transformers>=5.4.0,<5.6.0' accelerate einops timm
python - <<'EOF'
from huggingface_hub import snapshot_download
snapshot_download('OpenGVLab/InternVL3-1B-hf', local_dir='/mnt/d/CUHKSZ/AIE3001/Project/models/internvl3-1b-hf')
snapshot_download('bigdra/evo1_so101_cube_put_take', local_dir='/mnt/d/CUHKSZ/AIE3001/Project/models/evo1_so101_cube')
snapshot_download('MINT-SJTU/Evo1_SO100', local_dir='/mnt/d/CUHKSZ/AIE3001/Project/models/evo1_so100')
EOF
```

约 5.2GB，落在 `models/`（已 gitignore）。`models/evo1_so101_cube/config.json` 里已经改好三处：
`vlm_model_name` 指向本地 InternVL、`max_views=2`、`num_inference_timesteps=10`。

**别改回 `max_views=3`**：4GB 卡上会溢出到主机内存，单块从 0.68s 变 2.9s。

## 一次完整的跑

1. 挂设备（串口空闲 20 秒会自己掉，每次跑之前都要挂）

```powershell
'4-1','4-2','4-3','4-4' | ForEach-Object { usbipd attach --wsl --busid $_ }
```

2. 看一眼姿态和机位（不动舵机）

```bash
python /mnt/d/CUHKSZ/AIE3001/Project/vla/probe_scene.py --port /dev/ttyACM1
```

3. 空跑：只打印模型给的动作，不写舵机

```bash
python /mnt/d/CUHKSZ/AIE3001/Project/vla/evo1_rollout.py --port /dev/ttyACM1 --dry-run --duration 8
```

4. 真跑（臂会动，先确认桌面清空）

```bash
python /mnt/d/CUHKSZ/AIE3001/Project/vla/evo1_rollout.py --port /dev/ttyACM1 --duration 20 --max-step-deg 3
```

窗口 2 起后端，页面就能看到实时跟随：`python twin/server/main.py --source remote --remote-url ws://localhost:8765`

5. 收工把臂挪回原位

```bash
python /mnt/d/CUHKSZ/AIE3001/Project/vla/restore_pose.py --port /dev/ttyACM1 --seconds 5
```

`restore_pose.py` 的默认目标是 2026-11-24 试跑前的姿态 `-4.97,-109.58,96.09,75.74,49.36,1.15`；
换了起点就用 `--target "…"` 传六个数（五个关节角度 + 夹爪 0-100）。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--policy` | `models/evo1_so101_cube` | LeRobot 格式 checkpoint 目录 |
| `--weights` | 空 | 给了就走 MINT DeepSpeed 权重（`models/evo1_so100`） |
| `--scene` / `--wrist` | 2 / 0 | 相机索引：2 = 第三人称，0 = 腕部 |
| `--views` | 自动 | 模型视角 → 相机名，如 `top=scene,wrist=wrist` |
| `--prompt` | put the cube into the box | 语言指令（社区权重训的就是 cube put-take） |
| `--replan` | 25 | 每块 50 步里执行多少步后重规划 |
| `--max-step-deg` | 8 | 单步允许的最大关节变化，越小越慢越安全 |
| `--no-twin` | 关 | 不推帧给数字层 |
| `--dry-run` | 关 | 只打印不写舵机 |

## 报错速查

| 现象 | 原因 |
|---|---|
| `send_action` 写了但臂不动 | 没开扭矩。lerobot 的 `connect()` 不开，要 `robot.bus.enable_torque()` |
| 读出来全是 5.4V、关节角永远不变 | 连的是 **leader**（ACM0）。follower 是 ACM1，电压 12V |
| `Could not connect on port /dev/ttyACM1` | 串口空闲被挂了，重跑一次 `usbipd attach` |
| `ValueError: color_mode is expected to be in …` | `OpenCVCameraConfig` 位置参数是 (fps, width, height, index_or_path)，用关键字 |
| 单块推理 3 秒 | `max_views` 还是 3，显存溢出 |
| 632 个键 missing / unexpected | 构造 `Evo1Model` 时多加了 `model.` 前缀（那是 `Evo1Policy` 的层级） |
