// 自检钩子。页面自己报出可判定的量，无头脚本读这些值来判断，
// 而不是靠肉眼看截图。?diag=1 时同一份数据也会打到页面上。

import * as THREE from 'three';

import { BG_HEX, showDiag } from './stage.js';
import { countMeshes, readJointAngle } from './robot.js';

// 直接按十六进制拆，不用 THREE.Color —— Color 会把 sRGB 转成线性工作空间，比不了
const BG_RGB = [(BG_HEX >> 16) & 255, (BG_HEX >> 8) & 255, BG_HEX & 255];

export function installDebug({ robot, renderer, scene, camera, framing, joints, urdf, block, controls, onJointChange, onOverride }) {
  const gl = renderer.getContext();

  function sampleNonBg() {
    // 必须先渲染：preserveDrawingBuffer 只在自检模式下开，否则这里读到的是空缓冲
    renderer.render(scene, camera);
    const w = gl.drawingBufferWidth;
    const h = gl.drawingBufferHeight;
    const buf = new Uint8Array(w * h * 4);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);

    let hit = 0;
    let total = 0;
    for (let i = 0; i < buf.length; i += 4 * 17) {
      total += 1;
      const d =
        Math.abs(buf[i] - BG_RGB[0]) +
        Math.abs(buf[i + 1] - BG_RGB[1]) +
        Math.abs(buf[i + 2] - BG_RGB[2]);
      if (d > 24) hit += 1;
    }
    return total ? hit / total : 0;
  }

  const box = framing.box;

  window.__TWIN__ = {
    ready: true,
    urdf,
    // 下面这几项由 main.js 的渲染循环每帧更新，自检与 ?diag=1 直接读
    viewState: 'FOLLOW',
    paused: false,
    link: {}, // 连接状态与统计（framesReceived / maxGapMs / bootId / …）
    twin: {}, // 最新的 target / actual / applied 与 mode / blend / grip
    jointNames: joints.map((j) => j.name),
    jointLimits: Object.fromEntries(
      joints.map((j) => [j.name, { lower: j.lower, upper: j.upper }]),
    ),
    meshCount: countMeshes(robot),
    bbox: {
      min: box.min.toArray().map(round4),
      max: box.max.toArray().map(round4),
      size: framing.size.toArray().map(round4),
    },
    center: framing.center.toArray().map(round4),
    camera: camera.position.toArray().map(round4),
    viewport: [gl.drawingBufferWidth, gl.drawingBufferHeight],
    webgl: {
      version: gl.getParameter(gl.VERSION),
      renderer: unmaskedRenderer(gl),
    },
    nonBgRatio: null,
    errors: [],
  };

  // 自检里要按时间窗采两次来算帧率和静息抖动，所以得能清零重来
  window.__TWIN_RESET_STATS__ = () => {
    const s = window.__TWIN__.link;
    if (!s) return false;
    for (const k of ['framesReceived', 'framesApplied', 'framesDropped', 'seqGaps', 'maxGapMs']) s[k] = 0;
    s.lastFrameAt = null;
    return true;
  };

  const afterJointChange = () => {
    robot.updateMatrixWorld(true);
    // 外部写入（自检脚本）也要把前端切到接管，否则渲染循环下一帧就把它覆盖掉
    onOverride?.();
    onJointChange?.();
  };

  // 摆一个关节，验证「真在跑运动学」而不是贴了一张静态图
  window.__TWIN_SET_JOINT__ = (name, rad) => {
    const j = robot.joints?.[name];
    if (!j) return false;
    j.setJointValue(Number(rad));
    window.__TWIN__.jointLimits[name].value = Number(rad);
    afterJointChange();
    return true;
  };

  window.__TWIN_SET_ALL__ = (rad = 0) => {
    for (const { name, joint } of joints) {
      joint.setJointValue(Number(rad));
      window.__TWIN__.jointLimits[name].value = Number(rad);
    }
    afterJointChange();
    return joints.length;
  };

  // 末端位置。自检靠它数值化验证关节轴向，比看截图可靠
  window.__TWIN_EE__ = () => {
    const link = robot.links?.gripper_frame_link ?? robot.links?.gripper_link;
    if (!link) return null;
    robot.updateMatrixWorld(true);
    return link.getWorldPosition(new THREE.Vector3()).toArray().map(round4);
  };

  // 任一 link 的世界包围盒。用来量夹爪开合这类长度量，比看截图可靠
  window.__TWIN_LINK_BOX__ = (name) => {
    const link = robot.links?.[name];
    if (!link) return null;
    robot.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(link);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    return {
      center: center.toArray().map(round4),
      size: size.toArray().map(round4),
      min: box.min.toArray().map(round4),
      max: box.max.toArray().map(round4),
    };
  };

  // 某个 link 下所有网格顶点的世界坐标。量「咬合面在哪」这类问题包围盒不够用：
  // gripper_link 是整块腕部总成，AABB 有 12cm 见方，拿它算出来的间隙毫无意义。
  // stride 抽稀，几万个顶点也没关系。
  //
  // meshIndex 指定第几个网格（traverse 的顺序，一般就是 URDF 里 <visual> 的顺序）。
  // gripper_link 里装着两个网格 —— 腕部电机和定颚，混在一起量出来的中位数毫无意义。
  window.__TWIN_LINK_POINTS__ = (name, stride = 1, meshIndex = null) => {
    const link = robot.links?.[name];
    if (!link) return null;
    robot.updateMatrixWorld(true);
    const out = [];
    const v = new THREE.Vector3();
    let idx = -1;
    link.traverse((o) => {
      if (!o.isMesh || !o.geometry?.attributes?.position) return;
      idx += 1;
      if (meshIndex !== null && idx !== meshIndex) return;
      const pos = o.geometry.attributes.position;
      for (let i = 0; i < pos.count; i += stride) {
        v.fromBufferAttribute(pos, i).applyMatrix4(o.matrixWorld);
        out.push([+v.x.toFixed(5), +v.y.toFixed(5), +v.z.toFixed(5)]);
      }
    });
    return out;
  };

  window.__TWIN_LINKS__ = () => Object.keys(robot.links ?? {});

  // 方块放回原位、松开。自检需要个干净起点 —— 跑到那一步时动作循环可能
  // 已经把它夹起来搬到别处了，不重置的话断言全是谎话。
  window.__TWIN_RESET_BLOCK__ = () => {
    if (!block) return false;
    block.reset();
    return true;
  };

  // 把方块挪到指定位置。调 RED_BLOCK.position 时用它快速试 ——
  // 改文件、重启后端、再截图，一轮下来慢一个数量级。
  window.__TWIN_PLACE_BLOCK__ = (x, y, z) => {
    if (!block) return false;
    block.mesh.position.set(Number(x), Number(y), Number(z));
    return true;
  };

  // 方块到底在哪、有没有挂在夹爪上、父节点是谁。
  // 调位置时必须先看这个：否则"改完看不到方块"根本分不清是位置没生效、
  // 已经被夹走了，还是被渲染循环挪回原位了。
  window.__TWIN_BLOCK__ = () => {
    if (!block) return null;
    const p = block.mesh.getWorldPosition(new THREE.Vector3());
    return {
      world: p.toArray().map(round4),
      local: block.mesh.position.toArray().map(round4),
      held: block.held,
      parent: block.mesh.parent?.name || block.mesh.parent?.type || '(无父节点)',
      home: block.home.toArray().map(round4),
      size: block.mesh.geometry?.parameters
        ? [block.mesh.geometry.parameters.width, block.mesh.geometry.parameters.height]
        : null,
    };
  };

  // 把镜头摆到指定点旁边。核对夹爪这种局部几何时只靠滚轮推近没用 ——
  // OrbitControls 的缩放绕视口中心走，而夹爪常常在画面角落，推得越近越看不见它。
  window.__TWIN_LOOK_AT__ = (x, y, z, dist = 0.25, dx = 1, dy = 0.6, dz = 0.85) => {
    const t = new THREE.Vector3(Number(x), Number(y), Number(z));
    const dir = new THREE.Vector3(Number(dx), Number(dy), Number(dz)).normalize();
    camera.position.copy(t).addScaledVector(dir, Number(dist));
    camera.up.set(0, 1, 0);
    camera.lookAt(t);
    if (controls) {
      controls.target.copy(t);
      controls.update();
    }
    return {
      camera: camera.position.toArray().map(round4),
      target: t.toArray().map(round4),
    };
  };

  // 世界坐标转成某个 link 的局部坐标。量挂载偏移（方块该挂在夹爪的哪个位置）
  // 和将来跟实机对齐零位时都要用。
  window.__TWIN_TO_LOCAL__ = (name, x, y, z) => {
    const link = robot.links?.[name];
    if (!link) return null;
    robot.updateMatrixWorld(true);
    const v = new THREE.Vector3(Number(x), Number(y), Number(z));
    link.worldToLocal(v);
    return v.toArray().map(round4);
  };

  // 反过来：局部坐标转世界。搜姿态时用它算"咬合点在当前姿态下落到哪"。
  window.__TWIN_FROM_LOCAL__ = (name, x, y, z) => {
    const link = robot.links?.[name];
    if (!link) return null;
    robot.updateMatrixWorld(true);
    const v = new THREE.Vector3(Number(x), Number(y), Number(z));
    link.localToWorld(v);
    return v.toArray().map(round4);
  };

  window.__TWIN_SAMPLE__ = () => {
    const r = sampleNonBg();
    window.__TWIN__.nonBgRatio = r;
    return r;
  };

  window.addEventListener('error', (e) => window.__TWIN__.errors.push(String(e.message)));
  window.addEventListener('unhandledrejection', (e) => window.__TWIN__.errors.push(String(e.reason)));

  if (showDiag) {
    const el = document.getElementById('diag');
    el.hidden = false;
    const tick = () => {
      window.__TWIN_SAMPLE__();
      el.textContent = JSON.stringify(window.__TWIN__, null, 2);
      setTimeout(tick, 600);
    };
    tick();
  }
}

function round4(n) {
  return Number(Number(n).toFixed(4));
}

function unmaskedRenderer(gl) {
  const ext = gl.getExtension('WEBGL_debug_renderer_info');
  return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
}
