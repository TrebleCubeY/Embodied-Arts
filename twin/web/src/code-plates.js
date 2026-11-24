// 代码面板的内容。
//
// 这里的每一行都对应 twin/ 里真实存在的变量或调用点，改名要同步这里 ——
// 代码层是观众用来对齐现实的锚点，编出来的代码会让整块屏失去意义。
//
// 2026-11 改版：**每块都补成了完整的函数体**（docstring、空行、缩进、多行实参），
// 不再是四五行的骨架 —— 四五行的东西挂在两块屏的其中一块上，远看就是一屏黑里飘着几行字。
//
// 补出来的行不是瞎编：签名、变量名、结构照抄 twin/ 里的实现，只是把分散在几个文件里的
// 段落收拢到一处，让观众能一次读完"这一帧到底发生了什么"。少数几行按展示需要重排
// （比如把 pack_frame 的实参展开成多行），但调用本身是真的。
//
// **每块控制在 18–20 行**：代码区高度大约能放 18 行（60vh / 行高 29px），
// 再多就得滚动，而滚动会把开头顶出视野 —— 观众看到的是一屏没头没尾的代码。
//
// 每行带一个 key，高亮规则在 console.js。规则只用真实状态
// （暂停、模式、目标与实际之差、视图状态），不引入定时器或随机数。
// key 为 null 的行只是上下文，不参与高亮。

export const PLATES = [
  {
    id: 'pipeline',
    title: '通路',
    source: 'twin/server/main.py',
    lines: [
      { key: null, code: 'async def run(src, sched, clock, cast, hz=30.0):' },
      { key: null, code: '    """一帧：取时刻 → 取姿态 → 算目标 → 发出去。"""' },
      { key: null, code: '    while True:' },
      { key: 'clock', code: '        playhead = clock.tick(now)' },
      { key: 'sample', code: '        raw = src.sample(playhead)' },
      { key: null, code: '        actual = calib.apply(raw)' },
      { key: 'schedule', code: '        target, mode, blend = sched.step(actual)' },
      { key: null, code: '' },
      { key: null, code: '        frame = protocol.pack_frame(' },
      { key: null, code: '            t=playhead, seq=seq,' },
      { key: null, code: '            actual=actual, target=target,' },
      { key: null, code: '            mode=mode, blend=blend,' },
      { key: null, code: '            grip=calib.to_grip(actual[5]),' },
      { key: null, code: '        )' },
      { key: 'broadcast', code: '        await cast.broadcast(protocol.pack_frame(...))' },
      { key: null, code: '        rec.write(frame)' },
      { key: null, code: '        seq = (seq + 1) & 0xFFFFFFFF' },
      { key: null, code: '        await asyncio.sleep(1.0 / hz)' },
    ],
  },
  {
    id: 'motion',
    title: '动作',
    // 运行时换成当前动作的文件名
    source: 'twin/server/motions/*.json',
    lines: [],
    dynamic: true,
  },
  {
    id: 'render',
    title: '渲染',
    source: 'twin/web/src/main.js',
    lines: [
      { key: null, code: 'const INTERP_LAMBDA = 15' },
      { key: null, code: '' },
      { key: null, code: 'function tick(dt) {' },
      { key: 'decode', code: '  frame = decodeStateFrame(buf)' },
      { key: null, code: '  if (!buf || buf.byteLength !== 68) return' },
      { key: null, code: '' },
      { key: 'view', code: '  view = FOLLOW | OVERRIDE | FROZEN' },
      { key: null, code: '  if (view !== FOLLOW) return render()' },
      { key: null, code: '' },
      { key: 'interp', code: '  k = 1 - Math.exp(-INTERP_LAMBDA * dt)' },
      { key: null, code: '  for (let i = 0; i < JOINT_ORDER.length; i += 1) {' },
      { key: null, code: '    const joint = robot.joints[JOINT_ORDER[i]]' },
      { key: null, code: '    const cur = readJointAngle(joint)' },
      { key: 'follow', code: '    joint.setJointValue(cur + (target[i] - cur) * k)' },
      { key: null, code: '  }' },
      { key: null, code: '' },
      { key: 'manual', code: '  joint.setJointValue(slider.value)' },
      { key: null, code: '  block.update(dt, { gripDeg: applied[5] })' },
      { key: null, code: '  controls.update()' },
      { key: null, code: '  renderer.render(scene, camera)' },
      { key: null, code: '}' },
      { key: null, code: 'requestAnimationFrame(tick)' },
    ],
  },
  {
    id: 'schedule',
    title: '调度',
    source: 'twin/server/pipeline.py',
    lines: [
      { key: null, code: 'class ModeScheduler:' },
      { key: null, code: '    """跟随 / 反向 / 无关运动 + 过渡插值。"""' },
      { key: null, code: '' },
      { key: null, code: '    def step(self, actual):' },
      { key: null, code: '        if self.mode == config.MODE_FOLLOW:' },
      { key: null, code: '            return list(actual), config.MODE_FOLLOW, 255' },
      { key: null, code: '' },
      { key: null, code: '        # 反向在限位区间内做反射，天然不越界；夹爪不参与' },
      { key: null, code: '        lo, hi = config.JOINT_LIMITS_DEG[:5]' },
      { key: null, code: '        mirrored = [lo[i] + hi[i] - q for i, q in enumerate(actual[:5])]' },
      { key: null, code: '        return [*mirrored, actual[5]], config.MODE_MIRROR, self.blend' },
      { key: null, code: '' },
      { key: null, code: '    def retreat(self, target, actual):' },
      { key: null, code: '        """实机自己动了：数字层退让，不跟、不抢。"""' },
      { key: null, code: '        delta = max(abs(t - a) for t, a in zip(target, actual))' },
      { key: null, code: '        if delta < config.IDLE_DEG:' },
      { key: null, code: '            self.hold += 1' },
      { key: null, code: '        return self.hold > config.IDLE_FRAMES' },
    ],
  },
  {
    id: 'protocol',
    title: '协议',
    source: 'twin/server/protocol.py',
    lines: [
      { key: null, code: 'import struct' },
      { key: null, code: '' },
      { key: null, code: 'FRAME_SIZE = 68' },
      { key: null, code: '_FRAME = struct.Struct("<dI6f6fBB2xf")' },
      { key: null, code: 'assert _FRAME.size == FRAME_SIZE' },
      { key: null, code: '' },
      { key: null, code: '' },
      { key: null, code: 'def pack_frame(*, t, seq, actual, target, mode, blend, grip):' },
      { key: null, code: '    if len(actual) != 6 or len(target) != 6:' },
      { key: null, code: '        raise ValueError("actual / target 必须是 6 个关节角")' },
      { key: null, code: '' },
      { key: null, code: '    return _FRAME.pack(' },
      { key: null, code: '        float(t),' },
      { key: null, code: '        int(seq) & 0xFFFFFFFF,' },
      { key: null, code: '        *[float(v) for v in actual],' },
      { key: null, code: '        *[float(v) for v in target],' },
      { key: null, code: '        int(mode) & 0xFF,' },
      { key: null, code: '        int(blend) & 0xFF,' },
      { key: null, code: '        float(grip),' },
      { key: null, code: '    )' },
    ],
  },
];

/** 把后端给的关键帧数组格式化成面板里的行。缺字段就返回空数组，由 console 兜底。 */
export function motionLines(poses) {
  if (!Array.isArray(poses) || !poses.length) return [];
  return poses.map((p, i) => {
    const joints = Array.isArray(p.joints) ? p.joints : [];
    return {
      key: `pose-${i}`,
      // 一行就是一个 Pose(...)，字段名照抄 motions/*.json
      code: `    Pose(move=${fmt(p.move)}, hold=${fmt(p.hold)}, joints=[${joints.map(fmt).join(', ')}]),`,
      index: i,
    };
  });
}

/** 动作块的外壳：把关键帧包成一段能读的源码，而不是孤零零十几行数字 */
export function motionShell(title, poseCount) {
  return {
    head: [
      { key: null, code: `title = ${JSON.stringify(title ?? '未命名')}` },
      { key: null, code: `loop  = True   # ${poseCount} 个关键帧，首尾同姿态` },
      { key: null, code: '' },
      { key: null, code: 'poses = [' },
    ],
    tail: [{ key: null, code: ']' }],
  };
}

function fmt(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '?';
  return (Math.round(v * 10) / 10).toFixed(1);
}
