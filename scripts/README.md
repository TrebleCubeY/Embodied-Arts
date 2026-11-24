# 演示启动

一条命令起全套：WSL 里的桥、Windows 里的后端、两块观众屏上的页面。

## 前提

少一样就会在对应那一步报红，报红时会说清是哪一项。

| 项 | 要求 | 自己查一下 |
|---|---|---|
| Windows Python | `D:\conda_envs\so101-twin\Scripts\python.exe` 在，装了 websockets | `D:\conda_envs\so101-twin\Scripts\python.exe -c "import websockets"` |
| Chrome | `C:\Program Files\Google\Chrome\Application\chrome.exe` | 文件存在即可 |
| WSL | 发行版 `Ubuntu`，`~/miniconda3/envs/lerobot` 里装了 lerobot | `wsl -d Ubuntu -- bash -lc '~/miniconda3/envs/lerobot/bin/python -c "import lerobot; print(lerobot.__version__)"'` |
| 串口线 | 两条 CH343（VID `1a86:55d3`），follower 挂 `/dev/ttyACM1`、leader 挂 `/dev/ttyACM0` | `usbipd list` |
| usbipd bind | **只需做一次**（要管理员） | `usbipd bind --busid 4-1` |
| usbipd attach | **WSL 每次重开都要重做**（要管理员）。脚本会自己试，失败了会提示 | `usbipd attach --wsl --busid 4-1` |
| 校准文件 | `~/.cache/huggingface/lerobot/calibration/robots/so_follower/zyf_follower.json` 与 `.../teleoperators/so_leader/zyf_leader.json` | 两个文件都在 |
| 零位偏移 | `robot/offsets.json`（换过校准就要重量） | `python robot/robot_bridge.py --capture-zero --port /dev/ttyACM1 --id zyf_follower` |

`-NoRobot` 可以跳过桥这一整段，屏幕上照样能演完三个动作（臂不动）。

## 起

双击 `scripts\start_demo.cmd`，或者命令行：

```bat
scripts\start_demo.cmd                          rem 默认：停住档，两块外接屏各一窗
scripts\start_demo.cmd -Mode teleop             rem 直接进遥操
scripts\start_demo.cmd -SideBySide              rem 本机单屏：两个窗口并排
scripts\start_demo.cmd -NoRobot -SideBySide     rem 不接机械臂，只演屏幕
scripts\start_demo.cmd -ListScreens             rem 只看屏幕编号
```

两块外接屏 + 一块本机屏时，脚本默认跳过主屏、把两块外接屏给观众：

```bat
scripts\start_demo.cmd -ScreenA 1 -ScreenB 2    rem 编号不对就手动指定
scripts\start_demo.cmd -Swap                    rem 两块屏的视图对调
```

## 停

双击 `scripts\stop_demo.cmd`。

先停后端，再停桥。桥收到信号会走正常收尾，释放扭矩，**臂会软下来，先托住它**。

`stop_bridge.sh` 先发 SIGINT，5 秒没退再 SIGTERM，最后才 SIGKILL。**只发 SIGINT 是不够的**：nohup 起的后台作业会继承「忽略 SIGINT」，发了等于没发。

跳过桥（保留机械臂使能）：`scripts\stop_demo.cmd -KeepBridge`（要那就有 `scripts\stop.ps1` 直调）。

## 参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `-Mode` | `hold` | 起手档位：`hold` 停住 / `teleop` 遥操跟随 / `keyframes` 自动播预设动作 |
| `-View` | `both` | 单窗时的视图：`both` 两块并排 / `twin` 只有数字层 / `code` 只有代码层 |
| `-SideBySide` | 关 | 本机单屏：两个窗口各占半屏 |
| `-ScreenA` `-ScreenB` | 自动 | 指定屏幕编号（先 `-ListScreens`） |
| `-Swap` | 关 | 两块屏上的视图对调 |
| `-NoRobot` | 关 | 不起桥，后端用动作库当替身 |
| `-NoBrowser` | 关 | 只起服务不开窗口 |
| `-NoAttach` | 关 | 跳过 usbipd attach |
| `-Port` | 8000 | 后端 HTTP 端口（WS 是 +1） |

运行中切档：页面代码层最上面三个按钮，或键盘 `1` 遥操 / `2` 自动·预设 / `3` 停住 / `0` 暂停继续。

## 报错速查

| 看到什么 | 原因 | 怎么修 |
|---|---|---|
| `WSL 里没有 /dev/ttyACM*` | 设备没 attach（WSL 重开过、线重插过） | 管理员 PowerShell 跑 `usbipd attach --wsl --busid 4-1` 和 `4-2`，或重跑脚本 |
| `usbipd 里没看到 CH343 串口` | 线没插 / 驱动没装 | 换口，设备管理器看有没有 COM 口 |
| `桥没起来` + 日志里 `enable torque timeout` | follower 总线上有舵机没响应（常是刚 attach 完总线没稳，或供电不足） | 等两秒重跑；仍失败就检查电源和线 |
| `enable torque timeout on 'wrist_flex'(ID 4)` | 中段舵机掉线，通常是线松 | 重新插紧腕部那条线 |
| 端口被占（`WinError 10048`） | 上次没停干净 | 脚本会自动清；手工：`netstat -ano \| findstr :8000` 再 `taskkill /F /PID` |
| `桥起来了，但 leader 没连上` / 清单里 `遥操 不可用` | leader 串口开了但没连上：校准提问没过去、口反了、或线松 | 看 `.tmp\bridge.log` 里 `leader 连不上` 那行。两个 CH343 都能握手，只有 `Present_Voltage` 能区分（leader ≈ 5.4V，应是 `/dev/ttyACM0`）。要重新校准：WSL 里加 `--interactive-leader` 手动起一次 |
| 页面「状态帧 重连中」但 `.tmp\bridge.log` 里读数 50Hz、写入也在跑 | 后端连不上桥：WSL2 的 localhost 转发（`wslrelay`）没起来 | 脚本已经改成用 WSL 的 IP 连桥（看清单里 `桥 ws://172.x.x.x:8765`）。手工起后端时：`--remote-url ws://$(wsl hostname -I \| awk '{print $1}'):8765` |
| 页面「状态帧 重连中」 | 后端没起，或页面连的不是 8001 | 看 `.tmp\backend.err.log` |
| 页面能开但数字层不动 | 桥在 `hold` 档（不写指令） | 按 `2` 或 `1` |
| 臂变硬、掰不动 | 三档都要求舵机使能，这是正常的 | 想手掰：`scripts\stop_demo.cmd` 或 `-KeepBridge` 后用 `--release-torque` 起桥 |
