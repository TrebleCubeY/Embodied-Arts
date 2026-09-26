// 屏幕右半：代码层。
//
// 四段，从上到下：模式 / 关节角 / 代码轮转 / 事件。
//
// 这一块是观众用来对齐现实的锚点，所以里面每个数都来自真实状态，
// 没有装饰性的假数据。唯一允许的"设计"是措辞与排版。
//
// 高亮规则（代码面板）只依赖真实信号：暂停、模式、目标与实际的差、视图状态。
// 规则写在 litKeyFor() 里，一眼能看完，加状态时一起改。

import { JOINT_HINT, MODE_NAMES } from './frame.js';
import { OVERRIDE, FROZEN, VIEW_STATE_LABEL } from './view-state.js';
import { showPanel } from './stage.js';
import { PLATES, motionLines } from './code-plates.js';

// 面板是给人读的，不必每帧刷 DOM。60Hz 写文本纯属浪费，也看不清。
const SYNC_INTERVAL_MS = 100;
// 代码块自己轮转的间隔。真要盯着看，点一下标题就钉住。
const ROTATE_MS = 14000;
const TRACE_MAX = 80;
// 目标与实际差多少算"在动"。0.5° 是渲染插值肉眼可辨的量级。
const MOVING_DEG = 0.5;
// 目标角和某个关键帧的角差在这个容差内就算"停在那一帧上"。
// 后端在 hold 段返回的就是关键帧原值（float32 取整），所以这个比较是精确的，
// 不是靠"看它有没有停住"去猜 —— 猜法在 0.1s 的 hold 上会漏掉整帧。
const POSE_TOL_DEG = 0.05;

const CAPTION = {
  clock: '播放时钟已冻结，后端不再推进姿态',
  sample: '动作库按播放时刻取出六关节角',
  sampleGap: '动作之间的停顿，输出中性姿态',
  schedule: '模式调度器正在改写数字层的目标角',
  broadcast: '每秒定频发出 30 帧状态帧',
  decode: '解码 68 字节状态帧',
  view: '断流，冻结在最后一帧',
  follow: '按指数插值追向目标角',
  manual: '面板写关节角，渲染循环不介入',
  noPoses: '后端没有报关键帧数据',
};

const MODE_SENTENCE = {
  0: '数字层显示实机的关节角。',
  1: '数字层把实机的关节角做镜像映射。',
  2: '数字层在播与实机无关的运动。',
};

const LINK_LABEL = {
  idle: '未连后端',
  connecting: '连接中',
  open: '已连',
  reconnecting: '重连中',
  stopped: '已停止',
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/** 关节值 → 行程上的位置（0 到 1，按 URDF 限位） */
function norm(value, lower, upper) {
  if (!(upper > lower)) return 0.5;
  return Math.min(1, Math.max(0, (value - lower) / (upper - lower)));
}

export function createConsole(root, { joints }) {
  root.replaceChildren();

  // ────────────────────────────────────────────── 模式
  const modeBlock = el('section', 'block');
  modeBlock.append(
    (() => {
      const h = el('h2', 'block-head');
      h.append(el('span', 'lead', '模式'));
      return h;
    })(),
  );
  const modeRow = el('div', 'mode-row');
  const modeWord = el('b', 'mode-word', '—');
  const modeChip = el('span', 'chip', '偏离');
  modeChip.hidden = true;
  modeRow.append(modeWord, modeChip);
  const blendWrap = el('div', 'meter');
  const blendBar = el('i');
  blendWrap.append(blendBar);
  const modeNote = el('p', 'note');
  modeBlock.append(modeRow, blendWrap, modeNote);

  // ────────────────────────────────────────────── 关节角
  const jointBlock = el('section', 'block');
  {
    const h = el('h2', 'block-head');
    h.append(el('span', 'lead', '关节角'), el('em', null, '度'));
    jointBlock.append(h);
  }
  const head = el('div', 'jrow jhead');
  head.append(el('span', 'jname', '关节'), el('span', 'jgauge', '行程'));
  head.append(el('span', 'jnum', '目标'), el('span', 'jnum', '实际'));
  jointBlock.append(head);

  const rows = joints.map(({ name, lower, upper }) => {
    const row = el('div', 'jrow');
    const nameEl = el('span', 'jname', JOINT_HINT[name] ?? name);
    nameEl.title = name;
    const gauge = el('span', 'jgauge');
    const zero = el('i', 'jzero');
    const tgt = el('b', 'jtgt');
    const act = el('u', 'jact');
    gauge.append(zero, act, tgt);
    const tgtNum = el('span', 'jnum', '0.0');
    const actNum = el('span', 'jnum', '0.0');
    row.append(nameEl, gauge, tgtNum, actNum);
    jointBlock.append(row);
    const r = {
      name,
      lower,
      upper,
      tgt,
      act,
      tgtNum,
      actNum,
      last: ['', ''],
      zeroAt: ((0 - lower) / (upper - lower)) * 100,
    };
    zero.style.left = `${r.zeroAt}%`;
    return r;
  });

  // ────────────────────────────────────────────── 代码轮转
  const codeBlock = el('section', 'block sec-code');
  const codeTabs = el('div', 'tabs');
  const codeSrc = el('em', 'src');
  {
    const h = el('h2', 'block-head');
    h.append(el('span', 'lead', '代码'), codeSrc);
    codeBlock.append(h, codeTabs);
  }
  const codeList = el('ol', 'code');
  const codeNote = el('p', 'note lit-note');
  codeBlock.append(codeList, codeNote);

  let pinned = false;
  let plateIndex = 0;
  let plateTimer = null;
  const tabEls = PLATES.map((p, i) => {
    const b = el('button', 'tab', p.title);
    b.type = 'button';
    b.addEventListener('click', () => {
      pinned = true;
      plateIndex = i;
      renderPlate();
    });
    codeTabs.append(b);
    return b;
  });

  // ────────────────────────────────────────────── 事件
  const traceBlock = el('section', 'block grow');
  {
    const h = el('h2', 'block-head');
    h.append(el('span', 'lead', '事件'));
    traceBlock.append(h);
  }
  const traceList = el('ol', 'trace');
  traceBlock.append(traceList);

  root.append(modeBlock, jointBlock, codeBlock, traceBlock);

  // 手动控制放最后：它是给操作者用的，观众不需要，默认收起来。
  // ?panel=0 时整块不建 —— 自检脚本靠 #panel button 找按钮，所以这个 id 不能改。
  let panelEl = null;
  if (showPanel) {
    panelEl = el('section', 'block panel');
    panelEl.id = 'panel';
    root.append(panelEl);
  }

  // ────────────────────────────────────────────── 状态

  const t0 = performance.now();
  const state = {
    action: null,
    actionTitle: null,
    poses: null,
    motionSource: PLATES.find((p) => p.dynamic)?.source ?? '',
    litIndex: -1,
    // 臂当前在第几个关键帧上。用后端给的真实关键帧逐个比目标角得出，
    // 只允许往前走，所以那个"首尾同一个姿态"的闭合约定不会把它拽回第 1 帧。
    arrived: -1,
    matched: false,
  };

  function litKeyFor(s) {
    if (plateIndex === 0) {
      if (s.paused) return 'clock';
      if (s.mode !== 0) return 'schedule';
      if (!state.action) return 'sampleGap';
      return 'broadcast';
    }
    if (plateIndex === 2) {
      if (s.viewState === FROZEN) return 'view';
      if (s.viewState === OVERRIDE) return 'manual';
      if (s.lag > MOVING_DEG) return 'follow';
      return 'decode';
    }
    return null; // 动作块按关键帧下标高亮，见 renderMotion
  }

  function renderPlate() {
    const plate = PLATES[plateIndex];
    tabEls.forEach((b, i) => b.classList.toggle('on', i === plateIndex));
    codeSrc.textContent = plate.dynamic ? state.motionSource : plate.source;

    let lines = plate.lines;
    if (plate.dynamic) lines = motionLines(state.poses);
    if (!lines.length) {
      const li = el('li', 'ln empty');
      li.append(el('code', null, CAPTION.noPoses));
      codeList.replaceChildren(li);
      codeNote.textContent = '';
      state.litIndex = -1;
      return;
    }
    codeList.replaceChildren(
      ...lines.map((ln, i) => {
        const li = el('li', 'ln');
        li.append(el('span', 'lno', String(i + 1).padStart(2, '0')));
        li.append(el('code', null, ln.code));
        return li;
      }),
    );
    state.litIndex = -1;
  }

  /** 只改 class，不重建节点 —— 高亮每次同步都可能变 */
  function setLit(index) {
    if (index === state.litIndex) return;
    const kids = codeList.children;
    if (state.litIndex >= 0 && kids[state.litIndex]) kids[state.litIndex].classList.remove('lit');
    state.litIndex = index;
    if (index >= 0 && kids[index]) kids[index].classList.add('lit');
  }

  function syncCode(s) {
    if (plateIndex !== 1) {
      const key = litKeyFor(s);
      const lines = PLATES[plateIndex].lines;
      const i = lines.findIndex((l) => l.key === key);
      setLit(i);
      codeNote.textContent = CAPTION[key] ?? '';
      return;
    }

    // 动作块：高亮臂当前停在 / 正在靠近的那一条关键帧
    if (!state.poses?.length) {
      setLit(-1);
      codeNote.textContent = CAPTION.noPoses;
      return;
    }
    const last = state.poses.length - 1;
    const here = Math.max(state.arrived, 0);
    setLit(Math.min(state.matched ? here : here + 1, last));
    codeNote.textContent = state.matched
      ? `停在第 ${here + 1} 个关键帧`
      : `正在移到第 ${Math.min(here + 2, last + 1)} 个关键帧`;
  }

  /** 目标角是不是正落在某个关键帧上 */
  function poseAt(pose, targetDeg) {
    const j = pose?.joints;
    if (!Array.isArray(j)) return false;
    for (let i = 0; i < 6; i += 1) {
      if (Math.abs((j[i] ?? 0) - (targetDeg[i] ?? 0)) > POSE_TOL_DEG) return false;
    }
    return true;
  }

  /**
   * 推进关键帧指针。只往后走一格以内 —— 首尾是同一个姿态（动作库的闭合约定），
   * 不加这个限制的话刚起步就会被认成最后一帧。
   */
  function trackKeyframes(targetDeg) {
    if (!state.poses?.length) {
      state.arrived = -1;
      state.matched = false;
      return;
    }
    const lim = Math.min(state.arrived + 1, state.poses.length - 1);
    for (let i = 0; i <= lim; i += 1) {
      if (poseAt(state.poses[i], targetDeg)) state.arrived = Math.max(state.arrived, i);
    }
    state.matched = state.arrived >= 0 && poseAt(state.poses[state.arrived], targetDeg);
  }

  let lastSync = 0;

  return {
    /** 每帧调用，内部按 SYNC_INTERVAL_MS 自己节流 */
    sync(s) {
      // 关键帧指针不受节流影响：hold 段只有 0.1s，100ms 才看一眼会整个漏掉。
      // 它只改两个数字，不碰 DOM。
      trackKeyframes(s.targetDeg ?? [0, 0, 0, 0, 0, 0]);

      const now = performance.now();
      if (now - lastSync < SYNC_INTERVAL_MS) return;
      lastSync = now;

      // —— 模式
      const word = MODE_NAMES[s.mode] ?? String(s.mode);
      if (modeWord.textContent !== word) modeWord.textContent = word;
      const off = s.mode !== 0;
      modeChip.hidden = !off;
      modeWord.classList.toggle('off', off);
      const pct = `${Math.min(100, Math.max(0, (s.blend / 255) * 100))}%`;
      blendBar.style.width = pct;
      blendWrap.classList.toggle('run', s.blend < 255);
      blendWrap.classList.toggle('off', off);
      modeNote.textContent = MODE_SENTENCE[s.mode] ?? '';

      // —— 关节角
      for (let i = 0; i < rows.length; i += 1) {
        const r = rows[i];
        const t = s.targetDeg?.[i] ?? 0;
        const a = s.appliedDeg?.[i] ?? 0;
        const tn = t.toFixed(1);
        const an = a.toFixed(1);
        if (r.last[0] !== tn) {
          r.tgtNum.textContent = tn;
          r.last[0] = tn;
        }
        if (r.last[1] !== an) {
          r.actNum.textContent = an;
          r.last[1] = an;
        }
        r.tgt.style.left = `${norm(t, r.lower, r.upper) * 100}%`;
        const an0 = norm(0, r.lower, r.upper) * 100;
        const an1 = norm(a, r.lower, r.upper) * 100;
        r.act.style.left = `${Math.min(an0, an1)}%`;
        r.act.style.width = `${Math.abs(an1 - an0)}%`;
      }

      // —— 代码
      syncCode(s);
    },

    /** 动作切换。后端会带上这个动作的真实关键帧，没有就用空表兜底。 */
    setAction(msg) {
      state.action = msg.name ?? null;
      state.actionTitle = msg.title ?? null;
      state.poses = Array.isArray(msg.poses) ? msg.poses : null;
      if (msg.path) state.motionSource = msg.path;
      state.arrived = -1;
      state.matched = false;
      if (plateIndex === 1) renderPlate();
    },

    /** 往事件流里写一行。kind 只影响左边那道细线的颜色。 */
    push(kind, text) {
      const stamp = ((performance.now() - t0) / 1000).toFixed(1);
      const li = el('li', `tr ${kind}`);
      li.append(el('time', null, `+${stamp}`), el('span', null, text));
      traceList.append(li);
      while (traceList.children.length > TRACE_MAX) traceList.firstElementChild.remove();
      traceList.scrollTop = traceList.scrollHeight; // 新的在最下面，自动跟到底
    },

    panelEl,

    start() {
      // ?plate=0|1|2 直接钉住某一块，演示时不用等它自己轮
      const want = new URLSearchParams(location.search).get('plate');
      const i = Number(want);
      if (want !== null && Number.isInteger(i) && i >= 0 && i < PLATES.length) {
        pinned = true;
        plateIndex = i;
      }
      renderPlate();
      plateTimer = setInterval(() => {
        if (pinned) return;
        plateIndex = (plateIndex + 1) % PLATES.length;
        renderPlate();
      }, ROTATE_MS);
    },
  };
}

/** 页头右侧那一行状态：连没连上、多少赫兹、在播还是暂停、谁在写关节值。 */
export function createTopline(root) {
  const mk = (label, cls) => {
    const d = el('div', cls ? `tl ${cls}` : 'tl');
    const dot = el('i', 'dot');
    const val = el('b', null, '—');
    const unit = el('em', null, label);
    d.append(dot, val, unit);
    root.append(d);
    return { d, dot, val, unit };
  };

  const link = mk('状态帧', 'link');
  const hz = mk('Hz');
  const play = mk('');
  const write = mk('');

  const last = { link: '', hz: '', play: '', write: '' };

  return {
    sync(s) {
      const linkText = LINK_LABEL[s.linkState] ?? s.linkState ?? '—';
      if (linkText !== last.link) {
        last.link = linkText;
        link.val.textContent = linkText;
        link.dot.className = `dot ${s.linkState === 'open' ? 'on' : 'off'}`;
      }
      const hzText = s.linkState === 'open' && s.hz ? s.hz.toFixed(0) : '—';
      if (hzText !== last.hz) {
        last.hz = hzText;
        hz.val.textContent = hzText;
      }
      const playText = s.paused ? '已暂停' : '播放中';
      if (playText !== last.play) {
        last.play = playText;
        play.val.textContent = playText;
        play.d.classList.toggle('warn', !!s.paused);
      }
      const writeText = VIEW_STATE_LABEL[s.viewState] ?? s.viewState ?? '—';
      if (writeText !== last.write) {
        last.write = writeText;
        write.val.textContent = writeText;
        write.d.classList.toggle('warn', s.viewState !== 'FOLLOW');
      }
    },
  };
}
