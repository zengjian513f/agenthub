'use strict';

/**
 * 前端 CLI 行为的公共基类。
 *
 * app.js 只保存和渲染乐观消息；某条原生记录是否能确认/结束排队、
 * 旧状态如何迁移、特殊键是否取消队列，都由具体 CLI 实现决定。
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

  createQueuedMessage(fields) {
    return { ...fields, state: 'queued' };
  }

  queueAction(message) {
    return ['user', 'command'].includes(message?.role)
      ? { type: 'remove', text: String(message.text || '') } : null;
  }

  queuedMessageExpired(_item, _now, _hasNativeHistory) {
    return false;
  }

  queuedMessageLabel(item) {
    return item?.state === 'sending' ? '发送中' : '排队中';
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

  migrateQueuedMessages(items, fromVersion, _toVersion) {
    // v2 只能证明 tmux 粘贴成功，无法区分真实排队和幽灵气泡。
    // 先降级为“发送中”，等该会话的原生历史读入后再对账：真队列
    // 会被 enqueue 重新确认，幽灵副本才会过期。
    if (fromVersion < 3) {
      return super.migrateQueuedMessages(items).map(item => ({
        ...item,
        state: 'sending',
        expiresAt: (+item?.created || 0) + 8000,
        legacy: true,
      }));
    }
    return super.migrateQueuedMessages(items);
  }

  createQueuedMessage(fields) {
    return {
      ...fields,
      state: 'sending',
      // Claude 的 user/enqueue 记录实测会立即落盘。超时仍没有任何
      // 原生回执，只能说明 tmux 收到了按键，不能继续声称“排队中”。
      expiresAt: fields.created + 8000,
    };
  }

  queueAction(message) {
    const normal = super.queueAction(message);
    if (normal !== null) return normal;
    if (message?.role !== 'queue_operation') return null;
    if (message.operation === 'enqueue') {
      return { type: 'confirm', text: String(message.text || '') };
    }
    return message.operation === 'remove'
      ? { type: 'remove', text: String(message.text || '') } : null;
  }

  queuedMessageExpired(item, now, hasNativeHistory) {
    if (item?.legacy && !hasNativeHistory) return false;
    return item?.state === 'sending' && Number.isFinite(+item.expiresAt)
      && now >= +item.expiresAt;
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
    // v4 起 Codex 队列归服务端管理。浏览器旧副本没有交付凭据，全部丢弃；
    // 真实待发送项会随 /api/messages 或 SSE 重新同步回来。
    return fromVersion < 4 ? [] : super.migrateQueuedMessages(items);
  }

  queuedMessageLabel(item) {
    if (item?.state === 'delivering') return '发送中';
    if (item?.state === 'failed') return '发送未确认';
    return '排队中';
  }

  // Codex 的待发送消息由服务端持久队列管理。Esc 只中断当前回合；
  // 服务端确认原生状态已经结束后，仍要继续交付队首消息。
}

class GrokCli extends SesmanCli {
  constructor() {
    super('grok', 'Grok', 'i-grok', 'var(--grok)');
  }

  // Grok 暂无已验证的内存队列控制事件：只使用基类的正式消息对账，
  // 不把 Claude/Codex 的 Esc 假设套过来。
  queueAction(message) {
    return super.queueAction(message);
  }
}

const SESMAN_CLIS = Object.freeze({
  claude: new ClaudeCli(),
  codex: new CodexCli(),
  grok: new GrokCli(),
});

function sesmanCli(sourceOrUid) {
  const value = String(sourceOrUid || '');
  let source = value.split(':', 1)[0];
  // 首条原生记录落盘前，新建会话的 uid 是 tmux:sesman-<cli>-...。
  // 这个阶段也必须使用对应 CLI 的发送确认策略。
  if (source === 'tmux') {
    source = value.match(/^tmux:sesman-(claude|codex|grok)-/)?.[1] || source;
  }
  return SESMAN_CLIS[source] || null;
}

// 供 app.js、term.js 以及 headless 回归共同使用。
Object.assign(globalThis, {
  SesmanCli, ClaudeCli, CodexCli, GrokCli, SESMAN_CLIS, sesmanCli,
});
