// 方块的搬运状态。数字层不跑物理，所以"夹住"是一个约定，不是解出来的：
// 夹爪合到能包住方块的开口、并且方块确实落在两颚的咬合点上，就把方块挂到
// gripper_frame_link 底下（用 three 的 attach，世界变换不变，所以看不到它"跳"到夹爪上）。
// 挂上之后位置由父节点带，方块自然跟着臂走，包括腕部自转。
// 松口就放回场景里自由落体——下落是纯运动学，不接物理引擎。
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

const GRAVITY = 9.8;

/** 夹爪当前开口（毫米）。用来判断能不能罩住方块。 */
export function jawOpeningMm(gripDeg) {
  const t = (gripDeg - JAW_CLOSED_DEG) / (JAW_OPEN_DEG - JAW_CLOSED_DEG);
  return JAW_TRAVEL_MM * Math.min(1, Math.max(0, t));
}

/**
 * @param scene          方块平时待的父节点
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
  // 方块中心离咬合点多远之内才认。抓取姿态下这两个点本来就是重合的，
  // 这个值只是防止夹爪路过别处时误抓。
  const reachM = 0.02;

  const home = mesh.position.clone();
  const bite = new THREE.Vector3();
  const blockAt = new THREE.Vector3();

  let held = false;
  let fallVy = 0;

  function bitePoint() {
    // 要祖先链的最新变换：这个方法可能在渲染之前被调用
    gripperFrame.updateWorldMatrix(true, false);
    return bite.copy(GRASP_LOCAL).applyMatrix4(gripperFrame.matrixWorld);
  }

  function grab() {
    gripperFrame.updateWorldMatrix(true, false);
    gripperFrame.attach(mesh);
    held = true;
    fallVy = 0;
  }

  function drop() {
    scene.attach(mesh);
    held = false;
    fallVy = 0;
  }

  function update(dt, { gripDeg }) {
    if (held) {
      if (jawOpeningMm(gripDeg) > releaseMM) drop();
      return; // 挂着的时候由父节点带位置，不用管
    }

    mesh.getWorldPosition(blockAt);
    if (jawOpeningMm(gripDeg) <= grabMM && blockAt.distanceTo(bitePoint()) <= reachM) {
      grab();
      return;
    }

    // 自由落体：离地才动，落地就停。只有松手那一下用得上。
    if (mesh.position.y > restY + 1e-6) {
      fallVy -= GRAVITY * dt;
      mesh.position.y += fallVy * dt;
      if (mesh.position.y <= restY) {
        mesh.position.y = restY;
        fallVy = 0;
      }
    } else {
      mesh.position.y = restY;
    }
  }

  return {
    mesh,
    update,
    get held() {
      return held;
    },
    get home() {
      return home;
    },
    /** 放回原位、松开。自检和刷新后用得上。 */
    reset() {
      if (held) drop();
      mesh.position.copy(home);
      fallVy = 0;
    },
  };
}
