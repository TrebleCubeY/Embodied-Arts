"""数字层后端入口。一条命令起两个 listener：

    D:/conda_envs/so101-twin/Scripts/python.exe twin/server/main.py

      ├─ 静态 HTTP :8000   server/static.py，与 tools/serve.py 共用同一份实现
      └─ 状态帧 WS :8001   websockets 库

为什么是两个端口：静态文件用标准库的 SimpleHTTPRequestHandler（它已经被自检跑过很多次），
状态帧用 websockets 库（标准库没有 WS）。两条路各用各的最稳，放一个进程里起两个 listener，
一条命令就够。纯前端开发不需要后端时，tools/serve.py 单跑也行。
"""

import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import broadcast  # noqa: E402
import config  # noqa: E402
import library  # noqa: E402
import pipeline  # noqa: E402
import protocol  # noqa: E402
import sources  # noqa: E402
import static  # noqa: E402


def parse_args():
    ap = argparse.ArgumentParser(description="数字层后端：动作库 → 30Hz 状态帧 → 浏览器")
    ap.add_argument("--http-port", type=int, default=config.HTTP_PORT)
    ap.add_argument("--ws-port", type=int, default=config.WS_PORT)
    ap.add_argument("--hz", type=float, default=config.BROADCAST_HZ)
    ap.add_argument("--no-record", action="store_true", help="不写 JSONL 留档")
    ap.add_argument("--skip-check", action="store_true", help="跳过动作库校验")
    ap.add_argument("--quiet", action="store_true", help="不每 5 秒打一行进度")
    return ap.parse_args()


def make_keyframe_recorder():
    """面板上「记录当前姿态」按下去之后，把六关节角追加到 motions/_draft.json。

    不直接改正式的三个动作文件：那是手编的、要保干净。攒够了一次性粘过去再调 move/hold。
    """
    draft = config.MOTIONS_DIR / "_draft.json"

    def record(msg):
        joints = msg.get("joints")
        if not isinstance(joints, list) or len(joints) != 6:
            print(f"  收到的关键帧不对，忽略：{joints!r}")
            return
        if draft.exists():
            data = json.loads(draft.read_text(encoding="utf-8"))
        else:
            data = {"note": "面板「记录当前姿态」攒下来的草稿，粘进正式动作文件时记得补 move / hold", "poses": []}
        data["poses"].append(
            {"move": 0.6, "hold": 0.1, "joints": [round(float(v), 2) for v in joints]}
        )
        draft.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  记下一个关键帧，草稿里现在共 {len(data['poses'])} 个")

    return record


async def loop_frames(src, calib, sched, clock, cast, rec, hz, quiet):
    period = 1.0 / hz
    t0 = time.perf_counter()
    seq = 0
    nxt = t0
    last_log = t0
    last_action = None

    while True:
        now = time.perf_counter()
        playhead = clock.tick(now)
        cast.maybe_auto_resume(now)

        raw = src.sample(playhead)
        actual = calib.apply(raw)
        target, mode, blend = sched.step(actual)
        frame_t = now - t0
        seq += 1

        frame = protocol.pack_frame(
            t=frame_t,
            seq=seq,
            actual=actual,
            target=target,
            mode=mode,
            blend=blend,
            grip=calib.to_grip(actual[5]),
        )
        await cast.broadcast(frame)
        await cast.flush_events()

        if seq % config.RECORD_EVERY == 0:
            rec.write(
                {
                    "t": round(frame_t, 4),
                    "seq": seq,
                    "actual": [round(v, 4) for v in actual],
                    "target": [round(v, 4) for v in target],
                    "mode": mode,
                    "blend": blend,
                    "grip": round(calib.to_grip(actual[5]), 2),
                }
            )

        name, phase = src.describe(playhead)
        if name != last_action:
            last_action = name
            if name:
                act = src.lib.actions[name]
                title = act.title
                ev = {
                    "type": "action",
                    "name": name,
                    "title": title,
                    # 带上这个动作的真实关键帧。屏幕右半的代码面板直接显示这份数据 ——
                    # 面板要是自己编一份，代码层就不再是观众对齐现实的锚点。
                    # 只往事件里加字段，68 字节状态帧一个字节不动。
                    "path": f"twin/server/motions/{act.path.name}",
                    "total": round(act.total, 3),
                    "poses": act.poses,
                }
            else:
                title = "停顿"
                ev = {"type": "action", "name": None, "title": title}
            cast.queue_event(ev)
            if not quiet:
                print(f"  [{frame_t:7.2f}s] 轮到 {title}（{name or '—'}）")

        if not quiet and now - last_log >= 5.0:
            print(
                f"  [{frame_t:7.2f}s] {name or '停顿'} · {phase} · "
                f"{len(cast.clients)} 个客户端 · {'已暂停' if clock.paused else '播放中'}"
            )
            rec.flush()
            last_log = now

        nxt += period
        delay = nxt - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            nxt = time.perf_counter()  # 落后了就重新对齐，不追帧


def main() -> int:
    args = parse_args()

    if not args.skip_check:
        problems = library.check()
        if problems:
            print("动作库有问题，先修好再起：")
            for p in problems:
                print(f"  [!] {p}")
            return 1

    lib = library.load()
    src = sources.MotionLibrarySource(lib)
    calib = pipeline.Calibrator()
    sched = pipeline.ModeScheduler()
    clock = pipeline.PlayClock()
    rec = broadcast.Recorder(enabled=not args.no_record)
    cast = broadcast.Broadcaster(clock, rec, port=args.ws_port)
    cast.on_keyframe = make_keyframe_recorder()

    httpd = static.make_server(config.HOST, args.http_port, config.WEB_DIR)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print(f"数字层后端  启动标识 {cast.boot_id}")
    print(f"  页面      http://{config.HOST}:{args.http_port}/")
    print(f"  状态帧    ws://{config.HOST}:{args.ws_port}/state   {args.hz:g}Hz / {protocol.FRAME_SIZE} 字节")
    print(f"  数据源    {src.kind}（动作库当实机侧的替身）")
    print(f"  播放列表  {' → '.join(config.PLAYLIST)}，动作之间停 {config.ACTION_GAP:g}s")
    print(f"  动作循环  一圈 {lib.cycle:.2f}s")
    print(f"  留档      {rec.path if rec.path else '未开'}")
    print("  校验      动作库通过；Ctrl+C 结束\n")

    async def run():
        await cast.start()
        try:
            await loop_frames(src, calib, sched, clock, cast, rec, args.hz, args.quiet)
        finally:
            rec.close()
            await cast.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
