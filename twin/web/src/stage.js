import * as THREE from 'three';

const params = new URLSearchParams(location.search);

// 自检模式要读回像素，必须保留绘制缓冲；平时关掉更省。
export const isSelftest = params.has('selftest');
export const showPanel = params.get('panel') !== '0';
export const showDiag = params.has('diag');

export const BG_HEX = 0x101216;

export function createStage(container) {
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    preserveDrawingBuffer: isSelftest,
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = false;
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(BG_HEX);

  // near/far 等取景算出来后由 robot.js 收窄，这里先给宽容值
  const camera = new THREE.PerspectiveCamera(
    42,
    Math.max(container.clientWidth, 1) / Math.max(container.clientHeight, 1),
    0.001,
    100,
  );

  // STL 只有几何没有贴图，靠光照读出体积。三点光够用。
  const key = new THREE.DirectionalLight(0xffffff, 2.6);
  key.position.set(1.4, 2.2, 1.6);
  const fill = new THREE.DirectionalLight(0xffffff, 0.85);
  fill.position.set(-1.8, 0.7, -1.0);
  const rim = new THREE.DirectionalLight(0xffffff, 0.5);
  rim.position.set(-0.4, 0.6, -2.0);
  scene.add(key, fill, rim);
  scene.add(new THREE.HemisphereLight(0xdce6f2, 0x1b2029, 1.15));

  function resize() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);

  return { renderer, scene, camera, resize };
}

// 接地网格：给机械臂一个参照平面，不然悬空的模型读不出姿态和高度
export function addGround(scene, radius) {
  const cell = 0.05;
  const span = Math.max(0.6, Math.ceil((radius * 3) / cell) * cell);
  const divisions = Math.round(span / cell);

  const grid = new THREE.GridHelper(span, divisions, 0x35404f, 0x232a34);
  grid.material.transparent = true;
  grid.material.opacity = 0.9;
  scene.add(grid);
  return grid;
}

// 红色方块：纯几何体，不跑物理。
//
// ## 位置怎么来的
//
// 用"对方块不穿透 + 方块某个轴向的两个相对面外侧都贴着夹爪点"扫出来的，
// 判据里不含"哪个 link 是定颚、哪个轴是开合方向"这类假设 —— 前面几版全都栽
// 在这些假设上：把 `gripper_link` 的第 0 个网格（其实是 sts3215 电机）当定颚、
// 把整团夹爪点云混在一起求最近距离（结果被伸出去的颚尖满足，方块实际在夹爪外面）。
//
// 扫出来的位置 (0.3820, 0.0125, 0.0060) 是在抓取姿态
// `shoulder_lift 30 / elbow_flex 12.5 / wrist_roll -60` 下成立的：
// 到动颚 18.1mm、到定颚 19.2mm，两侧接触方向内积 -0.82。半宽 12.5mm。
//
// ## 为什么边长是 2.5cm
//
// 两颚在贴地高度上的可用间隙就这么大。3.5cm 的方块在这个姿态下无论放哪都会被
// 夹爪穿透，或者只有一侧能贴住（表现为"方块在夹子外面"）。2.5cm 是能稳定夹住的
// 尺寸，3.0cm 也能夹但两侧余量已经很紧。
//
// ## 另外一处关键
//
// `wrist_roll` 必须是 -60 左右。原来用的 -150 会让两颚**上下**开合（腕件的 y 范围
// 0.0196~0.0695、动颚 -0.031~0.051，一上一下），方块躺在地上根本没法被夹 ——
// 下颚要么插进地面，要么方块被上颚整个盖住，画面里看不到。
//
// **它和 motions/pick_red_block.json 的抓取姿态、block.js 的 GRASP_LOCAL 是一组，
// 改一处必须改另两处。** 将来和实机对齐时也是改这一处。
export const RED_BLOCK = {
  edge: 0.025,
  // 底面贴地，所以 y = edge / 2
  position: [0.3720, 0.0125, 0.0060],
};

export function addRedBlock(scene, { edge = RED_BLOCK.edge, position = RED_BLOCK.position } = {}) {
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(edge, edge, edge),
    new THREE.MeshStandardMaterial({ color: 0xb8342a, roughness: 0.62, metalness: 0 }),
  );
  mesh.position.set(position[0], position[1], position[2]);
  scene.add(mesh);
  return mesh;
}
