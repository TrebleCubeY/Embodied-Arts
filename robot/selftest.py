#!/usr/bin/env python3
"""桥进程 + 数据源自检，不需要机械臂。

    D:/conda_envs/so101-twin/Scripts/python.exe robot/selftest.py
    python robot/selftest.py --url ws://127.0.0.1:8765     # 对着已起来的桥跑

没找到桥就自己用 --fake 起一个（临时，测完杀掉）。检查的是能判定的东西：
换算公式、offset 文件两种写法、帧长/帧率/序号、target==actual、断流不补零。
真机姿态对不对这件事自检判不了 —— 那要靠眼睛看。
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "twin" / "server"))

import protocol  # noqa: E402
import robot_bridge as rb  # noqa: E402
import sources  # noqa: E402

OK = True


def chk(cond, label, detail=""):
    global OK
    OK = OK and bool(cond)
    print(f"  {'通过' if cond else '不通过'}  {label}{('  — ' + detail) if detail else ''}")


def wait_port(port, timeout=8.0):
    import socket

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def check_frame(url, n=45):
    from websockets.sync.client import connect

    with connect(url, proxy=None) as c:
        t0 = time.perf_counter()
        frames, hello = [], None
        while len(frames) < n:
            msg = c.recv()
            if isinstance(msg, str):
                hello = hello or json.loads(msg)
                continue
            frames.append(protocol.unpack_frame(msg))
        dt = time.perf_counter() - t0
    chk(hello and hello.get("frameSize") == protocol.FRAME_SIZE, "连上就发 hello（含 frameSize）")
    chk(all(len(f["actual"]) == 6 for f in frames), "帧长 68 字节、六个关节")
    seqs = [f["seq"] for f in frames]
    chk(all(b == a + 1 for a, b in zip(seqs, seqs[1:])), "seq 连续", f"{seqs[0]} → {seqs[-1]}")
    chk(all(b > a for a, b in zip((f["t"] for f in frames), (f["t"] for f in frames[1:]))), "t 单调递增")
    hz = len(frames) / dt
    chk(20 <= hz <= 40, "推帧 30Hz 量级", f"{hz:.1f} fps")
    chk(all(f["mode"] == 0 and f["blend"] == 255 for f in frames), "桥只报跟随（mode 0 / blend 255）")
    chk(all(f["target"] == f["actual"] for f in frames), "target 与 actual 一致（偏离由后端算）")
    span = max(max(f["actual"][:5]) - min(f["actual"][:5]) for f in frames)
    chk(span > 0.01, "六关节在动", f"极差 {span:.1f}°")
    return frames


def main() -> int:
    url = sys.argv[sys.argv.index("--url") + 1] if "--url" in sys.argv else "ws://127.0.0.1:8765"
    port = int(url.rsplit(":", 1)[1].split("/")[0])

    print("换算")
    chk(abs(rb.grip_to_deg(0) + 10) < 1e-9, "grip 0 → -10°（闭合）")
    chk(abs(rb.grip_to_deg(100) - 100) < 1e-9, "grip 100 → 100°（张开）")
    obs = {f"{n}.pos": float(i) for i, n in enumerate(rb.JOINTS[:5])}
    obs["gripper.pos"] = 100.0
    deg, grip = rb.to_twin_deg(obs, [10.0, -10.0, 0, 0, 0, 0])
    chk(deg[:5] == [10.0, -9.0, 2.0, 3.0, 4.0] and deg[5] == 100.0, "关节加 offset、夹爪走映射", str(deg))

    print("参考姿态")
    ref = rb.parse_ref_pose(rb.REF_POSE_DEFAULT)
    chk(ref == [0.0, 0.0, 0.0, 0.0, -90.0, 0.0], "默认参考姿态是腕部 -90（数字层静止姿态）", str(ref))
    chk(rb.parse_ref_pose("0,0,0,0,-90,0") == ref, "六个数写法与 k=v 一致")
    chk(rb.parse_ref_pose("") == [0.0] * 6, "空串 = 全 0")
    # 实机在那姿态读到的数是 raw，offset 必须是 ref - raw，重算回去正好等于 ref
    med = [1.0, -2.0, 3.0, -4.0, 5.0, 60.0]
    off = rb.offsets_from(med, ref)
    chk(all(abs(a + b - r) < 1e-9 for a, b, r in zip(off[:5], med[:5], ref[:5])), "offset = 参考 − 读数", str(off))
    chk(off[5] == 0.0, "夹爪 offset 恒 0")

    # 2026-10-24 实测：实机摆的静态姿态读数是腕 ~0、其余几度，于是腕部 offset ≈ -87
    med = [-4.5, 5.3, -1.5, 7.2, -2.6, 2.6]
    off = rb.offsets_from(med, ref)
    chk(abs(off[4] + 87.4) < 0.5 and max(abs(v) for v in off[:4]) < 8.0, "实测那组算出来的样子（腕 ≈ -87）", str(off))

    print("offsets 文件")
    tmp = Path(tempfile.mkdtemp(dir=str(ROOT / ".tmp")))
    p = tmp / "arr.json"
    p.write_text(json.dumps([1, 2, 3, 4, 5, 6]), encoding="utf-8")
    chk(rb.load_offsets(p) == ([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], True), "数组写法")
    p2 = tmp / "named.json"
    p2.write_text(json.dumps({"shoulder_pan": -3.5}), encoding="utf-8")
    off, _ = rb.load_offsets(p2)
    chk(off[0] == -3.5 and off[1:] == [0.0] * 5, "按关节名写法，缺的补 0")
    p3 = tmp / "captured.json"  # capture-zero 实际打印的格式
    p3.write_text(
        json.dumps({"note": "…", "reference": [0, 0, 0, 0, -90, 0], "offsets": {"shoulder_pan": -3.5}}),
        encoding="utf-8",
    )
    off, found = rb.load_offsets(p3)
    chk(found and off[0] == -3.5 and off[4] == 0.0, "capture-zero 的输出格式能被读回")
    chk(rb.load_offsets(tmp / "none.json") == ([0.0] * 6, False), "文件不存在 → 全 0 并标记")

    print("断流")
    dead = sources.RemoteStateSource(url="ws://127.0.0.1:9/state", wait_first=0.5)
    chk(dead.sample(0) == [0.0] * 6, "连不上时不出帧、sample 给零位")
    chk("未连上" in dead.describe(0)[1], "describe 说清没连上", dead.describe(0)[1])
    dead.close()

    proc = None
    if not wait_port(port, timeout=0.5):
        print(f"\n没找到 {url}，用 --fake 起一个临时的桥")
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "robot_bridge.py"), "--fake", "--teleop", "--ws-port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not wait_port(port):
            print("  起不来，自检中止")
            return 1

    try:
        print("桥 → 帧")
        check_frame(url)
        print("数据源")
        src = sources.RemoteStateSource(url=url, wait_first=3.0)
        a = src.sample(0)
        time.sleep(0.3)
        b = src.sample(0)
        chk(a != [0.0] * 6, "RemoteStateSource 收到实值", str([round(v, 1) for v in a]))
        chk(a != b, "两次采样不同（在动）")
        chk(src.describe(0)[0] == "real", "describe 报 real", src.describe(0)[1])
        src.close()
    finally:
        if proc is not None:
            proc.terminate()

    print("\n全部通过" if OK else "\n有失败项")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
