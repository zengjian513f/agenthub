(() => {
  const buttons = [...document.querySelectorAll('[data-pwa-install-button]')];
  if (!buttons.length) return;

  let installPrompt = null;
  let installed = false;

  const isStandalone = () =>
    matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;

  const render = () => {
    const active = installed || isStandalone();
    for (const button of buttons) {
      button.textContent = active ? '已安装' : '安装到桌面';
      button.disabled = active || !installPrompt;
      button.title = active
        ? '当前已作为独立应用运行'
        : installPrompt ? '安装为独立桌面应用' : '浏览器尚未提供安装能力';
    }
  };

  const install = async () => {
    const prompt = installPrompt;
    if (!prompt) return;
    installPrompt = null;
    render();
    const choice = await prompt.prompt();
    if (choice.outcome === 'accepted') installed = true;
    render();
  };

  for (const button of buttons) button.addEventListener('click', install);

  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
    render();
  });

  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    installed = true;
    render();
  });

  render();
})();
