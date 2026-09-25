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

// 机身颜色。URDF 里只有两个材质：
//   3d_printed = rgba 1.0 0.82 0.12（黄）—— 3D 打印件，这里改成白色
//   sts3215    = rgba 0.1 0.1 0.1（黑灰）—— 舵机，保持不动
//
// 判据同时看材质名和当前颜色：只认名字的话，换 URDF 或哪天改了名字就会静默失效；
// 只看颜色的话，别的偏暖色的件会被误伤。
//
// 数值写在线性空间里（urdf-loader 是用 setRGB 直接写进去的，没做 sRGB 转换），
// 所以 0.85 显示出来比十六进制里的 0.85 更亮。嫌过曝就往下调一点。
const PRINTED_WHITE = new THREE.Color().setRGB(0.85, 0.85, 0.84);
const isPrintedPart = (m) =>
  m.name === '3d_printed' || Boolean(m.color && m.color.r > 0.5 && m.color.b < 0.5);

/** STL 的三角面绕向不一定一致，双面渲染最省事，也避免背面出现黑洞。
 *  顺手把 3D 打印件从黄色改成白色（舵机不动）。 */
export function tuneMaterials(robot) {
  robot.traverse((o) => {
    if (!o.isMesh) return;
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    for (const m of mats) {
      if (!m) continue;
      m.side = THREE.DoubleSide;
      if (isPrintedPart(m)) m.color.copy(PRINTED_WHITE);
      m.needsUpdate = true;
    }
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
  const dist = (radius / Math.sin(fov / 2)) * 0.95;

  // 默认机位：斜前上方，接近观众站在实机前面的角度
  const dir = new THREE.Vector3(1.0, 0.42, 1.15).normalize();
  camera.position.copy(center).addScaledVector(dir, dist);
  camera.near = Math.max(dist / 500, 0.0005);
  camera.far = dist * 200;
  camera.updateProjectionMatrix();
  camera.lookAt(center);

  return { box, size, center, radius, dist };
}
