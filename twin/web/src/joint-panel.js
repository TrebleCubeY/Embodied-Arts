// 手动控制面板：6 个关节滑块 + 暂停 / 回到跟随 / 记录姿态 / 回零位。
//
// 它不再是"临时件"—— 接管要长期保留（调试动作、观众自己摆都用得上）。
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

function mkButton(text, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = text;
  b.addEventListener('click', onClick);
  return b;
}

export function createJointPanel(container, joints, hooks = {}) {
  container.replaceChildren();

  const title = document.createElement('h2');
  title.textContent = '手动控制';
  container.append(title);

  const widgets = [];

  for (const { name, joint, lower, upper } of joints) {
    const row = document.createElement('div');
    row.className = 'row';

    const lab = document.createElement('div');
    lab.className = 'lab';
    const nm = document.createElement('div');
    nm.className = 'name';
    nm.textContent = name;
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = JOINT_HINT[name] ?? '';
    lab.append(nm, hint);

    const input = document.createElement('input');
    input.type = 'range';
    input.min = String(lower);
    input.max = String(upper);
    input.step = String((upper - lower) / 600);
    input.value = String(readJointAngle(joint));

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
    container.append(row);
  }

  const status = document.createElement('div');
  status.className = 'status';
  container.append(status);

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
  container.append(buttons);

  const note = document.createElement('div');
  note.className = 'note';
  note.textContent = '拖滑块即接管，粘性，按「回到跟随」退出 · 单位 度';
  container.append(note);

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
    parts.push(VIEW_STATE_LABEL[s.viewState] ?? s.viewState);
    if (s.paused) parts.push('后端已暂停');
    const text = parts.join(' · ');
    if (status.textContent !== text) status.textContent = text;

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
