import * as THREE from 'three';

import {
  JOINT_ORDER,
  JOINT_LIMITS_DEG,
  DEG2RAD,
  MODE_NAMES,
  DRIVE_MODES,
  DRIVE_LABELS,
  defaultStateUrl,
} from './frame.js';
import { RED_BLOCK, createStage, addGround, addRedBlock, showPanel, isSelftest } from './stage.js';
import { createBlockRig, jawOpeningMm } from './block.js';
import {
  loadRobot,
  listJoints,
  readJointAngle,
  tuneMaterials,
  groundModel,
  frameCamera,
} from './robot.js';
import { attachControls } from './controls.js';
import { createJointPanel } from './joint-panel.js';
import { createLink } from './net.js';
import { createViewState, FOLLOW, VIEW_STATE_LABEL } from './view-state.js';
import { createConsole, createTopline, createModeBar } from './console.js';
import { installDebug } from './debug.js';

const URDF_URL = './models/so101/so101_new_calib.urdf';

// 渲染插值系数（技术方案第 6.2 节）。λ 是这里唯一的旋钮：
// 小了顺滑但会抹掉微小残差，大了残差保留但大动作能看出阶梯。现场调。
const INTERP_LAMBDA = 15;

// 目标与实际差多少算"臂在动"。和 console.js 里那个阈值是一回事，两处都改。
const MOVING_DEG = 0.5;

// 视图：一块屏一个窗口。
//   both  两块屏并排（默认；单机兜底与自检用这个）
//   twin  只有数字层，铺满一屏
//   code  只有代码层，铺满一屏（**不建 3D**，省一个 WebGL 上下文）
const params = new URLSearchParams(location.search);
const VIEW = (params.get('view') || 'both').toLowerCase();
const VIEW_TWIN = VIEW === 'twin' || VIEW === 'both';
const VIEW_CODE = VIEW === 'code' || VIEW === 'both';
document.body.dataset.view = VIEW;

const viewport = document.getElementById('viewport');
const loadingEl = document.getElementById('loading');
const loadingMsg = loadingEl?.querySelector('.msg');
const consoleEl = document.getElementById('console');

// 两块屏：左边是数字层（模式读数在它的标题条上），右边是代码层
// （驱动档 / 关节角 / 代码轮转 / 事件，手动控制也在它里面，最后一格抽屉）。
const modeBar = VIEW_TWIN ? createModeBar(document.getElementById('modebar')) : null;
const topline = createTopline(document.getElementById('topline'));

// code 视图没有 URDF，也就没有 urdf-loader 解析出来的限位 —— 用 frame.js 里那份
// 与 config.py 对齐的度数限位拼一份（弧度，形状与 listJoints 一致）。
function jointsFromLimits() {
  return JOINT_ORDER.map((name) => ({
    name,
    joint: null,
    lower: JOINT_LIMITS_DEG[name][0] * DEG2RAD,
    upper: JOINT_LIMITS_DEG[name][1] * DEG2RAD,
  }));
}

// WebGL 起不来时不要白屏：把原因写下来，自检脚本据此区分「环境问题」和「代码写错」
function showError(msg) {
  if (loadingEl) {
    loadingEl.hidden = false;
    loadingEl.classList.add('err');
  }
  if (loadingMsg) loadingMsg.textContent = msg;
  window.__TWIN__ = { ready: false, error: msg };
  console.error('[twin]', msg);
}

let stage = null;
if (VIEW_TWIN) {
  try {
    stage = createStage(viewport);
  } catch (err) {
    showError(`WebGL 初始化失败：${err?.message ?? err}`);
  }
}

// ─────────────────────────────────────────────────────────── 共享状态

let robot = null;
let controls = null;
let panel = null;
// 左屏右下角那个「关节角」浮层菜单。它和代码层的抽屉是同一个组件、同一份关节值，
// 只是长在了像这一侧 —— 观众看着像，想知道六个角是多少，不用去看另一块屏。
let angleMenu = null;
let link = null;
let block = null;
let codeLayer = null;
const tmpVec = new THREE.Vector3();

// 目标姿态与最近一帧的数值（弧度 / 度各存一份，都是预分配好的数组）。
// 这两组数只在渲染循环里消费，不进任何响应式结构 —— 30Hz 的依赖更新会让页面抖。
const targetRad = new Array(6).fill(0);
const appliedRad = new Array(6).fill(0);
const status = {
  mode: 0,
  blend: 255,
  grip: 0,
  actualDeg: new Array(6).fill(0),
  targetDeg: new Array(6).fill(0),
  appliedDeg: new Array(6).fill(0),
  drive: null,
};

const view = createViewState({
  onChange(next, prev, why) {
    // 断流/重连导致的冻结与解冻不往事件里写：上面那条连接状态已经说过了，
    // 再写一遍会让人以为"断流"是独立发生的事。
    if (why !== 'link') codeLayer?.push('view', VIEW_STATE_LABEL[next] ?? next);
    console.log(`[twin] 视图状态 ${prev} → ${next}`);
  },
});

// ─────────────────────────────────────────────────────────── 控制

function sendControl(obj) {
  const ok = link?.send(obj) ?? false;
  if (!ok) codeLayer?.push('link', `没发出去（后端未连）：${JSON.stringify(obj)}`);
  return ok;
}

function sendDrive(mode) {
  if (!DRIVE_MODES.includes(mode)) return false;
  return sendControl({ type: 'drive', mode });
}

function togglePlay() {
  const paused = !!link?.stats.paused;
  sendControl(paused ? { type: 'resume' } : { type: 'pause' });
}

// 演示者站在机械臂旁边，用的是无线键盘：数字键够大、不撞浏览器快捷键。
// F1 是浏览器帮助、F5 是刷新，都不能用。
window.addEventListener('keydown', (ev) => {
  if (ev.repeat || ev.ctrlKey || ev.altKey || ev.metaKey) return;
  const tag = (ev.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return; // 手动抽屉里拖滑块时不抢键
  const idx = '123'.indexOf(ev.key);
  if (idx >= 0) {
    ev.preventDefault();
    sendDrive(DRIVE_MODES[idx]);
    return;
  }
  if (ev.key === '0') {
    ev.preventDefault();
    togglePlay();
  }
});

// ─────────────────────────────────────────────────────────── 代码层

function buildCodeLayer(joints) {
  codeLayer = createConsole(consoleEl, {
    joints,
    modeBar,
    onDrive: (mode) => sendDrive(mode),
  });
  codeLayer.start();
  codeLayer.push('link', '页面就绪');

  // 手动抽屉要写 3D 的 joint 对象，code 视图没有模型，抽屉建不了 —— 直接摘掉。
  // 演示时这块本来也收着（观众屏不该有写权限）。
  if (!joints.some((j) => j.joint)) {
    codeLayer.panelEl?.remove();
  } else if (showPanel && codeLayer.panelEl) {
    panel = createJointPanel(codeLayer.panelEl, joints, {
      onManual: () => {
        view.takeOver('panel');
        // 双屏时数字层在另一个窗口里，它得知道"关节值现在由人写"
        sendControl({ type: 'override', on: true });
      },
      onPause: () => sendControl({ type: 'pause' }),
      onResume: () => sendControl({ type: 'resume' }),
      onFollow: () => {
        view.release('panel');
        sendControl({ type: 'override', on: false });
      },
      // 回零位要回的是后端那份 NEUTRAL_DEG，前端不复制一份，免得两边飘
      neutralDeg: () => link?.stats?.neutralDeg,
      onKeyframe: () => {
        const deg = status.appliedDeg.map((v) => Number(v.toFixed(2)));
        if (!link?.send({ type: 'keyframe', joints: deg })) {
          console.warn('[twin] 后端没连上，这个姿态没记下来');
        }
      },
    });
  }
}

// ─────────────────────────────────────────────────────────── 左屏的角度菜单

function buildAngleMenu(joints) {
  const host = document.getElementById('angle-menu');
  if (!host) return;
  angleMenu = createJointPanel(host, joints, {
    title: '关节角',
    onManual: () => {
      view.takeOver('menu');
      // 双屏时另一块屏（代码层）也要知道关节值现在由人写
      sendControl({ type: 'override', on: true });
    },
    onPause: () => sendControl({ type: 'pause' }),
    onResume: () => sendControl({ type: 'resume' }),
    onFollow: () => {
      view.release('menu');
      sendControl({ type: 'override', on: false });
    },
    neutralDeg: () => link?.stats?.neutralDeg,
    onKeyframe: () => {
      const deg = status.appliedDeg.map((v) => Number(v.toFixed(2)));
      link?.send({ type: 'keyframe', joints: deg });
    },
  });
}

// ─────────────────────────────────────────────────────────── 连后端

function onLinkState(stats) {
  if (stats.state === 'open') view.thaw('link');
  else view.freeze('link');
  topline.sync({
    linkState: stats.state,
    hz: stats.hz,
    paused: stats.paused,
    viewState: view.state,
  });
}

function applyDrive(state) {
  if (!state) return;
  const prevMode = status.drive?.mode;
  status.drive = state;
  if (state.mode !== prevMode) {
    codeLayer?.push('drive', `驱动 → ${DRIVE_LABELS[state.mode] ?? state.mode}`);
  }
}

function syncPayload() {
  let lag = 0;
  for (let i = 0; i < 5; i += 1) {
    lag = Math.max(lag, Math.abs(status.targetDeg[i] - status.appliedDeg[i]));
  }
  return {
    viewState: view.state,
    paused: !!link?.stats.paused,
    mode: status.mode,
    blend: status.blend,
    grip: status.grip,
    lag,
    targetDeg: status.targetDeg,
    actualDeg: status.actualDeg,
    // code 视图没有插值，用真实观测角当"实际"
    appliedDeg: VIEW_TWIN ? status.appliedDeg : status.actualDeg,
    drive: status.drive,
  };
}

function startLink() {
  const stateUrl = defaultStateUrl();
  if (!stateUrl) {
    codeLayer?.push('link', '?ws=0：不连后端，只显示静态模型');
    console.log('[twin] ?ws=0：不连后端，只显示静态模型');
    return;
  }
  let lastLink = null;
  link = createLink({
    url: stateUrl,
    onFrame(frame) {
      for (let i = 0; i < 6; i += 1) {
        targetRad[i] = frame.target[i] * DEG2RAD;
        status.targetDeg[i] = frame.target[i];
        status.actualDeg[i] = frame.actual[i];
      }
      status.mode = frame.mode;
      status.blend = frame.blend;
      status.grip = frame.grip;
      // code 视图没有渲染循环，靠帧驱动刷新；内部有 100ms 节流，30Hz 调不浪费
      if (!VIEW_TWIN) codeLayer?.sync(syncPayload());
    },
    onState(stats) {
      if (stats.state !== lastLink) {
        lastLink = stats.state;
        codeLayer?.push('link', `状态帧 ${stats.state}`);
      }
      onLinkState(stats);
    },
    onEvent(msg) {
      if (msg.type === 'action') {
        codeLayer?.push('action', `轮到 ${msg.title}`);
        codeLayer?.setAction(msg);
        // 只有夹取类动作才让方块出现（方块平时不存在，见 block.js）。
        // 自检模式下不给：那边是手动摆姿态验证抓取，后端这时可能正在播别的动作，
        // 事件一来就会把 allowGrab 冲掉，导致"有时抓得住有时抓不住"。
        if (!isSelftest) block?.setAction(msg.name);
      } else if (msg.type === 'state') {
        codeLayer?.push('transport', msg.paused ? '后端已暂停' : '后端已恢复');
      } else if (msg.type === 'hello') {
        // 后连上来的窗口靠 hello 里的这两个字段对齐
        if (msg.drive) applyDrive(msg.drive);
        if (msg.override) view.takeOver('remote');
      } else if (msg.type === 'drive') {
        applyDrive(msg);
      } else if (msg.type === 'override') {
        // 另一个窗口拖了滑块，这个窗口得同步停手
        if (msg.on) view.takeOver('remote');
        else view.release('remote');
      }
    },
  });
}

// code 视图：没有 3D 就不进渲染循环，靠定时器保证断流时状态也能刷新
if (VIEW_CODE && !VIEW_TWIN) {
  buildCodeLayer(jointsFromLimits());
  if (loadingEl) loadingEl.hidden = true;
  startLink();
  // 自检用：code 视图没有 __TWIN__（那套挂在 3D 上），单开一个轻量读数
  window.__CODE__ = () => ({
    view: VIEW,
    ready: !!link,
    frames: link?.stats.framesReceived ?? 0,
    applied: link?.stats.framesApplied ?? 0,
    dropped: link?.stats.framesDropped ?? 0,
    seqGaps: link?.stats.seqGaps ?? 0,
    hz: link?.stats.hz ?? null,
    state: link?.stats.state ?? null,
    drive: status.drive?.mode ?? null,
    targetDeg: status.targetDeg.slice(),
    actualDeg: status.actualDeg.slice(),
  });
  setInterval(() => {
    topline.sync({
      linkState: link?.stats.state ?? 'idle',
      hz: link?.stats.hz,
      paused: !!link?.stats.paused,
      viewState: view.state,
    });
    codeLayer?.sync(syncPayload());
  }, 200);
}

// ─────────────────────────────────────────────────────────── 3D

if (stage) {
  const { renderer, scene, camera } = stage;
  const clock = new THREE.Clock();

  loadRobot(scene, URDF_URL, {
    onReady(r) {
      robot = r;

      // 以下几步都必须在网格真到位之后做，否则包围盒算出来是 (0,0,0)
      tuneMaterials(r);
      groundModel(r);
      const framing = frameCamera(r, camera);

      // 方块和网格必须在取景之后加进场景，
      // 否则会把取景的包围盒带偏，机械臂在画面里变小
      addGround(scene, framing.radius);
      block = createBlockRig({
        scene,
        mesh: addRedBlock(scene),
        // 挂载点用 URDF 自己定义的抓取参考点，不是整块腕部
        gripperFrame: r.links.gripper_frame_link,
        edge: RED_BLOCK.edge,
      });
      controls = attachControls(camera, renderer.domElement, framing.center);

      const joints = listJoints(r);

      // both 视图在这里建代码层；twin 视图只有 3D，不建
      if (VIEW_CODE) buildCodeLayer(joints);
      // 左屏的角度菜单两个视图都可以有，但它长在 #screen-a 里，只有 twin/both 用得上
      if (VIEW_TWIN) buildAngleMenu(joints);

      // 调试钩子两个视图都要有：自检和探针靠 window.__TWIN__ 判断页面是否就绪
      installDebug({
        robot: r,
        renderer,
        scene,
        camera,
        framing,
        joints,
        urdf: URDF_URL,
        block,
        controls,
        // 自检脚本这类外部写入也要切到接管，否则渲染循环下一帧就把它覆盖掉
        onOverride: () => view.takeOver('external'),
      });

      startLink();

      if (loadingEl) loadingEl.hidden = true;
      console.log(`[twin] 模型就绪 · 关节 ${joints.length} 个 · 网格已加载`);
    },
    onError(err) {
      showError(`模型加载失败：${err?.message ?? err}`);
    },
  });

  renderer.setAnimationLoop(() => {
    // FROZEN / 接管态下也要消费 dt，否则恢复的第一帧 dt 是几秒，插值会瞬间跳到位
    const dt = clock.getDelta();
    const state = view.state;

    if (robot) {
      if (state === FOLLOW) {
        const k = 1 - Math.exp(-INTERP_LAMBDA * dt);
        for (let i = 0; i < JOINT_ORDER.length; i += 1) {
          const joint = robot.joints[JOINT_ORDER[i]];
          if (!joint) continue;
          const cur = readJointAngle(joint);
          joint.setJointValue(cur + (targetRad[i] - cur) * k);
        }
      }
      for (let i = 0; i < JOINT_ORDER.length; i += 1) {
        const joint = robot.joints[JOINT_ORDER[i]];
        const rad = joint ? readJointAngle(joint) : 0;
        appliedRad[i] = rad;
        status.appliedDeg[i] = rad * (180 / Math.PI);
      }
    }

    // 方块的抓放。判定用渲染中的夹爪角而不是后端目标值 —— 插值过程中也能正确接住
    block?.update(dt, { gripDeg: status.appliedDeg[5] });

    controls?.update();
    renderer.render(scene, camera);

    // 目标与实际差多少。夹爪不参与：它开合几十度属于正常动作，会把这个值顶起来
    let lag = 0;
    for (let i = 0; i < 5; i += 1) {
      lag = Math.max(lag, Math.abs(status.targetDeg[i] - status.appliedDeg[i]));
    }

    const linkState = link?.stats.state ?? 'idle';
    const hz = link?.stats.hz;
    const paused = !!link?.stats.paused;

    topline.sync({ linkState, hz, paused, viewState: state });
    codeLayer?.sync(syncPayload());
    panel?.sync({ viewState: state, appliedRad, linkState, hz, paused });
    angleMenu?.sync({ viewState: state, appliedRad, linkState, hz, paused });

    const dbg = window.__TWIN__;
    if (dbg && dbg.ready) {
      dbg.viewState = state;
      dbg.paused = paused;
      dbg.twin.targetDeg = status.targetDeg;
      dbg.twin.actualDeg = status.actualDeg;
      dbg.twin.appliedDeg = status.appliedDeg;
      dbg.twin.mode = status.mode;
      dbg.twin.modeName = MODE_NAMES[status.mode] ?? String(status.mode);
      dbg.twin.blend = status.blend;
      dbg.twin.grip = status.grip;
      dbg.twin.lag = Number(lag.toFixed(3));
      dbg.twin.drive = status.drive?.mode ?? null;
      dbg.view = VIEW;
      if (link) Object.assign(dbg.link, link.stats);

      // 方块的状态给自检看：是不是被夹住了、现在在哪、夹爪开口多少
      if (block) {
        block.mesh.getWorldPosition(tmpVec);
        const b = (dbg.block ??= { pos: [0, 0, 0] });
        b.held = block.held;
        // 方块不常驻：平时 opacity 是 0、visible 是 false，夹起来那一下才淡入
        b.visible = block.visible;
        b.opacity = Number(block.opacity.toFixed(3));
        b.allowGrab = block.allowGrab;
        b.biteDist = +block.biteDistance().toFixed(4);
        b.grabs = block.grabs;
        b.pos[0] = +tmpVec.x.toFixed(4);
        b.pos[1] = +tmpVec.y.toFixed(4);
        b.pos[2] = +tmpVec.z.toFixed(4);
        b.restY = RED_BLOCK.edge / 2;
        b.openingMm = +jawOpeningMm(status.appliedDeg[5]).toFixed(1);
      }
    }
  });
}
