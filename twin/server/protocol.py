"""状态帧的打包与解析。与 web/src/frame.js 的 decodeStateFrame 是一对，改一边必须改另一边。

布局（小端，共 68 字节）：

    偏移   类型        字段
    0      float64     t        后端单调时钟，秒。永远往前走，暂停也不停
    8      uint32      seq      递增序号
    12     float32[6]  actual   实机（本期＝动作库）六关节角，度
    36     float32[6]  target   数字层六关节角，度
    60     uint8       mode     0 跟随 / 1 反向 / 2 无关运动
    61     uint8       blend    过渡进度 0-255
    62     2 字节      —        填充：让后面的 float32 落在 4 字节边界
    64     float32     grip     夹爪 0-100，仅供显示

两条务必别改坏的地方：

- **格式串用 `<`，不要用 `=` 或 `@`。** 后两者走本机对齐，会静默插 padding，
  换台机器就整体错位，而且不会报任何错。
- **那 2 字节填充是故意的。** DataView 读 float32 不要求对齐，所以对齐错了不会有症状，
  只会在某天想零拷贝 `new Float32Array(buf, 12, 6)` 时直接抛异常。

自己跑一遍看布局：
    $PY twin/server/protocol.py
"""

import json
import struct

FRAME_SIZE = 68
_FRAME = struct.Struct("<dI6f6fBB2xf")

assert _FRAME.size == FRAME_SIZE, f"状态帧布局变了：算出 {_FRAME.size}，应为 {FRAME_SIZE}"

# 前端上行的控制消息类型。别的一律忽略
CONTROL_TYPES = ("hello", "pause", "resume", "keyframe")


def pack_frame(*, t, seq, actual, target, mode, blend, grip) -> bytes:
    if len(actual) != 6 or len(target) != 6:
        raise ValueError("actual / target 必须是 6 个关节角")
    return _FRAME.pack(
        float(t),
        int(seq) & 0xFFFFFFFF,
        *[float(v) for v in actual],
        *[float(v) for v in target],
        int(mode) & 0xFF,
        int(blend) & 0xFF,
        float(grip),
    )


def unpack_frame(buf) -> dict:
    """把自己发出去的帧读回来。后端自检与留档回读都用它。"""
    if len(buf) != FRAME_SIZE:
        raise ValueError(f"帧长应为 {FRAME_SIZE}，实得 {len(buf)}")
    fields = _FRAME.unpack(buf)
    return {
        "t": fields[0],
        "seq": fields[1],
        "actual": list(fields[2:8]),
        "target": list(fields[8:14]),
        "mode": fields[14],
        "blend": fields[15],
        "grip": fields[16],
    }


def parse_control(text: str):
    """前端上行消息。只认 CONTROL_TYPES 里的类型，其余返回 None。"""
    if not isinstance(text, str):
        return None
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") not in CONTROL_TYPES:
        return None
    return msg


if __name__ == "__main__":
    sample = {
        "t": 12.5,
        "seq": 375,
        "actual": [1.0, -2.5, 3.25, 0.0, -88.0, -10.0],
        "target": [1.0, -2.5, 3.25, 0.0, -88.0, -10.0],
        "mode": 0,
        "blend": 255,
        "grip": 0.0,
    }
    buf = pack_frame(**sample)
    back = unpack_frame(buf)

    print(f"帧长 {len(buf)} 字节（应为 {FRAME_SIZE}）")
    print(f"格式串 {_FRAME.format!r}\n")
    print(f"{'偏移':>5} {'字节':>5}  字段")
    for label, off, size in (
        ("t", 0, 8),
        ("seq", 8, 4),
        ("actual[0..5]", 12, 24),
        ("target[0..5]", 36, 24),
        ("mode", 60, 1),
        ("blend", 61, 1),
        ("(填充)", 62, 2),
        ("grip", 64, 4),
    ):
        print(f"{off:>5} {size:>5}  {label}")

    print("\n往返对拍：")
    bad = 0
    for key, want in sample.items():
        got = back[key]
        ok = got == want if not isinstance(want, float) else abs(got - want) < 1e-5
        if isinstance(want, list):
            ok = all(abs(a - b) < 1e-5 for a, b in zip(got, want))
        bad += 0 if ok else 1
        print(f"  {'OK ' if ok else '错 '} {key}: {want} -> {got}")
    print(f"\n{'布局没问题' if not bad else f'{bad} 个字段对不上'}")
    raise SystemExit(1 if bad else 0)
