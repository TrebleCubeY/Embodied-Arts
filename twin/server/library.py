"""动作库：读手编的关键帧 JSON，按时间采样出六关节角（度）。

格式：

    {
      "name": "wave",
      "title": "挥手",
      "note": "手编关键帧，不是真机示范数据",
      "poses": [
        {"move": 0.9, "hold": 0.1, "ease": "smooth", "joints": [0, 0, 0, 0, 0, 0]},
        ...
      ]
    }

joints 是 6 个数，顺序同 config.JOINT_ORDER，**单位度**。

时间轴：依次经过 poses。进第 i 个姿态用它的 move 秒（从第 i-1 个姿态插值过去；
第 0 个从最后一个姿态插值过去，所以整个动作是闭合循环的），到位后停 hold 秒。
ease 默认 smooth（smoothstep `t*t*(3-2t)`，两端速度为 0），可写 linear。

校验：
    $PY twin/server/library.py --check
"""

import json
from pathlib import Path

import config


def ease(kind: str, u: float) -> float:
    if kind == "linear":
        return u
    return u * u * (3.0 - 2.0 * u)


def lerp(a, b, u):
    return [x + (y - x) * u for x, y in zip(a, b)]


class Action:
    def __init__(self, data: dict, path: Path):
        self.path = path
        self.name = data["name"]
        self.title = data.get("title", self.name)
        self.note = data.get("note", "")
        self.poses = data["poses"]
        self.total = sum(p["move"] + p["hold"] for p in self.poses)

    def sample(self, tau: float) -> list:
        """tau 在 [0, total) 之内的时间，返回该时刻的六关节角（度）。"""
        tau %= self.total
        acc = 0.0
        n = len(self.poses)
        for i, pose in enumerate(self.poses):
            move = pose["move"]
            hold = pose["hold"]
            if tau < acc + move:
                prev = self.poses[i - 1]["joints"]
                u = (tau - acc) / move if move > 0 else 1.0
                return lerp(prev, pose["joints"], ease(pose.get("ease", "smooth"), u))
            acc += move
            if tau < acc + hold:
                return list(pose["joints"])
            acc += hold
        return list(self.poses[n - 1]["joints"])

    def phase(self, tau: float) -> str:
        """给日志看的一句话：现在在第几个姿态附近。"""
        tau %= self.total
        acc = 0.0
        for i, pose in enumerate(self.poses):
            acc += pose["move"]
            if tau < acc:
                return f"移到第 {i + 1} 个姿态"
            acc += pose["hold"]
            if tau < acc:
                return f"停在第 {i + 1} 个姿态"
        return "收尾"


class MotionLibrary:
    """整条播放列表。三个动作首尾都是同一个中性姿态，动作之间靠 ACTION_GAP 的停顿连接，
    所以不需要额外的过渡插值。"""

    def __init__(self, actions: dict, playlist: list, gap: float, neutral: list):
        self.actions = actions
        self.neutral = list(neutral)
        self.slots = []
        t = 0.0
        for name in playlist:
            act = actions[name]
            self.slots.append((t, t + act.total, name))
            t += act.total + gap
        self.cycle = t

    def sample(self, t: float) -> list:
        tt = t % self.cycle
        for start, end, name in self.slots:
            if tt < start:
                return list(self.neutral)  # 落在动作之间的停顿里
            if tt < end:
                return self.actions[name].sample(tt - start)
        return list(self.neutral)

    def current(self, t: float):
        """返回 (动作名 或 None, 阶段描述)。只用于日志和事件。"""
        tt = t % self.cycle
        for start, end, name in self.slots:
            if start <= tt < end:
                act = self.actions[name]
                return name, act.phase(tt - start)
        return None, "停顿"


def load(motions_dir: Path = None, playlist=None, gap=None, neutral=None) -> MotionLibrary:
    motions_dir = motions_dir or config.MOTIONS_DIR
    playlist = playlist if playlist is not None else config.PLAYLIST
    gap = config.ACTION_GAP if gap is None else gap
    neutral = config.NEUTRAL_DEG if neutral is None else neutral

    actions = {}
    for path in sorted(motions_dir.glob("*.json")):
        if path.name.startswith("_"):  # _draft.json 这类草稿不算动作
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        act = Action(data, path)
        actions[act.name] = act

    missing = [n for n in playlist if n not in actions]
    if missing:
        raise ValueError(f"播放列表里的动作没找到：{missing}；{motions_dir} 里只有 {sorted(actions)}")
    return MotionLibrary(actions, playlist, gap, neutral)


# ------------------------------------------------------------------ 校验

def check(motions_dir: Path = None, neutral=None, margin: float = 5.0) -> list:
    """校验动作 JSON。留 margin 度的余量，别顶着限位写。"""
    motions_dir = motions_dir or config.MOTIONS_DIR
    neutral = config.NEUTRAL_DEG if neutral is None else neutral
    limits = list(config.JOINT_LIMITS_DEG.values())
    problems = []
    seen = set()

    for path in sorted(motions_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            problems.append(f"{path.name}：JSON 读不了（{e}）")
            continue

        tag = path.name
        for key in ("name", "poses"):
            if key not in data:
                problems.append(f"{tag}：缺 {key}")
        if "poses" not in data:
            continue
        if data["name"] in seen:
            problems.append(f"{tag}：动作名 {data['name']} 重复")
        seen.add(data["name"])

        poses = data["poses"]
        if not poses:
            problems.append(f"{tag}：poses 是空的")
            continue

        total = 0.0
        for i, pose in enumerate(poses):
            where = f"{tag} 第 {i + 1} 个姿态"
            joints = pose.get("joints")
            if not isinstance(joints, list) or len(joints) != 6:
                problems.append(f"{where}：joints 必须是 6 个数")
                continue
            for name, value, (lo, hi) in zip(config.JOINT_ORDER, joints, limits):
                if not isinstance(value, (int, float)):
                    problems.append(f"{where}：{name} 不是数字（{value!r}）")
                elif not (lo + margin <= value <= hi - margin):
                    problems.append(
                        f"{where}：{name} = {value} 超出留了 {margin}° 余量的范围 "
                        f"[{lo + margin}, {hi - margin}]"
                    )
            move, hold = pose.get("move"), pose.get("hold")
            if not isinstance(move, (int, float)) or move <= 0:
                problems.append(f"{where}：move 必须是正数（{move!r}）")
            if not isinstance(hold, (int, float)) or hold < 0:
                problems.append(f"{where}：hold 不能是负数（{hold!r}）")
            if isinstance(move, (int, float)) and isinstance(hold, (int, float)):
                total += move + hold
            if pose.get("ease", "smooth") not in ("smooth", "linear"):
                problems.append(f"{where}：ease 只认 smooth / linear")

        if not (1.5 <= total <= 25.0):
            problems.append(f"{tag}：总时长 {total:.2f}s 不在 1.5–25s 之间")

        first, last = poses[0]["joints"], poses[-1]["joints"]
        if any(abs(a - b) > 0.01 for a, b in zip(first, last)):
            problems.append(f"{tag}：首尾姿态不一致，循环接不上")
        if any(abs(a - b) > 0.01 for a, b in zip(first, neutral)):
            problems.append(f"{tag}：首个姿态不是中性姿态 {neutral}")

    # 跨动作：全部都得从同一个中性姿态出发，动作之间的停顿才自然
    for name in config.PLAYLIST:
        if name not in seen:
            problems.append(f"播放列表里的 {name} 没有对应文件")

    return problems


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="校验动作库")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if not args.check:
        ap.print_help()
        return 0

    print(f"动作库目录 {config.MOTIONS_DIR}\n")
    print(f"{'文件':<24} {'动作':<16} {'姿态':>4} {'总时长':>8}")
    for path in sorted(config.MOTIONS_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            act = Action(json.loads(path.read_text(encoding="utf-8")), path)
        except Exception as e:  # noqa: BLE001
            print(f"{path.name:<24} 读不了：{e}")
            continue
        print(f"{path.name:<24} {act.name:<16} {len(act.poses):>4} {act.total:>7.2f}s")

    problems = check()
    print("\n校验")
    if problems:
        for p in problems:
            print(f"  [!] {p}")
        print(f"\n{len(problems)} 个问题")
        return 1
    print("  长度、限位、时长、首尾闭合都过关")
    print("\n动作库没问题")
    return 0


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(_main())
