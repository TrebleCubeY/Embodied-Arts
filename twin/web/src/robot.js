import * as THREE from 'three';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js';
// urdf-loader 是 default export，不是具名的
import URDFLoader from 'urdf-loader';
import { JOINT_ORDER } from './frame.js';

// 同一个 STL 会被多个 link 引用（SO-101 的 17 处 visual 只用到 13 个文件），
// 而 urdf-loader 给每个 mesh 都新建一个 STLLoader，也就是每次都重新取一遍。
// 这里自己按解析后的路径缓存几何，13 个文件各取一次。
const geometryCache = new Map();

function installMeshCache(loader) {
  const fallback = URDFLoader.prototype.defaultMeshLoader;
  loader.loadMeshCb = (path, manager, material, done) => {
    if (!/\.stl$/i.test(path)) {
      fallback.call(loader, path, manager, material, done);
      return;
    }

    const makeMesh = (geom) => new THREE.Mesh(geom, material || new THREE.MeshPhongMaterial());

    const cached = geometryCache.get(path);
    if (cached) {
      done(makeMesh(cached));
      return;
    }

    new STLLoader(manager).load(
      path,
      (geom) => {
        geometryCache.set(path, geom);
        done(makeMesh(geom));
      },
      null,
      (err) => done(null, err),
    );
  };
}

/**
 * 加载 URDF。
 *
 * 两条纪律：
 *  - onComplete 只代表 XML 解析完，此时网格还在加载。改材质 / 算包围盒 / 取景
 *    一律走 onReady（由 LoadingManager.onLoad 触发）。
 *  - workingPath 不要手动设，urdf-loader 会从 load(url) 自己推导出 URDF 所在目录，
 *    URDF 里写的相对路径 assets/xxx.stl 就是照这个目录解析的。
 */
export function loadRobot(scene, url, { onReady, onError }) {
  const manager = new THREE.LoadingManager();
  const loader = new URDFLoader(manager);
  // parseCollision 默认 false，只加载 13 个 visual 网格，不会翻倍
  installMeshCache(loader);

  let robot = null;
  let fired = false;
  let managerDone = false;

  const fire = (r) => {
    if (fired) return;
    fired = true;
    onReady(r);
  };

  // 兜住两种到达顺序：网格先加载完、或 URDF 回调先到
  manager.onLoad = () => {
    managerDone = true;
    if (robot) fire(robot);
  };

  loader.load(
    url,
    (r) => {
      robot = r;
      // Z-up → Y-up：只转根节点，逐个网格补旋转会把模型拆散
      r.rotation.set(-Math.PI / 2, 0, 0);
      scene.add(r);
      if (managerDone) fire(r);
    },
    undefined,
    (err) => {
      console.error('[twin] URDF 加载失败', err);
      onError?.(err);
    },
  );

  return { loader, manager };
}

/** 读一个关节当前的角度（弧度）。jointValue 在 urdf-loader 里是数组，
 *  为了兼容可能的标量实现，两种都认。 */
export function readJointAngle(joint) {
  const v = joint?.jointValue;
  if (typeof v === 'number') return v;
  if (Array.isArray(v) && v.length) return Number(v[0]) || 0;
  return 0;
}

/** 按 JOINT_ORDER 取出受驱动关节及其限位（弧度） */
export function listJoints(robot) {
  const out = [];
  for (const name of JOINT_ORDER) {
    const joint = robot.joints?.[name];
    if (!joint || joint.jointType === 'fixed') continue;
    const lower = Number.isFinite(joint.limit?.lower) ? joint.limit.lower : -Math.PI;
    const upper = Number.isFinite(joint.limit?.upper) ? joint.limit.upper : Math.PI;
    out.push({ name, joint, lower, upper });
  }
  return out;
}

/** 数一下真的加载到几个网格 —— 13 个零件才对 */
export function countMeshes(root) {
  let n = 0;
  root.traverse((o) => {
    if (o.isMesh) n += 1;
  });
  return n;
}

// 机身配色：**黑的主体 + 白的夹爪**，和桌上那台实物一致。
//
// URDF 里只有两个材质：
//   3d_printed = rgba 1.0 0.82 0.12（黄）—— 3D 打印件
//   sts3215    = rgba 0.1 0.1 0.1（黑灰）—— 舵机
// 光靠这两个材质名分不出"主体"和"夹爪"，所以按 link 名再切一刀：
//   gripper_link / moving_jaw_so101_v1_link 上的打印件 = 白（颚片）
//   其余打印件 = 磨砂黑（臂体）
//   舵机始终是深色（实物就是黑色塑料壳）
//
// 材质统一换成 MeshStandardMaterial：Phong 没有 roughness/metalness，
// 做不出"磨砂黑在纸背景上的那一点高光"，黑色会糊成一片。
const BODY_COLOR = new THREE.Color('#1a1b1f');
const SERVO_COLOR = new THREE.Color('#0d0e10');
const JAW_COLOR = new THREE.Color('#f3f0e9');

/** 往上找最近的 URDF link 名。夹爪判定靠它，不靠网格文件名 */
function linkNameOf(obj) {
  let p = obj;
  while (p) {
    if (p.isURDFLink) return p.name || '';
    p = p.parent;
  }
  return '';
}

const isJawLink = (name) => /^(gripper|moving_jaw)/i.test(name);

/** 三个共享材质，全模型最多三份，减少 draw call 与状态切换 */
function makePalette() {
  const mk = (kind, color, roughness, metalness) =>
    new THREE.MeshStandardMaterial({
      name: `twin-${kind}`,
      color,
      roughness,
      metalness,
      side: THREE.DoubleSide,
    });
  return {
    body: mk('body', BODY_COLOR, 0.54, 0.16),
    servo: mk('servo', SERVO_COLOR, 0.36, 0.46),
    jaw: mk('jaw', JAW_COLOR, 0.66, 0.02),
  };
}

export function tuneMaterials(robot) {
  const palette = makePalette();
  robot.traverse((o) => {
    if (!o.isMesh) return;
    const jaw = isJawLink(linkNameOf(o));
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    // 舵机永远深色：夹爪里那颗小电机变白会显得很假
    const picked = mats.map((m) => {
      if (!m) return m;
      if (m.name === 'sts3215') return palette.servo;
      return jaw ? palette.jaw : palette.body;
    });
    o.material = Array.isArray(o.material) ? picked : picked[0];
    // STL 三角面绕向不一定一致，双面渲染已经写在材质上；
    // 投影是白底上唯一能读出"它站在地上"的线索，所以每个网格都参与。
    o.castShadow = true;
    o.receiveShadow = true;
  });
}

/** 把模型落到 y=0 平面上，让网格有意义 */
export function groundModel(robot) {
  robot.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(robot);
  if (Number.isFinite(box.min.y)) {
    robot.position.y -= box.min.y;
    robot.updateMatrixWorld(true);
  }
}

/**
 * 自动取景。必须在网格真到位之后调（即 onReady 里），
 * 在 onComplete 里算会得到 (0,0,0)，相机定位全错。
 */
export function frameCamera(robot, camera) {
  robot.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(robot);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());

  const radius = Math.max(size.length() * 0.5, 1e-3);
  const vFov = THREE.MathUtils.degToRad(camera.fov);
  const hFov = 2 * Math.atan(Math.tan(vFov / 2) * camera.aspect);
  const fov = Math.max(Math.min(vFov, hFov), 1e-3); // 窄的那一边决定距离
  // 0.95 会把整条臂顶到画面边缘。白屏之后画面里只有臂，可以再收紧一点：
  // 观众要看的是姿态，不是留白。
  const dist = (radius / Math.sin(fov / 2)) * 0.82;

  // 默认机位：斜前上方，接近观众站在实机前面的角度
  const dir = new THREE.Vector3(0.92, 0.40, 1.2).normalize();
  camera.position.copy(center).addScaledVector(dir, dist);
  camera.near = Math.max(dist / 500, 0.0005);
  camera.far = dist * 200;
  camera.updateProjectionMatrix();
  camera.lookAt(center);

  return { box, size, center, radius, dist };
}
