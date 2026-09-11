(() => {
  const script = document.currentScript;
  const appName = script?.dataset.appName || '应用';
  const storageKey = script?.dataset.storageKey || 'pwa.install-dismissed';
  const iconUrl = new URL('icons/icon-192.png', script?.src || location.href).href;
  const dismissFor = 7 * 24 * 60 * 60 * 1000;
  let installPrompt = null;
  let overlay = null;

  const isStandalone = () =>
    matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;

  const recentlyDismissed = () => {
    try {
      const dismissedAt = Number(localStorage.getItem(storageKey) || 0);
      return Date.now() - dismissedAt < dismissFor;
    } catch {
      return false;
    }
  };

  const rememberDismissal = () => {
    try { localStorage.setItem(storageKey, String(Date.now())); } catch {}
  };

  const hide = () => {
    overlay?.remove();
    overlay = null;
  };

  const show = () => {
    if (overlay || isStandalone() || recentlyDismissed()) return;

    const canPrompt = Boolean(installPrompt);

    overlay = document.createElement('div');
    overlay.className = 'pwa-install-overlay';
    overlay.innerHTML = `
      <section class="pwa-install-card" role="dialog" aria-modal="true"
        aria-labelledby="pwa-install-title" aria-describedby="pwa-install-description">
        <img class="pwa-install-icon" src="${iconUrl}" alt="">
        <div class="pwa-install-copy">
          <h2 id="pwa-install-title">安装 ${appName} 到桌面</h2>
          <p id="pwa-install-description">${canPrompt
            ? '安装后会作为独立应用打开，不显示浏览器地址栏。'
            : '可安装为独立桌面应用，不显示浏览器地址栏。'}</p>
        </div>
        <div class="pwa-install-actions">
          <button type="button" data-pwa-dismiss>以后再说</button>
          <button type="button" class="pwa-install-primary" data-pwa-install>安装</button>
        </div>
      </section>
    `;
    document.body.append(overlay);
    overlay.querySelector('[data-pwa-install]')?.focus();

    overlay.querySelector('[data-pwa-dismiss]')?.addEventListener('click', () => {
      rememberDismissal();
      hide();
    });
    overlay.addEventListener('click', event => {
      if (event.target === overlay) {
        rememberDismissal();
        hide();
      }
    });
    overlay.querySelector('[data-pwa-install]')?.addEventListener('click', async () => {
      const button = overlay.querySelector('[data-pwa-install]');
      if (button.dataset.instructions) {
        rememberDismissal();
        hide();
        return;
      }
      const prompt = installPrompt;
      if (!prompt) {
        overlay.querySelector('#pwa-install-description').textContent =
          '请点击 Edge 地址栏右侧的“应用可用”图标，或打开 … → 应用 → 将此站点作为应用安装。';
        overlay.querySelector('[data-pwa-dismiss]')?.remove();
        button.textContent = '知道了';
        button.dataset.instructions = 'true';
        return;
      }
      installPrompt = null;
      hide();
      if (!prompt) return;
      const choice = await prompt.prompt();
      if (choice.outcome !== 'accepted') rememberDismissal();
    });
  };

  const style = document.createElement('style');
  style.textContent = `
    .pwa-install-overlay {
      position: fixed; inset: 0; z-index: 100000; display: grid; place-items: center;
      padding: 20px; background: rgb(15 23 42 / 45%); backdrop-filter: blur(3px);
      pointer-events: none;
    }
    .pwa-install-card {
      box-sizing: border-box; width: min(400px, 100%); padding: 22px;
      display: grid; grid-template-columns: 64px 1fr; gap: 14px 16px;
      color: var(--text, #1c2024); background: var(--panel, #fff);
      border: 1px solid var(--border, #e3e6ec); border-radius: 18px;
      box-shadow: 0 24px 70px rgb(15 23 42 / 30%);
      pointer-events: auto;
    }
    .pwa-install-icon { width: 64px; height: 64px; border-radius: 15px; }
    .pwa-install-copy { align-self: center; min-width: 0; }
    .pwa-install-copy h2 { margin: 0 0 6px; font: 700 18px/1.3 system-ui, sans-serif; }
    .pwa-install-copy p {
      margin: 0; color: var(--muted, #6b7280); font: 400 13px/1.55 system-ui, sans-serif;
    }
    .pwa-install-actions {
      grid-column: 1 / -1; display: flex; justify-content: flex-end; gap: 9px; margin-top: 4px;
    }
    .pwa-install-actions button {
      min-height: 36px; padding: 7px 15px; color: var(--text, #1c2024);
      background: var(--panel-2, #fbfbfd); border: 1px solid var(--border, #e3e6ec);
      border-radius: 9px; font: 600 14px/1 system-ui, sans-serif; cursor: pointer;
    }
    .pwa-install-actions .pwa-install-primary {
      color: #fff; background: var(--accent, #3b6ef6); border-color: transparent;
    }
    @media (max-width: 480px) {
      .pwa-install-overlay { align-items: end; padding: 12px; }
      .pwa-install-card { padding: 18px; border-radius: 18px; }
    }
  `;
  document.head.append(style);

  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
    if (overlay) hide();
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', show, { once: true });
    } else {
      show();
    }
  });

  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    hide();
    try { localStorage.removeItem(storageKey); } catch {}
  });

  const showFallback = () => setTimeout(show, 1200);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', showFallback, { once: true });
  } else {
    showFallback();
  }
})();
