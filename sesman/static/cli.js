'use strict';

/**
 * 前端 CLI 行为的公共基类。
 *
 * app.js 只保存和渲染乐观消息；某条原生记录是否能结束排队、旧状态如何
 * 迁移、特殊键是否取消队列，都由具体 CLI 实现决定。
 */
class SesmanCli {
  constructor(source, name, icon, color) {
    this.source = source;
    this.name = name;
    this.icon = icon;
    this.color = color;
  }

  migrateQueuedMessages(items, _fromVersion, _toVersion) {
    return Array.isArray(items) ? items : [];
  }

  queueResolution(message) {
    return ['user', 'command'].includes(message?.role)
      ? String(message.text || '') : null;
  }

  clearsQueuedMessages(_keys) {
    return false;
  }

  repeatedEscape(_now, _previousAt) {
    return { rewind: false, nextAt: -Infinity };
  }
}

class ClaudeCli extends SesmanCli {
  constructor() {
    super('claude', 'Claude', 'i-claude', 'var(--claude)');
  }

  queueResolution(message) {
    const normal = super.queueResolution(message);
    if (normal !== null) return normal;
    return message?.role === 'queue_operation' && message.operation === 'remove'
      ? String(message.text || '') : null;
  }

  clearsQueuedMessages(keys) {
    // Claude 的真实记录在 Esc 后会产生 queue-operation remove；先在 UI
    // 撤掉副本，后续原生事件仍作为最终对账依据。
    return Array.isArray(keys) && keys.includes('Escape');
  }

  repeatedEscape(now, previousAt) {
    const rewind = now - previousAt <= 650;
    return { rewind, nextAt: rewind ? -Infinity : now };
  }
}

class CodexCli extends SesmanCli {
  constructor() {
    super('codex', 'Codex', 'i-codex', 'var(--codex)');
  }

  migrateQueuedMessages(items, fromVersion, _toVersion) {
    // 旧版没有记录 Esc 取消，Codex 又没有持久化的 queue remove 事件；
    // 这些 UI 副本已无法判定真伪，只在 v1 -> v2 时清理一次。
    return fromVersion < 2 ? [] : super.migrateQueuedMessages(items);
  }

  clearsQueuedMessages(keys) {
    return Array.isArray(keys) && keys.includes('Escape');
  }
}

class GrokCli extends SesmanCli {
  constructor() {
    super('grok', 'Grok', 'i-grok', 'var(--grok)');
  }

  // Grok 暂无已验证的内存队列控制事件：只使用基类的正式消息对账，
  // 不把 Claude/Codex 的 Esc 假设套过来。
  queueResolution(message) {
    return super.queueResolution(message);
  }
}

const SESMAN_CLIS = Object.freeze({
  claude: new ClaudeCli(),
  codex: new CodexCli(),
  grok: new GrokCli(),
});

function sesmanCli(sourceOrUid) {
  const source = String(sourceOrUid || '').split(':', 1)[0];
  return SESMAN_CLIS[source] || null;
}

// 供 app.js、term.js 以及 headless 回归共同使用。
Object.assign(globalThis, {
  SesmanCli, ClaudeCli, CodexCli, GrokCli, SESMAN_CLIS, sesmanCli,
});
