// 状态帧与事件的客户端。
//
// 二进制消息是状态帧，文本消息是事件，同一条 socket —— 所以重连逻辑只有一份。
// 重连用指数退避；重连后按 t 丢弃比本地已处理时间更旧的帧，否则数字层会回跳一次
// （技术方案第 2.3 节，这条在真的会断线的场合比在实验室重要得多）。

import { decodeStateFrame } from './frame.js';

const BACKOFF_MS = [300, 600, 1200, 2400, 4800, 8000];

export function createLink({ url, onFrame, onEvent, onState } = {}) {
  const stats = {
    state: 'idle', // idle | connecting | open | reconnecting | stopped
    bootId: null,
    hz: null,
    frameSize: null,
    framesReceived: 0,
    framesApplied: 0,
    framesDropped: 0,
    // 缺号只能在这里逐帧数：外面按时间采样是采不全的（无头下渲染帧率可能只有几帧每秒）
    seqGaps: 0,
    lastSeq: null,
    lastT: null,
    lastFrameAt: null,
    maxGapMs: 0,
    paused: false,
    error: null,
  };

  let ws = null;
  let attempt = 0;
  let stopped = false;
  let lastT = -1;

  const setState = (next) => {
    if (stats.state === next) return;
    stats.state = next;
    onState?.(stats);
  };

  function schedule() {
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt += 1;
    setState('reconnecting');
    setTimeout(connect, delay);
  }

  function connect() {
    if (stopped) return;
    setState(attempt === 0 ? 'connecting' : 'reconnecting');

    ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer'; // 默认是 blob，不设的话拿到的是 Blob，读不了字节

    ws.onopen = () => {
      attempt = 0;
      stats.error = null;
      setState('open');
      // 连上时上报期望状态（默认 running）。不这么做，"暂停之后刷新页面"
      // 会看起来像坏了 —— 后端还是暂停的，页面却以为在正常播。
      send({ type: 'hello', expect: 'running' });
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        handleEvent(ev.data);
        return;
      }
      handleFrame(ev.data);
    };

    ws.onclose = () => {
      if (!stopped) schedule();
    };

    ws.onerror = () => {
      // onclose 会跟着来，这里不重复处理
    };
  }

  function handleFrame(buf) {
    const frame = decodeStateFrame(buf);
    if (!frame) {
      stats.error = `帧长不对：${buf?.byteLength} 字节`;
      return;
    }
    stats.framesReceived += 1;
    const now = performance.now();
    if (stats.lastFrameAt != null) {
      stats.maxGapMs = Math.max(stats.maxGapMs, now - stats.lastFrameAt);
    }
    stats.lastFrameAt = now;
    // 逐帧数缺号。重连之后序号可能从头开始，那不是缺号，所以只在前一帧也算同一轮回时比
    if (stats.lastSeq != null && frame.seq !== stats.lastSeq + 1) stats.seqGaps += 1;
    stats.lastSeq = frame.seq;

    // 只和上一帧的后端时间比，绝不和 performance.now() 比：
    // 两个时钟没有关联，混着用会误判。
    if (frame.t < lastT) {
      stats.framesDropped += 1;
      return;
    }
    lastT = frame.t;
    stats.lastT = frame.t;
    stats.framesApplied += 1;
    onFrame?.(frame);
  }

  function handleEvent(text) {
    let msg = null;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.type === 'hello') {
      stats.bootId = msg.bootId;
      stats.hz = msg.hz;
      stats.frameSize = msg.frameSize;
      stats.paused = !!msg.paused;
    } else if (msg.type === 'state') {
      stats.paused = !!msg.paused;
    }
    onEvent?.(msg);
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  function resetStats() {
    stats.framesReceived = 0;
    stats.framesApplied = 0;
    stats.framesDropped = 0;
    stats.seqGaps = 0;
    stats.maxGapMs = 0;
    stats.lastFrameAt = null;
  }

  connect();

  return {
    stats,
    send,
    resetStats,
    stop() {
      stopped = true;
      setState('stopped');
      try {
        ws?.close();
      } catch {
        /* 已经断了 */
      }
    },
  };
}
