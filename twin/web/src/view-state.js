// 「谁写关节值」的唯一裁决处。
//
// 这里解决的是两件**正交**的事，别揉在一起：
//
//   - 暂停 = 控后端（冻结播放时钟）。前端只反映后端回传的真实状态，不做本地乐观勾选。
//   - 接管 = 控本地写权限。不依赖网络，后端断了照样能手动摆。
//
// 三种状态：
//
//   FOLLOW    渲染循环写：对 target 做 λ=15 指数插值。面板滑块只读，仅刷新显示。
//   OVERRIDE  面板写；渲染循环一个关节都不写。
//   FROZEN    断流，谁都不写，保持最后一帧姿态（绝不外推、绝不回零位，技术方案第 8 节）。
//
// 接管做成**粘性**的：松开滑块也停在原地，直到面板上那个「回到跟随」被按下。
// 理由有三条：像抓住它一样符合直觉；多关节依次拖时松开的那个不会往回跑；
// 状态机不需要引用计数。代价是可能把演示留在接管态，用显眼的按钮退出，不用隐形定时器。

export const FOLLOW = 'FOLLOW';
export const OVERRIDE = 'OVERRIDE';
export const FROZEN = 'FROZEN';

export const VIEW_STATE_LABEL = {
  [FOLLOW]: '跟随状态帧',
  [OVERRIDE]: '手动接管',
  [FROZEN]: '断流 · 冻结最后一帧',
};

export function createViewState({ onChange } = {}) {
  let state = FOLLOW;

  const set = (next, why) => {
    if (state === next) return;
    const prev = state;
    state = next;
    onChange?.(state, prev, why);
  };

  return {
    get state() {
      return state;
    },
    takeOver: (why = 'panel') => set(OVERRIDE, why),
    release: (why = 'panel') => set(FOLLOW, why),
    // 断流时如果已经在接管，就保持接管：手动摆不该因为网络断了而失效
    freeze: (why = 'link') => {
      if (state !== OVERRIDE) set(FROZEN, why);
    },
    thaw: (why = 'link') => {
      if (state === FROZEN) set(FOLLOW, why);
    },
  };
}
