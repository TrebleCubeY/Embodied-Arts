// 手动控制：6 个关节滑块 + 暂停 / 回到跟随 / 记录姿态 / 回零位。
//
// 布局上它是代码层最下面那一格，默认收起来 —— 它是给操作者调动作、也给自己上手摆用的，
// 观众看的那一屏不需要它。按钮文案不要改：自检脚本是按文案找按钮的
// （selftest.mjs 的 clickButton）。
//
// 与状态帧的分工见 view-state.js：面板管「谁写关节值」，暂停管「后端播不播」，两件正交的事。

import { JOINT_HINT, RAD2DEG } from './frame.js';
import { FOLLOW, VIEW_STATE_LABEL } from './view-state.js';
import { readJointAngle } from './robot.js';

// 面板是给人看的，不必每帧刷。60Hz 写 DOM 纯属浪费。
const SYNC_INTERVAL_MS = 120;

const LINK_LABEL = {
  idle: '未连后端',
  connecting: '连接中',
  open: '已连上',
  reconnecting: '重连中',
  stopped: '已停止',
};

// 抽屉头那一行要短，长标签（"断流 · 冻结最后一帧"）会把这一行撑爆
const SHORT_STATE = {
  FOLLOW: '跟随',
  OVERRIDE: '已接管',
  FROZEN: '断流',
};

function mkButton(text, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = text;
  b.addEventListener('click', onClick);
  return b;
}

export function createJointPanel(container, joints, hooks = {}) {
  container.replaceChildren();

  // ── 抽屉头
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'drawer-head';
  const title = document.createElement('span');
  title.textContent = '手动控制';
  const headState = document.createElement('em');
  headState.className = 'state';
  headState.style.fontStyle = 'normal';
  headState.style.letterSpacing = '0';
  const arrow = document.createElement('span');
  arrow.className = 'arrow';
  arrow.textContent = '▾';
  head.append(title, headState, arrow);

  const body = document.createElement('div');
  body.className = 'drawer-body';

  container.dataset.open = '0';
  head.setAttribute('aria-expanded', 'false');
  head.addEventListener('click', () => {
    const open = container.dataset.open === '1' ? '0' : '1';
    container.dataset.open = open;
    head.setAttribute('aria-expanded', open === '1' ? 'true' : 'false');
  });
  container.append(head, body);

  // ── 六个关节
  const widgets = [];

  for (const { name, joint, lower, upper } of joints) {
    const row = document.createElement('div');
    row.className = 'row';

    const lab = document.createElement('div');
    lab.className = 'lab';
    const nm = document.createElement('div');
    nm.className = 'name';
    nm.textContent = JOINT_HINT[name] ?? name;
    nm.title = name;
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = name;
    lab.append(nm, hint);

    const input = document.createElement('input');
    input.type = 'range';
    input.min = String(lower);
    input.max = String(upper);
    input.step = String((upper - lower) / 600);
    input.value = String(readJointAngle(joint));
    input.setAttribute('aria-label', name);

    const val = document.createElement('div');
    val.className = 'val';

    const show = (rad) => {
      val.textContent = `${(rad * RAD2DEG).toFixed(1)}°`;
      input.value = String(rad);
    };

    input.addEventListener('input', () => {
      // 键盘方向键只发 input 不发 pointer 事件，所以接管挂在这里，键盘操作也覆盖得到
      const a = Math.min(upper, Math.max(lower, parseFloat(input.value)));
      joint.setJointValue(a);
      show(a);
      hooks.onManual?.(name, a);
    });
    show(readJointAngle(joint));

    const w = { name, joint, show, active: false };
    // 正被指针按住的那一行不跟着状态帧刷，否则会把正在拖的滑块打回去
    input.addEventListener('pointerdown', () => {
      w.active = true;
    });
    window.addEventListener('pointerup', () => {
      w.active = false;
    });
    window.addEventListener('pointercancel', () => {
      w.active = false;
    });

    widgets.push(w);
    row.append(lab, input, val);
    body.append(row);
  }

  // ── 状态与按钮
  const status = document.createElement('div');
  status.className = 'status';
  body.append(status);

  const buttons = document.createElement('div');
  buttons.className = 'buttons';
  // 一个按钮toggle：按后端回传的状态决定这次该发 pause 还是 resume。
  // 前端不做本地乐观勾选，只反映后端说的。
  const pauseBtn = mkButton('暂停', () => {
    if (pausedShown) hooks.onResume?.();
    else hooks.onPause?.();
  });
  const followBtn = mkButton('回到跟随', () => hooks.onFollow?.());
  const keyBtn = mkButton('记录姿态', () => hooks.onKeyframe?.());
  const zeroBtn = mkButton('回零位', () => {
    for (const w of widgets) {
      w.joint.setJointValue(0);
      w.show(0);
    }
    hooks.onManual?.('__all__', 0);
  });
  buttons.append(pauseBtn, followBtn, keyBtn, zeroBtn);
  body.append(buttons);

  const note = document.createElement('div');
  note.className = 'note';
  note.textContent = '拖滑块即接管，粘性，按「回到跟随」退出 · 单位 度';
  body.append(note);

  let lastSync = 0;
  let pausedShown = null;

  function syncAll() {
    for (const w of widgets) w.show(readJointAngle(w.joint));
  }

  function sync(s) {
    const now = performance.now();
    if (now - lastSync < SYNC_INTERVAL_MS) return;
    lastSync = now;

    // 刷新滑块显示，跳过正被按住的那一行。
    // 不只是跟随态要刷：外部（自检脚本、以后的状态帧重放）改了关节值，
    // 面板显示的也得是真实角度，否则和画面里看到的不一致。
    if (s.appliedRad) {
      for (let i = 0; i < widgets.length; i += 1) {
        if (!widgets[i].active) widgets[i].show(s.appliedRad[i]);
      }
    }

    const parts = [LINK_LABEL[s.linkState] ?? s.linkState];
    if (s.linkState === 'open' && s.hz) parts.push(`${s.hz.toFixed(0)} Hz`);
    if (s.paused) parts.push('后端已暂停');
    const text = parts.join(' · ');
    if (status.textContent !== text) status.textContent = text;

    // 抽屉收起来时也要知道谁在写关节值，所以头这一行常显
    const stateText = SHORT_STATE[s.viewState] ?? VIEW_STATE_LABEL[s.viewState] ?? '';
    if (headState.textContent !== stateText) headState.textContent = stateText;

    const p = !!s.paused;
    if (p !== pausedShown) {
      pausedShown = p;
      pauseBtn.textContent = p ? '继续' : '暂停';
    }
    pauseBtn.disabled = s.linkState !== 'open';
    followBtn.disabled = s.viewState === FOLLOW;
    keyBtn.disabled = s.linkState !== 'open';
  }

  return { widgets, sync, syncAll };
}
