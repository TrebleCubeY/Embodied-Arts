#!/usr/bin/env node
// 在无头浏览器里对着活页面跑一段 JS，把结果打印出来（可选截图）。
// 用来做数值化的现场勘查，比截图猜要快。
//
//   node twin/tools/probe.mjs --eval "return window.__TWIN__.jointNames"
//   node twin/tools/probe.mjs --file .workbuddy/tmp/q.js --out .workbuddy/tmp/a.png
//   node twin/tools/probe.mjs --pose "gripper=1.74533" --zoom 5 --out .workbuddy/tmp/b.png
//
// --eval 里的代码会被包进 async 函数体，所以可以直接 return、也可以用 await。
// 需要页面先起服务：$PY twin/tools/serve.py

import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { PROJECT, TWIN, openPage, sleep, wheelZoom } from './cdp.mjs';

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const has = (name) => args.includes(name);

const PAGE = argOf('--url', 'http://127.0.0.1:8000/');
const FILE = argOf('--file', null);
const OUT = argOf('--out', null);
const ZOOM = Number(argOf('--zoom', '0'));
const PORT = Number(argOf('--debug-port', '9344'));
const W = Number(argOf('--width', '1600'));
const H = Number(argOf('--height', '900'));
const POSE = (argOf('--pose', '') || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)
  .map((s) => {
    const [name, value] = s.split('=');
    return { name: name?.trim(), value: Number(value) };
  })
  .filter((p) => p.name && Number.isFinite(p.value));

const exprArg = argOf('--eval', null);
const code = FILE ? readFileSync(resolve(PROJECT, FILE), 'utf8') : exprArg;

// 只截图不跑代码也是合法用法：--pose ... --out x.png
if (!code && !OUT) {
  console.error('给一段代码：--eval "return ..." 或 --file path.js；或者至少给 --out 截图');
  process.exit(2);
}

const pageUrl = new URL(PAGE);
pageUrl.searchParams.set('selftest', '1');

let session = null;
try {
  session = await openPage({ url: pageUrl.href, port: PORT, width: W, height: H });
  const { cdp } = session;

  const state = await cdp.readHook('window.__TWIN__');
  if (!state?.ready) {
    console.error(`页面没就绪：${state?.error ?? '未知'}`);
    process.exit(2);
  }

  // 摆姿态：先归零再逐个设，避免受上一状态影响
  if (POSE.length) {
    await cdp.evaluate('window.__TWIN_SET_ALL__(0)');
    for (const p of POSE) {
      await cdp.evaluate(
        `window.__TWIN_SET_JOINT__(${JSON.stringify(p.name)}, ${p.value})`,
      );
    }
    await sleep(250);
  }

  if (ZOOM) {
    await wheelZoom(cdp, { x: W * 0.42, y: H * 0.5, steps: ZOOM });
    await sleep(400);
  }

  const result = code ? await cdp.evaluate(code, { async: true }) : null;
  if (code) console.log(JSON.stringify(result, null, 2));

  if (OUT) {
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: true });
    writeFileSync(resolve(PROJECT, OUT), Buffer.from(shot.data, 'base64'));
    console.log(`截图 ${resolve(PROJECT, OUT)}`);
  }

  const errs = cdp.pageErrors();
  if (errs.length) console.error(`页内错误：${errs.slice(0, 4).join(' | ')}`);
} catch (err) {
  console.error(`probe 中断：${err.message}`);
  if (err.chromeStderr) console.error(err.chromeStderr.split('\n').slice(-5).join('\n'));
  process.exitCode = 1;
} finally {
  session?.close();
  await sleep(500);
}
