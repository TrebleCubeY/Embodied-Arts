import * as THREE from 'three';

const params = new URLSearchParams(location.search);

// 自检模式要读回像素，必须保留绘制缓冲；平时关掉更省。
export const isSelftest = params.has('selftest');
export const showPanel = params.get('panel') !== '0';
export const showDiag = params.has('diag');

// 场景底色。必须和 styles.css 里 #screen-a 的 --stage（也就是 --paper）是同一个值 ——
// 自检靠"非背景像素占比"判断模型画出来了没，两边不一致就会把整个画面算成背景。
export const BG_HEX = 0xf3f1ec;

export function createStage(container) {
  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    preserveDrawingBuffer: isSelftest,
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  // 白底上的黑物件需要一层投影才落得住地，否则看起来像贴纸。
  // 用 PCFSoft，阴影边缘有一点毛，比硬边更像摄影棚里的那种光。
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
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

  // 白底 + 黑物：难点是暗部会不会死黑。所以环境光要给足（当白墙的反光），
  // 再靠一盏背后的 rim 勾出轮廓 —— 黑色物件的体积全靠这条轮廓线读出来。
  const key = new THREE.DirectionalLight(0xfff4e2, 2.35);
  key.position.set(1.5, 2.3, 1.5);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.near = 0.1;
  key.shadow.camera.far = 6;
  key.shadow.camera.left = -0.85;
  key.shadow.camera.right = 0.85;
  key.shadow.camera.top = 0.95;
  key.shadow.camera.bottom = -0.35;
  key.shadow.bias = -0.0006;
  key.shadow.normalBias = 0.015;

  const fill = new THREE.DirectionalLight(0xe4edf8, 0.55);
  fill.position.set(-1.9, 0.7, -1.0);

  // 轮廓光：从背后偏左打过来，只为了让黑色边缘亮出一条线
  const rim = new THREE.DirectionalLight(0xffffff, 0.95);
  rim.position.set(-0.5, 0.55, -2.1);

  scene.add(key, fill, rim);
  // 半球光当"白墙反射"：地面亮度托住暗部，天空色保持纸的暖调
  scene.add(new THREE.HemisphereLight(0xffffff, 0xcfc8ba, 0.85));

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

// 接地：一张极淡的坐标网格 + 一层只显示阴影的平面。
//
// 网格交代尺度（不然悬空的模型读不出姿态和高度），阴影交代重力（不然它像一张贴纸）。
// 白底上这两样都必须很轻 —— 观众先看到的是机械臂，不是背景。
export function addGround(scene, radius) {
  const cell = 0.05;
  const span = Math.max(0.6, Math.ceil((radius * 3) / cell) * cell);
  const divisions = Math.round(span / cell);

  const grid = new THREE.GridHelper(span, divisions, 0xd7d1c4, 0xe2ddd2);
  grid.material.transparent = true;
  grid.material.opacity = 0.7;
  scene.add(grid);

  const shadow = new THREE.Mesh(
    new THREE.PlaneGeometry(span * 1.7, span * 1.7),
    new THREE.ShadowMaterial({ color: 0x14120e, opacity: 0.17 }),
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = 0.0006;
  shadow.receiveShadow = true;
  shadow.renderOrder = -1;
  scene.add(shadow);

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
// 现在的值 (0.3720, 0.0125, 0.0060) 是在扫描结果附近**手调过的**，
// 不是扫描输出的原值。配套的抓取姿态是
// `shoulder_lift 28 / elbow_flex 12.5 / wrist_roll -60`。半宽 12.5mm。
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
    // 一开始就透明且不可见：方块不常驻场景，只有真的夹起来的那一下才淡入出现。
    // 这里必须 transparent:true，否则 opacity 会被忽略（Material 的透明通道要显式打开）。
    new THREE.MeshStandardMaterial({
      color: 0xb8342a,
      roughness: 0.62,
      metalness: 0,
      transparent: true,
      opacity: 0,
    }),
  );
  mesh.position.set(position[0], position[1], position[2]);
  mesh.visible = false;
  scene.add(mesh);
  return mesh;
}
