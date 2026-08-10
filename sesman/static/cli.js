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

  questionAnswerKeys(_prompt, _optionIndex) {
    return null;
  }

  questionCancelKeys() {
    return ['Escape'];
  }

  repeatedEscape(_now, _previousAt, _context = {}) {
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
    if (message.operation === 'remove') {
      return { type: 'remove', text: String(message.text || '') };
    }
    // Claude 2.1.226 在当前回合结束/中断后，为每条即将提升成正式 user
    // 消息的输入写一个无正文 dequeue；旧版本使用带正文的 popAll。
    if (message.operation === 'dequeue') return { type: 'promote-first' };
    if (message.operation === 'popAll') return { type: 'promote-all' };
    return null;
  }

  queuedMessageExpired(item, now, hasNativeHistory) {
    if (item?.legacy && !hasNativeHistory) return false;
    return item?.state === 'sending' && Number.isFinite(+item.expiresAt)
      && now >= +item.expiresAt;
  }

  questionAnswerKeys(prompt, optionIndex) {
    const options = prompt?.questions?.[0]?.options || [];
    if (!options[optionIndex]) return null;
    return [...Array(options.length + 3).fill('Up'),
      ...Array(optionIndex).fill('Down'), 'Enter'];
  }

  repeatedEscape(now, previousAt, context = {}) {
    // 运行中 Esc 是中断，对话框中 Esc 是取消；两者都不是 rewind 的第一击。
    if (context.busy || context.empty === false) {
      return { rewind: false, nextAt: -Infinity };
    }
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

  questionAnswerKeys(prompt, optionIndex) {
    const options = prompt?.questions?.[0]?.options || [];
    if (!options[optionIndex] || optionIndex >= 9) return null;
    // Codex 的问题菜单会循环选择，不能照搬 Claude 的“多按 Up 夹到
    // 第一项”。菜单原生支持数字直选且立即提交，位置不受另一网页或
    // 原生终端先前移动光标的影响。request_user_input 目前最多三个选项。
    return [String(optionIndex + 1)];
  }

  repeatedEscape(now, previousAt, context = {}) {
    // Codex 官方语义：运行/询问中单 Esc 中断；空输入双 Esc 进入上一条
    // 用户消息的编辑与 fork 界面。
    if (context.busy || context.empty === false) {
      return { rewind: false, nextAt: -Infinity };
    }
    const rewind = now - previousAt <= 650;
    return { rewind, nextAt: rewind ? -Infinity : now };
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
