import * as THREE from 'three';

import { JOINT_ORDER, DEG2RAD, MODE_NAMES, defaultStateUrl } from './frame.js';
import { showPanel, RED_BLOCK, createStage, addGround, addRedBlock } from './stage.js';
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
import { createViewState, FOLLOW } from './view-state.js';
import { installDebug } from './debug.js';

const URDF_URL = './models/so101/so101_new_calib.urdf';

// 渲染插值系数（技术方案第 6.2 节）。λ 是这里唯一的旋钮：
// 小了顺滑但会抹掉微小残差，大了残差保留但大动作能看出阶梯。现场调。
const INTERP_LAMBDA = 15;

const viewport = document.getElementById('viewport');
const loadingEl = document.getElementById('loading');
const panelEl = document.getElementById('panel');

if (!showPanel) panelEl.hidden = true;

// WebGL 起不来时不要白屏：把原因写下来，自检脚本据此区分「环境问题」和「代码写错」
function showError(msg) {
  loadingEl.hidden = false;
  loadingEl.classList.add('err');
  loadingEl.textContent = msg;
  window.__TWIN__ = { ready: false, error: msg };
  console.error('[twin]', msg);
}

let stage = null;
try {
  stage = createStage(viewport);
} catch (err) {
  showError(`WebGL 初始化失败：${err?.message ?? err}`);
}

if (stage) {
  const { renderer, scene, camera } = stage;
  const clock = new THREE.Clock();

  let robot = null;
  let controls = null;
  let panel = null;
  let link = null;
  let block = null;
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
  };

  const view = createViewState({
    onChange(next, prev) {
      console.log(`[twin] 视图状态 ${prev} → ${next}`);
    },
  });

  // 断流就冻结在最后一帧：不往外推，也不跳回零位（技术方案第 8 节）
  function onLinkState(stats) {
    if (stats.state === 'open') view.thaw('link');
    else view.freeze('link');
  }

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
      panel = showPanel
        ? createJointPanel(panelEl, joints, {
            onManual: () => view.takeOver('panel'),
            onPause: () => link?.send({ type: 'pause' }),
            onResume: () => link?.send({ type: 'resume' }),
            onFollow: () => view.release('panel'),
            onKeyframe: () => {
              const deg = status.appliedDeg.map((v) => Number(v.toFixed(2)));
              if (!link?.send({ type: 'keyframe', joints: deg })) {
                console.warn('[twin] 后端没连上，这个姿态没记下来');
              }
            },
          })
        : null;

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

      const stateUrl = defaultStateUrl();
      if (stateUrl) {
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
          },
          onState: onLinkState,
          onEvent(msg) {
            if (msg.type === 'action') console.log(`[twin] 轮到 ${msg.title}`);
          },
        });
      } else {
        console.log('[twin] ?ws=0：不连后端，只显示静态模型');
      }

      loadingEl.hidden = true;
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

    panel?.sync({
      viewState: state,
      appliedRad,
      linkState: link?.stats.state ?? 'idle',
      hz: link?.stats.hz,
      paused: link?.stats.paused,
    });

    const dbg = window.__TWIN__;
    if (dbg && dbg.ready) {
      dbg.viewState = state;
      dbg.paused = !!link?.stats.paused;
      dbg.twin.targetDeg = status.targetDeg;
      dbg.twin.actualDeg = status.actualDeg;
      dbg.twin.appliedDeg = status.appliedDeg;
      dbg.twin.mode = status.mode;
      dbg.twin.modeName = MODE_NAMES[status.mode] ?? String(status.mode);
      dbg.twin.blend = status.blend;
      dbg.twin.grip = status.grip;
      if (link) Object.assign(dbg.link, link.stats);

      // 方块的状态给自检看：是不是被夹住了、现在在哪、夹爪开口多少
      if (block) {
        block.mesh.getWorldPosition(tmpVec);
        const b = (dbg.block ??= { pos: [0, 0, 0] });
        b.held = block.held;
        b.pos[0] = +tmpVec.x.toFixed(4);
        b.pos[1] = +tmpVec.y.toFixed(4);
        b.pos[2] = +tmpVec.z.toFixed(4);
        b.restY = RED_BLOCK.edge / 2;
        b.openingMm = +jawOpeningMm(status.appliedDeg[5]).toFixed(1);
      }
    }
  });
}
