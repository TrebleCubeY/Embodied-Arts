import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

// 薄封装。将来要锁死视角（比如固定演示机位）时，把 enableRotate 关掉就行。
export function attachControls(camera, dom, target) {
  const controls = new OrbitControls(camera, dom);
  controls.enableDamping = true;
  controls.dampingFactor = 0.09;
  controls.rotateSpeed = 0.8;
  controls.zoomSpeed = 0.9;
  controls.target.copy(target);
  controls.update();
  return controls;
}
