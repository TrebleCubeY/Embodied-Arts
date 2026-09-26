// 代码面板的内容。
//
// 这里的每一行都对应 twin/ 里真实存在的变量或调用点，改名要同步这里 ——
// 代码层是观众用来对齐现实的锚点，编出来的代码会让整块屏失去意义。
// 简化只允许删减（比如把三行压成一行），不允许写不存在的调用。
//
// 每行带一个 key，高亮规则在 console.js。规则只用真实状态
// （暂停、模式、目标与实际之差、视图状态），不引入定时器或随机数。

export const PLATES = [
  {
    id: 'pipeline',
    title: '通路',
    source: 'twin/server/main.py',
    lines: [
      { key: 'clock', code: 'playhead = clock.tick(now)' },
      { key: 'sample', code: 'raw = src.sample(playhead)' },
      { key: 'schedule', code: 'target, mode, blend = sched.step(actual)' },
      { key: 'broadcast', code: 'await cast.broadcast(protocol.pack_frame(...))' },
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
      { key: 'decode', code: 'frame = decodeStateFrame(buf)' },
      { key: 'view', code: 'view = FOLLOW | OVERRIDE | FROZEN' },
      { key: 'interp', code: 'k = 1 - Math.exp(-INTERP_LAMBDA * dt)' },
      { key: 'follow', code: 'joint.setJointValue(cur + (target[i] - cur) * k)' },
      { key: 'manual', code: 'joint.setJointValue(slider.value)' },
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
      // 一行就是 motions/*.json 里的一条记录，字段名照抄，没做改写
      code: `move ${fmt(p.move)}  hold ${fmt(p.hold)}  [${joints.map(fmt).join(', ')}]`,
      index: i,
    };
  });
}

function fmt(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '?';
  return (Math.round(v * 10) / 10).toFixed(1);
}
