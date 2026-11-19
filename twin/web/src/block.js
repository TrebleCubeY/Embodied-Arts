// 方块的搬运状态。数字层不跑物理，所以"夹住"是一个约定，不是解出来的：
// 夹爪合到能包住方块的开口、并且咬合点确实落在方块该在的位置，就把方块挂到
// gripper_frame_link 底下（用 three 的 attach，世界变换不变）。
//
// **方块平时不存在。** 它不常驻地面，而是：
//   夹爪合拢到位 → 直接以淡入的方式出现在夹爪里（0.35s 不透明度 0→1）
//   松口         → 停在放下的地方淡出（0.5s），淡完回原位待命
// 所以观众看到的是"夹起来的那一下，方块才成立"，而不是地上有个方块被拿走。
//
// 为什么用夹爪角而不是等后端发一个"抓到了"的信号：状态帧是给渲染用的定长二进制，
// 加字段要两端一起改；而夹爪开合量本来就能从 grip 角算出来，够用了。

import * as THREE from 'three';

// 夹爪行程：全闭 -10° 到全开 100° 之间，动颚走 55.6mm（URDF 实测）。
const JAW_CLOSED_DEG = -10;
const JAW_OPEN_DEG = 100;
const JAW_TRAVEL_MM = 55.6;

// 方块夹住时该待的点，相对 gripper_frame_link 的局部坐标。
//
// **别把它当成 gripper_frame_link 的原点。** 那个原点在夹爪末端，跟两颚的实际位置
// 差得远，拿它当抓取点会让夹爪在方块头顶合拢，看上去就是"夹不起来"。
//
// 这个值是实测的：把方块摆到 `RED_BLOCK.position`，在抓取姿态
// `shoulder_lift 28 / elbow_flex 12.5 / wrist_roll -60 / gripper 15` 下
// 换算成 gripper_frame_link 的局部坐标。姿态或方块位置任一改动，这个值都要重算。
const GRASP_LOCAL = new THREE.Vector3(-0.0133, -0.0030, 0.0083);

// 淡入比淡出快一点，看起来像"抓住了"而不是"慢慢显影"
const FADE_IN_SEC = 0.35;
const FADE_OUT_SEC = 0.5;

/** 夹爪当前开口（毫米）。用来判断能不能罩住方块。 */
export function jawOpeningMm(gripDeg) {
  const t = (gripDeg - JAW_CLOSED_DEG) / (JAW_OPEN_DEG - JAW_CLOSED_DEG);
  return JAW_TRAVEL_MM * Math.min(1, Math.max(0, t));
}

/**
 * @param scene          方块待命时挂的父节点（淡出后回原位）
 * @param mesh           方块
 * @param gripperFrame   夹爪（urdf 里的 gripper_frame_link），挂载和判定都相对它
 * @param edge           方块边长（米）
 */
export function createBlockRig({ scene, mesh, gripperFrame, edge }) {
  const edgeMm = edge * 1000;
  const restY = edge / 2; // 底面贴地

  // 滞回：合到 edge+4 就抓，张到 edge+8 才松。中间那段维持原状，
  // 免得夹爪角在边界上抖的时候方块跟着一闪一闪。
  const grabMM = edgeMm + 4;
  const releaseMM = edgeMm + 8;
  // 咬合点离方块待命点多远之内才认。抓取姿态下这两个点本来就是重合的，
  // 这个值只是防止夹爪路过别处时误抓。
  const reachM = 0.02;

  const home = mesh.position.clone();
  const bite = new THREE.Vector3();
  const blockAt = new THREE.Vector3();

  let held = false;
  // 抓了几次。调试用：如果只涨不落，说明判定进去了但立刻被放掉
  let grabs = 0;
  // 只有"当前在演的动作是夹取类动作"时才允许抓起。
  // 光看几何距离不够：零位和挥手时夹爪也会扫过那个位置，方块会平白出现一次。
  // 动作名由后端 action 事件给（setAction），没有动作在演（遥操/停住）时一律不抓。
  let allowGrab = false;
  // 透明度是渲染状态，不往外暴露；外面只用 held
  let alpha = mesh.material.opacity ?? 1;
  let target = alpha;

  // 一开始就按"不存在"处理（addRedBlock 建出来时 opacity 是 0）
  mesh.visible = alpha > 0.01;
  mesh.material.transparent = true;

  function bitePoint() {
    // 要祖先链的最新变换：这个方法可能在渲染之前被调用
    gripperFrame.updateWorldMatrix(true, false);
    return bite.copy(GRASP_LOCAL).applyMatrix4(gripperFrame.matrixWorld);
  }

  function grab() {
    gripperFrame.updateWorldMatrix(true, false);
    gripperFrame.attach(mesh);
    held = true;
    grabs += 1;
    target = 1;
  }

  function drop() {
    // attach 回场景会保持世界变换，所以它就停在放下的地方淡出 —— 不会跳回原位
    scene.attach(mesh);
    held = false;
    target = 0;
  }

  function update(dt, { gripDeg }) {
    const open = jawOpeningMm(gripDeg);

    if (held) {
      // 动作切走（或松口）就放下并淡出
      if (!allowGrab || open > releaseMM) drop();
    } else if (allowGrab && open <= grabMM) {
      mesh.getWorldPosition(blockAt);
      if (blockAt.distanceTo(bitePoint()) <= reachM) grab();
    }

    // 透明度：线性逼近目标，够平滑也够便宜
    if (alpha !== target) {
      const step = dt / (target > alpha ? FADE_IN_SEC : FADE_OUT_SEC);
      alpha += Math.min(step, Math.abs(target - alpha)) * (target > alpha ? 1 : -1);
      if (Math.abs(target - alpha) < 1e-3) alpha = target;
      mesh.material.opacity = alpha;
      mesh.visible = alpha > 0.01;
    }

    // 淡干净了才回原位待命：这样"放下的位置"能一直显示到它消失
    if (!held && alpha <= 0.001 && mesh.parent === scene) {
      const away = mesh.position.distanceTo(home) > 1e-6;
      if (away) mesh.position.copy(home);
    }
  }

  return {
    mesh,
    update,
    /** 方块待命点到夹爪咬合点的距离（米）。调试/自检用：判断"为什么没抓"。 */
    biteDistance() {
      mesh.getWorldPosition(blockAt);
      return blockAt.distanceTo(bitePoint());
    },
    /** 当前在演哪个动作。只有夹取类动作才允许方块出现，其余一律不抓。 */
    setAction(name) {
      allowGrab = typeof name === 'string' && /pick|grasp|夹/i.test(name);
      if (!allowGrab && held) drop();
    },
    get held() {
      return held;
    },
    get allowGrab() {
      return allowGrab;
    },
    get grabs() {
      return grabs;
    },
    get home() {
      return home;
    },
    /** 现在看得见吗（自检与调试用） */
    get visible() {
      return alpha > 0.01;
    },
    get opacity() {
      return alpha;
    },
    /** 放回原位、松开。自检和刷新后用得上。 */
    reset() {
      if (held) drop();
      mesh.position.copy(home);
      alpha = 0;
      target = 0;
      mesh.material.opacity = 0;
      mesh.visible = false;
    },
  };
}
