// 状态帧契约。与 twin/server/protocol.py 是一对，改一边必须改另一边。
//
// 布局（小端，共 68 字节）：
//
//   偏移   类型        字段
//   0      float64     t        后端单调时钟，秒
//   8      uint32      seq      递增序号
//   12     float32[6]  actual   度
//   36     float32[6]  target   度
//   60     uint8       mode     0 跟随 / 1 反向 / 2 无关运动
//   61     uint8       blend    0-255
//   62     2 字节      —        填充
//   64     float32     grip     0-100，仅供显示
//
// 注意 actual[5] 和 grip 是同一个夹爪的两种编码：**actual[5] 才是渲染权威**，
// grip 是派生值、只给界面显示用。别拿 grip 去驱动 gripper 关节——
// 实机的电机零位还没核对过，那个换算关系还不能当准。

export const JOINT_ORDER = [
  'shoulder_pan',
  'shoulder_lift',
  'elbow_flex',
  'wrist_flex',
  'wrist_roll',
  'gripper',
];

// 面板上给一句中文提示，免得对着一串英文关节名要想一下
export const JOINT_HINT = {
  shoulder_pan: '底座回转',
  shoulder_lift: '肩部抬落',
  elbow_flex: '肘部弯伸',
  wrist_flex: '腕部俯仰',
  wrist_roll: '腕部自转',
  gripper: '夹爪开合',
};

export const FRAME_SIZE = 68;

export const MODE = { FOLLOW: 0, MIRROR: 1, IDLE: 2 };
export const MODE_NAMES = ['跟随', '反向', '无关运动'];
export const BLEND_DONE = 255;

// 六个关节的限位（度），与 twin/server/config.py 的 JOINT_LIMITS_DEG 一一对应。
// code 视图不加载 URDF（也就不建 3D），拿不到 urdf-loader 解析出来的限位，
// 所以这里得有一份 —— 改 config.py 时同步改这里。
export const JOINT_LIMITS_DEG = {
  shoulder_pan: [-110.0, 110.0],
  shoulder_lift: [-100.0, 100.0],
  elbow_flex: [-96.8, 96.8],
  wrist_flex: [-95.0, 95.0],
  wrist_roll: [-157.2, 162.8],
  gripper: [-10.0, 100.0],
};

/** 驱动档。三档与桥那边的字符串一致，别改。 */
export const DRIVE_MODES = ['teleop', 'keyframes', 'hold'];
export const DRIVE_LABELS = {
  teleop: '遥操跟随',
  keyframes: '自动 · 预设',
  hold: '停住',
};
export const DRIVE_HINT = {
  teleop: '人握着 leader，follower 跟着动，屏幕跟着 follower',
  keyframes: '桥按 twin/server/motions 里的关键帧回放',
  hold: '不写指令，舵机自己保持当前姿态',
};

export const DEG2RAD = Math.PI / 180;
export const RAD2DEG = 180 / Math.PI;

export function decodeStateFrame(buf) {
  // 长度校验挡两件事：事件 JSON 混进二进制分支，以及后端改版没同步前端
  if (!(buf instanceof ArrayBuffer) || buf.byteLength !== FRAME_SIZE) return null;
  const dv = new DataView(buf);
  const actual = new Array(6);
  const target = new Array(6);
  for (let i = 0; i < 6; i += 1) {
    actual[i] = dv.getFloat32(12 + i * 4, true);
    target[i] = dv.getFloat32(36 + i * 4, true);
  }
  return {
    t: dv.getFloat64(0, true),
    seq: dv.getUint32(8, true),
    actual,
    target,
    mode: dv.getUint8(60),
    blend: dv.getUint8(61),
    grip: dv.getFloat32(64, true),
  };
}

// 状态帧地址：默认跟页面同一个主机、端口 8001（后端 main.py 的 WS listener）。
// 用 ?ws=ws://.../state 覆盖；?ws=0 表示不连后端，只看静态模型。
export function defaultStateUrl() {
  const override = new URLSearchParams(location.search).get('ws');
  if (override === '0') return null;
  if (override) return override;
  const host = location.hostname || '127.0.0.1';
  return `ws://${host}:8001/state`;
}
