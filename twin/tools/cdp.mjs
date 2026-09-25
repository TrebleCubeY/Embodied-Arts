// 无头 Chrome + CDP 的共用部分。selftest.mjs 与 probe.mjs 都用它。
// 零依赖：Node 22 自带 WebSocket 与 fetch，不需要装包。
//
// 无头下 WebGL 走软渲染，参数上有一条硬要求：
//   必须加 --enable-unsafe-swiftshader，且不能加 --disable-gpu（那会把软渲染路径一起切掉）。

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const TWIN = resolve(HERE, '..');
export const PROJECT = resolve(TWIN, '..');
// 临时产物（截图、探测脚本、浏览器 profile）落在仓库根的 .tmp/ 下，已 gitignore。
// 不用系统临时目录：跑完能直接找到，也不会散落在各处。
export const TMP_DIR = join(PROJECT, '.tmp');

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean);

export function findChrome() {
  for (const p of CHROME_CANDIDATES) if (existsSync(p)) return p;
  return null;
}

export async function waitFor(fn, { timeout = 20000, interval = 200, label = '条件' } = {}) {
  const t0 = Date.now();
  for (;;) {
    try {
      const v = await fn();
      if (v) return v;
    } catch {
      /* 还没起来，继续等 */
    }
    if (Date.now() - t0 > timeout) throw new Error(`等待超时：${label}`);
    await sleep(interval);
  }
}

export class CDP {
  constructor(ws) {
    this.ws = ws;
    this.seq = 0;
    this.pending = new Map();
    this.events = [];
    ws.onmessage = (e) => this.#onMessage(e.data);
  }

  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = () => rej(new Error(`连不上调试端口 ${wsUrl}`));
    });
    return new CDP(ws);
  }

  #onMessage(data) {
    let msg;
    try {
      msg = JSON.parse(data);
    } catch {
      return;
    }
    if (msg.id != null) {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error) p.reject(new Error(JSON.stringify(msg.error)));
      else p.resolve(msg.result);
    } else {
      this.events.push(msg);
    }
  }

  send(method, params = {}) {
    const id = ++this.seq;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((res, rej) => this.pending.set(id, { resolve: res, reject: rej }));
  }

  async evaluate(expression, { async: useAsync = false } = {}) {
    const wrapped = useAsync ? `(async () => { ${expression} })()` : expression;
    const r = await this.send('Runtime.evaluate', {
      expression: wrapped,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
    }
    return r.result?.value;
  }

  /** 读页面里的一个对象钩子，返回解析后的值或 null */
  async readHook(path) {
    const s = await this.evaluate(`JSON.stringify(${path} ?? null)`);
    return s ? JSON.parse(s) : null;
  }

  clearEvents() {
    this.events.length = 0;
  }

  failedRequests() {
    const urls = new Map();
    const answered = new Set();
    for (const e of this.events) {
      if (e.method === 'Network.requestWillBeSent') {
        urls.set(e.params.requestId, e.params.request.url);
      } else if (e.method === 'Network.responseReceived') {
        answered.add(e.params.requestId);
      }
    }
    return this.events
      .filter((e) => e.method === 'Network.loadingFailed')
      // WebSocket 的关闭也会走 loadingFailed。页面自己连状态帧那条 socket
      // 断开重连是正常现象，不能算成资源加载失败。
      .filter((e) => e.params.type !== 'WebSocket')
      .map((e) => {
        const url = (urls.get(e.params.requestId) ?? '(未知)').replace(/^https?:\/\/[^/]+/, '');
        // 已经收到过响应的，说明数据到了，中断只是连接收尾，不该算失败
        return {
          url,
          type: e.params.type,
          text: `${e.params.type} ${e.params.errorText} ${url}`,
          lost: !answered.has(e.params.requestId),
        };
      });
  }

  responses() {
    return this.events
      .filter((e) => e.method === 'Network.responseReceived')
      .map((e) => ({
        url: e.params.response.url,
        status: e.params.response.status,
        type: e.params.type,
      }));
  }

  pageErrors() {
    const out = [];
    for (const e of this.events) {
      if (e.method === 'Runtime.exceptionThrown') {
        out.push(
          e.params.exceptionDetails?.exception?.description ?? e.params.exceptionDetails?.text,
        );
      } else if (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error') {
        out.push(e.params.args.map((a) => a.value ?? a.description ?? '').join(' '));
      } else if (e.method === 'Log.entryAdded' && e.params.entry.level === 'error') {
        out.push(e.params.entry.text);
      }
    }
    return out.filter(Boolean);
  }
}

/**
 * 起浏览器、开一个标签、连上 CDP、导航到页面、等到页面报出就绪。
 * 返回 { cdp, chromePath, version, close() }。
 */
export async function openPage({
  url,
  port = 9333,
  width = 1600,
  height = 900,
  timeout = 45000,
  readyTimeout = 45000,
} = {}) {
  const chromePath = findChrome();
  if (!chromePath) throw new Error('找不到 Chrome 或 Edge，可以用 CHROME_PATH=... 指定');

  mkdirSync(TMP_DIR, { recursive: true });
  // 浏览器数据目录固定复用一个，不每次新建再删：
  // 既省启动时间，也不用反复申请删除权限。要干净的 profile 就手动删掉那个目录。
  const profileDir = process.env.TWIN_CHROME_PROFILE || join(TMP_DIR, 'chrome-profile');
  mkdirSync(profileDir, { recursive: true });

  const child = spawn(
    chromePath,
    [
      '--headless=new',
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profileDir}`,
      `--window-size=${width},${height}`,
      '--hide-scrollbars',
      '--mute-audio',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-dev-shm-usage',
      '--enable-unsafe-swiftshader',
      '--use-angle=swiftshader',
      'about:blank',
    ],
    { stdio: ['ignore', 'ignore', 'pipe'] },
  );

  const stderr = [];
  child.stderr.on('data', (d) => stderr.push(String(d)));

  const cleanup = () => {
    try {
      child.kill();
    } catch {
      /* 已经退了 */
    }
  };

  try {
    const version = await waitFor(
      async () => {
        const r = await fetch(`http://127.0.0.1:${port}/json/version`);
        return r.ok ? r.json() : null;
      },
      { timeout, label: 'Chrome 的调试端口' },
    );

    let target = null;
    for (const method of ['PUT', 'GET']) {
      try {
        // 开空白页就够了。直接把目标 URL 交给它会让页面加载两次，
        // 第一次会被记成 net::ERR_ABORTED，白添噪声。
        const r = await fetch(`http://127.0.0.1:${port}/json/new?about%3Ablank`, { method });
        if (r.ok) {
          target = await r.json();
          break;
        }
      } catch {
        /* 换下一种方式 */
      }
    }
    if (!target) throw new Error('开不出新的标签页');

    const cdp = await CDP.connect(target.webSocketDebuggerUrl);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Log.enable');
    await cdp.send('Network.enable');
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await cdp.send('Page.navigate', { url });

    if (readyTimeout > 0) {
      await waitFor(
        async () => {
          const o = await cdp.readHook('window.__TWIN__');
          return o && (o.ready || o.error || o.webglError) ? o : null;
        },
        { timeout: readyTimeout, interval: 300, label: '页面报出就绪状态' },
      );
    }

    return { cdp, child, version, stderr, close: cleanup };
  } catch (err) {
    cleanup();
    if (stderr.length) err.chromeStderr = stderr.join('');
    throw err;
  }
}

/** 往视口中心打几档滚轮，用来把镜头推近看局部 */
export async function wheelZoom(cdp, { x, y, steps = 4, delta = -120 } = {}) {
  for (let i = 0; i < Math.abs(steps); i += 1) {
    await cdp.send('Input.dispatchMouseEvent', {
      type: 'mouseWheel',
      x,
      y,
      deltaX: 0,
      deltaY: steps > 0 ? delta : -delta,
      modifiers: 0,
      pointerType: 'mouse',
    });
    await sleep(60);
  }
}
