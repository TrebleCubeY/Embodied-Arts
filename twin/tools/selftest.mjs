#!/usr/bin/env node
// 无头 Chrome 自检，零依赖（Node 22 自带 WebSocket 与 fetch）。
//
//   node twin/tools/selftest.mjs
//   node twin/tools/selftest.mjs --url http://127.0.0.1:8100/ --out twin/selfcheck.png
//
// 判定的是可量化的东西，不是靠肉眼看截图：
//   meshCount === 13 · 包围盒非退化 · 非背景像素占比 > 2% · 摆一个关节后像素确实变了
// 最后仍会存一张 PNG，方便人直接看一眼。
//
// WebGL 起不来时退到结构级验证（关节名、网格数、页内错误），
// 并在结论里写明「结构通过、像素未验证」，不把环境问题说成代码问题。

import { writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

import { TWIN, openPage, sleep, waitFor } from './cdp.mjs';

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const has = (name) => args.includes(name);

const PAGE = argOf('--url', 'http://127.0.0.1:8000/');
const OUT = resolve(argOf('--out', join(TWIN, 'selfcheck.png')));
const DEBUG_PORT = Number(argOf('--debug-port', '9333'));
const READY_TIMEOUT = Number(argOf('--timeout', '45000'));
// 摆一个指定姿态再截图，用来肉眼核对关节方向，例如：
//   --pose "shoulder_lift=0.6,elbow_flex=1.2,wrist_flex=0.5"（单位弧度）
const POSE = (argOf('--pose', '') || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)
  .map((s) => {
    const [name, value] = s.split('=');
    return { name: name?.trim(), value: Number(value) };
  })
  .filter((p) => p.name && Number.isFinite(p.value));
const W = 1600;
const H = 900;

// 状态流那几项要后端在跑。--no-stream 用来只跑静态检查（纯前端开发时）。
const NO_STREAM = has('--no-stream');
const WS_URL = argOf('--ws-url', 'ws://127.0.0.1:8001/state');

const results = [];
const check = (ok, label, detail) => {
  results.push({ ok, label, detail });
  console.log(`  ${ok ? '通过' : '不通过'}  ${label}${detail ? `  — ${detail}` : ''}`);
};

/** 在页面里轮询方块状态，等条件成立或超时。
 *  无头 SwiftShader 下渲染帧率只有几帧每秒，按固定时间 sleep 根本等不到状态变化。 */
async function waitBlock(cdp, cond, ms = 5000) {
  return cdp.evaluate(
    `
    const read = () => {
      const b = window.__TWIN__.block;
      return b ? { held: b.held, pos: [...b.pos], openingMm: b.openingMm } : null;
    };
    const t0 = performance.now();
    for (;;) {
      const b = read();
      if (b && (${cond})) return { ok: true, b };
      if (performance.now() - t0 > ${ms}) return { ok: false, b };
      await new Promise((r) => requestAnimationFrame(r));
    }
  `,
    { async: true },
  );
}

/** 点页面上的真实按钮（走真实的事件处理，不是调内部钩子） */
async function clickButton(cdp, text) {
  const ok = await cdp.evaluate(
    `
    const b = [...document.querySelectorAll('#panel button')].find((x) => x.textContent.trim() === ${JSON.stringify(text)});
    if (!b || b.disabled) return false;
    b.click();
    return true;
  `,
    { async: true },
  );
  if (!ok) throw new Error(`点不到按钮「${text}」（找不到或已禁用）`);
}

/** 在一段时间窗里采页面状态：帧数、各关节变化范围、末端轨迹长度、最大帧间隔 */
async function probeWindow(cdp, ms) {
  return cdp.evaluate(
    `
    const w = window.__TWIN__;
    const t0 = performance.now();
    const frames0 = w.link.framesReceived;
    const span = w.twin.appliedDeg.map((v) => [v, v]);
    const tspan = w.twin.targetDeg.map((v) => [v, v]);
    const seqs = [];
    const ee = [];
    let stale = 0;
    while (performance.now() - t0 < ${ms}) {
      await new Promise((r) => requestAnimationFrame(r));
      const a = w.twin.appliedDeg;
      const t = w.twin.targetDeg;
      for (let i = 0; i < a.length; i += 1) {
        if (a[i] < span[i][0]) span[i][0] = a[i];
        if (a[i] > span[i][1]) span[i][1] = a[i];
        if (t[i] < tspan[i][0]) tspan[i][0] = t[i];
        if (t[i] > tspan[i][1]) tspan[i][1] = t[i];
      }
      ee.push(window.__TWIN_EE__());
      if (w.link.lastSeq != null && seqs[seqs.length - 1] !== w.link.lastSeq) seqs.push(w.link.lastSeq);
      if (w.link.lastFrameAt) stale = Math.max(stale, performance.now() - w.link.lastFrameAt);
    }
    let path = 0;
    for (let i = 1; i < ee.length; i += 1) {
      if (!ee[i] || !ee[i - 1]) continue;
      path += Math.hypot(ee[i][0] - ee[i - 1][0], ee[i][1] - ee[i - 1][1], ee[i][2] - ee[i - 1][2]);
    }
    return {
      ms: performance.now() - t0,
      frames: w.link.framesReceived - frames0,
      framesDropped: w.link.framesDropped,
      span: span.map(([lo, hi]) => +(hi - lo).toFixed(4)),
      targetSpan: tspan.map(([lo, hi]) => +(hi - lo).toFixed(4)),
      seqs,
      // 缺号是页面里逐帧数的；外面按时间采样采不全（无头下渲染帧率可能只有几帧每秒）
      seqGaps: w.link.seqGaps,
      eePath: +path.toFixed(5),
      staleMaxMs: +stale.toFixed(0),
      samples: ee.length,
      paused: w.paused,
      viewState: w.viewState,
      linkState: w.link.state,
      bootId: w.link.bootId,
      applied: w.twin.appliedDeg.map((v) => +v.toFixed(3)),
      target: w.twin.targetDeg.map((v) => +v.toFixed(3)),
    };
  `,
    { async: true },
  );
}

const maxOf = (arr) => arr.reduce((m, v) => Math.max(m, v), 0);

/** 后端的状态帧端口通不通（不连带建连接，只探一下） */
async function probeWs(url, timeout = 2500) {
  return new Promise((resolve) => {
    let ws;
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      try {
        ws?.close();
      } catch {
        /* 没连上就没什么可关的 */
      }
      resolve(ok);
    };
    try {
      ws = new WebSocket(url);
    } catch {
      finish(false);
      return;
    }
    setTimeout(() => finish(false), timeout);
    ws.onopen = () => finish(true);
    ws.onerror = () => finish(false);
  });
}

async function main() {
  console.log(`数字层自检  ${PAGE}\n`);

  // 0. 服务在不在
  try {
    const res = await fetch(PAGE, { method: 'GET' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    console.error(`目标页面打不开（${err.message}）`);
    console.error(`先起服务：D:/conda_envs/so101-twin/Scripts/python.exe twin/server/main.py`);
    console.error(`只想看模型不连后端的话，用 twin/tools/serve.py 起静态服务，并加 --no-stream`);
    return 2;
  }

  // 状态流那几项要后端在跑。环境问题和代码问题的结论要分开，所以这里单列一条。
  if (!NO_STREAM && !(await probeWs(WS_URL))) {
    console.error(`后端的状态帧端口连不上：${WS_URL}`);
    console.error(`先起后端：D:/conda_envs/so101-twin/Scripts/python.exe twin/server/main.py`);
    console.error(`只跑静态检查就加 --no-stream`);
    return 2;
  }

  let session = null;
  let cdp = null;
  let exitCode = 1;

  try {
    const pageUrl = new URL(PAGE);
    pageUrl.searchParams.set('selftest', '1');

    session = await openPage({
      url: pageUrl.href,
      port: DEBUG_PORT,
      width: W,
      height: H,
      timeout: READY_TIMEOUT,
    });
    cdp = session.cdp;
    console.log(`  浏览器 ${session.version.Browser}\n`);

    // 等模型就绪（超时也别直接放弃，后面按降级路径给结论）
    let state = null;
    let readyTimedOut = false;
    try {
      state = await waitFor(
        async () => {
          const s = await cdp.evaluate('JSON.stringify(window.__TWIN__ || null)');
          const o = s ? JSON.parse(s) : null;
          return o && (o.ready || o.error || o.webglError) ? o : null;
        },
        { timeout: READY_TIMEOUT, interval: 300, label: '页面报出就绪状态' },
      );
    } catch {
      readyTimedOut = true;
      const s = await cdp.evaluate('JSON.stringify(window.__TWIN__ || null)').catch(() => null);
      state = s ? JSON.parse(s) : null;
    }

    console.log('判定');
    const webglDown = !state?.ready;

    if (webglDown) {
      check(false, 'WebGL / 模型就绪', state?.error ?? (readyTimedOut ? '超时' : '未知'));
      const names = await cdp
        .evaluate('JSON.stringify(window.__TWIN__?.jointNames || [])')
        .catch(() => '[]');
      check(false, '像素级验证', `已跳过（结构级降级）。关节名读到：${names}`);
    } else {
      // 拿 URDF 现算期望值，不写死数字：一个网格文件会被多个 link 引用
      // （visual 那 17 处只用到 13 个文件），写死容易把对的实现判成错的
      const urdfUrl = new URL('./models/so101/so101_new_calib.urdf', PAGE);
      const urdf = await (await fetch(urdfUrl)).text();
      const expectMeshes = (urdf.match(/<visual>/g) ?? []).length;
      const meshFiles = new Set(
        [...urdf.matchAll(/filename="([^"]+)"/g)].map((m) => m[1].split('/').pop()),
      );

      check(
        state.meshCount === expectMeshes,
        `网格实例数与 URDF 一致（${expectMeshes}）`,
        `实测 ${state.meshCount}`,
      );
      check(meshFiles.size === 13, 'URDF 引用 13 个网格文件', `实测 ${meshFiles.size}`);

      const stlAll = cdp.responses().filter((r) => r.url.endsWith('.stl'));
      const stlOk = new Set(stlAll.filter((r) => r.status === 200).map((r) => r.url.split('/').pop()));
      check(stlOk.size === meshFiles.size, '13 个网格全部取到', `实测 ${stlOk.size}`);
      check(
        stlAll.length === meshFiles.size,
        '每个网格文件只请求一次',
        `${stlAll.length} 次请求 / ${new Set(stlAll.map((r) => r.url)).size} 个文件`,
      );

      const sizeOk = state.bbox.size.every((v) => v > 0.001);
      check(sizeOk, '包围盒非退化', `size = [${state.bbox.size.join(', ')}]`);
      check(state.jointNames.length === 6, '受驱动关节 6 个', state.jointNames.join(', '));

      // ——— 状态流：帧率 / 序号 / 量纲 / 真的在动 / 暂停真的生效 / 接管真的接管
      // 放在像素检查之前做：下面几条静态检查会直接写关节值，一写就切到接管态，
      // 之后再验状态流就不准了。
      if (NO_STREAM) {
        console.log('  跳过状态流检查（--no-stream）');
      } else {
        await cdp.evaluate('window.__TWIN_RESET_STATS__()');
        const moving = await probeWindow(cdp, 3000);
        check(moving.linkState === 'open', '状态帧已连上', `${moving.linkState} · 启动标识 ${moving.bootId ?? '(没报)'}`);
        const hz = (moving.frames / moving.ms) * 1000;
        check(
          hz >= 25 && hz <= 35,
          '帧率在 25–35 Hz',
          `${hz.toFixed(1)} Hz（${moving.frames} 帧 / ${moving.ms.toFixed(0)} ms）`,
        );
        check(moving.staleMaxMs < 150, '最大帧间隔 < 150 ms', `${moving.staleMaxMs} ms`);
        const gaps = moving.seqGaps ?? 0;
        check(gaps === 0, 'seq 零缺号', `窗口内收到 ${moving.frames} 帧，缺号 ${gaps} 个`);

        // 漏转弧度的话手臂会离谱地放大 57 倍，这条一眼抓得出来
        const unitErr = Math.max(...moving.target.map((t, i) => Math.abs(t - moving.applied[i])));
        check(unitErr < 15, 'applied 与 target 同量级（没漏转弧度）', `最大差 ${unitErr.toFixed(2)}°`);

        check(maxOf(moving.span) > 5, '关节确实在动', `最大关节变化 ${maxOf(moving.span).toFixed(2)}°`);
        check(moving.eePath > 0.01, '末端有位移', `轨迹 ${(moving.eePath * 1000).toFixed(1)} mm`);

        // 暂停：点真实按钮，走完「按钮 → 上行消息 → 后端冻结时钟 → 帧里姿态不动」整条路
        await clickButton(cdp, '暂停');
        await sleep(700); // λ=15 的时间常数约 67ms，等它收敛到位再测
        await cdp.evaluate('window.__TWIN_RESET_STATS__()');
        const idle = await probeWindow(cdp, 1800);
        check(idle.paused === true, '后端确认已暂停', `paused = ${idle.paused}`);
        check(
          idle.linkState === 'open' && idle.frames > 0,
          '暂停不等于断线（连接还在、帧还在来）',
          `${idle.linkState} · ${idle.frames} 帧`,
        );
        check(idle.span[0] < 0.05 && maxOf(idle.span) < 0.05, '暂停后关节纹丝不动', `最大变化 ${maxOf(idle.span).toFixed(4)}°`);
        check(idle.eePath < 0.001, '暂停后末端不动', `${(idle.eePath * 1000).toFixed(2)} mm`);
        // 阈值自校准：不写死毫米数，要求运动比静息抖动大一个数量级
        check(
          moving.eePath > Math.max(idle.eePath, 1e-5) * 10,
          '运动幅度远大于静息抖动',
          `${(moving.eePath * 1000).toFixed(1)} mm vs ${(idle.eePath * 1000).toFixed(2)} mm`,
        );

        await clickButton(cdp, '继续');
        await sleep(700);
        const resumed = await probeWindow(cdp, 1500);
        check(resumed.paused === false, '后端确认已恢复', `paused = ${resumed.paused}`);
        check(
          resumed.eePath > Math.max(idle.eePath, 1e-5) * 5,
          '恢复后末端重新开始移动',
          `${(resumed.eePath * 1000).toFixed(1)} mm`,
        );

        // 接管：外部写入之后，渲染循环不该再覆盖它
        await cdp.evaluate('window.__TWIN_SET_JOINT__("elbow_flex", 0.9)');
        await sleep(500);
        // 窗口要够长：动作里每个关键帧之间都有 0.2~0.4s 的静止段，
        // 900ms 的窗口正好可能整段落在里面，于是"target 没在推进"被误报。
        const held = await probeWindow(cdp, 2600);
        check(held.viewState === 'OVERRIDE', '外部写入把前端切到接管', held.viewState);
        check(
          Math.abs(held.applied[2] - 51.57) < 3,
          '接管后关节停在写入的值',
          `elbow_flex = ${held.applied[2].toFixed(2)}°（写入 0.9 rad ≈ 51.57°）`,
        );
        check(held.span[2] < 0.05, '接管期间这个关节不被改写', `变化 ${held.span[2].toFixed(4)}°`);
        // 看全部受驱动关节的最大变化。只盯 elbow 的话，恰好赶上动作里那段 hold 就会误报
        const targetMove = maxOf(held.targetSpan);
        check(
          targetMove > 1,
          '接管期间状态帧仍在推进（只是不再驱动）',
          `target 最大变化 ${targetMove.toFixed(1)}°`,
        );

        // 交还状态帧，后面几条静态检查要有干净起点
        await clickButton(cdp, '回到跟随');
        await sleep(300);
      }

      const before = await cdp.evaluate('window.__TWIN_SAMPLE__()');
      check(before > 0.02, '非背景像素占比 > 2%', `${(before * 100).toFixed(1)}%`);
      console.log(`  渲染器  ${state.webgl.renderer}`);

      // 摆一个关节：这一步才分得开「真在跑运动学」和「只贴了一张静态图」
      await cdp.evaluate('window.__TWIN_SET_JOINT__("elbow_flex", 0.6)');
      await cdp.evaluate('window.__TWIN_SET_JOINT__("shoulder_pan", 0.9)');
      await sleep(400);
      const after = await cdp.evaluate('window.__TWIN_SAMPLE__()');
      const changed = Math.abs(after - before) > 0.001;
      check(changed, '摆关节后画面确实变了', `${(before * 100).toFixed(1)}% → ${(after * 100).toFixed(1)}%`);

      // 关节轴向：不靠看截图，直接量末端位置随单个关节怎么变。
      // 这三条能同时验掉「关节接错 link」和「Z-up 校正漏了」
      const setAll = (v) => cdp.evaluate(`window.__TWIN_SET_ALL__(${v})`);
      const setOne = (n, v) =>
        cdp.evaluate(`window.__TWIN_SET_JOINT__(${JSON.stringify(n)}, ${v})`);
      const ee = async () => JSON.parse(await cdp.evaluate('JSON.stringify(window.__TWIN_EE__())'));
      const horiz = (p) => Math.hypot(p[0], p[2]); // 到底座竖直轴的水平半径
      const deg = (r) => (r * 180) / Math.PI;

      await setAll(0);
      const p0 = await ee();

      await setOne('shoulder_pan', 0.6);
      const pPan = await ee();
      check(
        Math.abs(deg(Math.atan2(pPan[2], pPan[0]) - Math.atan2(p0[2], p0[0]))) > 15 &&
          Math.abs(pPan[1] - p0[1]) < 0.005,
        'shoulder_pan 绕竖直轴转、不改高度',
        `水平角差 ${(deg(Math.atan2(pPan[2], pPan[0]) - Math.atan2(p0[2], p0[0]))).toFixed(1)}°，高度差 ${(pPan[1] - p0[1]).toFixed(4)} m`,
      );

      await setAll(0);
      await setOne('shoulder_lift', 0.6);
      const pLift = await ee();
      check(
        Math.abs(pLift[1] - p0[1]) > 0.02,
        'shoulder_lift 改变末端高度',
        `Δy = ${(pLift[1] - p0[1]).toFixed(4)} m`,
      );

      await setAll(0);
      await setOne('elbow_flex', 0.6);
      const pElbow = await ee();
      check(
        Math.abs(horiz(pElbow) - horiz(p0)) > 0.02,
        'elbow_flex 改变末端水平半径',
        `r ${horiz(p0).toFixed(4)} → ${horiz(pElbow).toFixed(4)} m`,
      );

      // 方块：它的位置和抓取姿态是一对，改一边不改另一边就夹不起来。
      // 所以这几条同时守着 RED_BLOCK.position、pick_red_block.json 的抓取姿态、
      // 以及 block.js 里那个咬合点偏移。
      //
      // 两个写法上的坑，踩过：
      //  - 关节必须一次 evaluate 设完。分几次设的话中间会插进渲染帧，夹爪可能在
      //    「臂已到位、爪还没张开」的那一帧误吸附（开口 5mm 就抓住了方块）。
      //  - 等待条件要能证明读到的快照是新的。无条件地"等一帧"会读到上一帧的旧值 ——
      //    曾经因此把开口读成 5.1mm、把方块位置读成动作循环举起它时的高度。
      const RAD = Math.PI / 180;
      const poseAt = (lift, elbow, grip) => `
        window.__TWIN_SET_JOINT__('shoulder_lift', ${lift * RAD});
        window.__TWIN_SET_JOINT__('elbow_flex', ${elbow * RAD});
        window.__TWIN_SET_JOINT__('wrist_roll', ${-60 * RAD});
        window.__TWIN_SET_JOINT__('gripper', ${grip * RAD});
      `;

      // 摆到抓取姿态、张着爪。reset 和摆姿态放在同一次求值里，中间不留帧
      await cdp.evaluate(`
        window.__TWIN_RESET_BLOCK__();
        window.__TWIN_SET_ALL__(0);
        ${poseAt(28, 12.5, 90)}
      `);
      let blk = await waitBlock(cdp, 'b.openingMm > 45 && !b.held', 6000);
      check(
        blk.ok && Math.abs(blk.b.pos[1] - 0.0125) < 0.005,
        '张着爪伸到方块上时不误夹',
        `开口 ${blk.b?.openingMm}mm，方块还在地上 y = ${blk.b?.pos[1]}`,
      );

      await cdp.evaluate(`window.__TWIN_SET_JOINT__('gripper', ${15 * RAD})`);
      blk = await waitBlock(cdp, 'b.held && b.openingMm < 30', 6000);
      check(blk.ok, '合拢后夹住方块', `held = ${blk.b?.held}，开口 ${blk.b?.openingMm}mm`);

      // 减小 shoulder_lift + 收肘把方块提起来
      await cdp.evaluate(`
        window.__TWIN_SET_JOINT__('shoulder_lift', ${8 * RAD});
        window.__TWIN_SET_JOINT__('elbow_flex', ${26 * RAD});
      `);
      blk = await waitBlock(cdp, 'b.held && b.pos[1] > 0.05', 6000);
      check(blk.ok, '举起时方块跟着离地', `方块 y = ${blk.b?.pos[1]}（贴地时是 0.0125）`);

      await cdp.evaluate(`
        window.__TWIN_SET_JOINT__('shoulder_lift', ${28 * RAD});
        window.__TWIN_SET_JOINT__('elbow_flex', ${12.5 * RAD});
        window.__TWIN_SET_JOINT__('gripper', ${90 * RAD});
      `);
      blk = await waitBlock(cdp, '!b.held && b.pos[1] < 0.02', 6000);
      check(blk.ok, '松口后方块落回地面', `held = ${blk.b?.held}，方块 y = ${blk.b?.pos[1]}`);

      // 收尾：给了 --pose 就摆成那个姿态，否则复位到 URDF 零位再截图
      if (POSE.length) {
        await setAll(0);
        for (const p of POSE) await setOne(p.name, p.value);
      } else {
        await setAll(0);
      }
      await sleep(300);
    }

    const shot = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true });
    writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
    console.log(`\n截图  ${OUT}`);

    const failures = cdp.failedRequests();
    const lost = failures.filter((f) => f.lost);
    const late = failures.filter((f) => !f.lost);
    if (late.length) {
      console.log(
        `\n收尾时被中断 ${late.length} 条（响应已经收到，不计入失败）：${[...new Set(late.map((f) => f.url))].join('; ')}`,
      );
    }
    if (lost.length) {
      console.log(`\n没拿到数据 ${lost.length} 条：${[...new Set(lost.map((f) => f.text))].join('; ')}`);
    }

    const pageErrs = [...(state?.errors ?? []), ...cdp.pageErrors()];
    if (pageErrs.length) console.log(`页内错误 ${pageErrs.length} 条：${pageErrs.slice(0, 4).join(' | ')}`);

    const bad = results.filter((r) => !r.ok);
    const total = bad.length + lost.length + pageErrs.length;
    exitCode = total ? 1 : 0;
    console.log(`\n${exitCode === 0 ? '自检通过' : `有问题：${total} 处`}`);
  } catch (err) {
    console.error(`\n自检中断：${err.message}`);
    if (err.chromeStderr) console.error(err.chromeStderr.split('\n').slice(-6).join('\n'));
    exitCode = 1;
  } finally {
    session?.close();
    await sleep(400);
  }

  return exitCode;
}

process.exitCode = await main();
