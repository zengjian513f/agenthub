'use strict';
(() => {
  const choices = {
    ubuntu: '"AgentHub CJK Sans", "AgentHub Ubuntu Sans Mono", "Ubuntu Sans Mono", "AgentHub Cascadia Mono", "Cascadia Mono", "Adwaita Mono", "Ubuntu Mono", Consola, Consolas, sans-serif',
    cascadia: '"AgentHub CJK Sans", "AgentHub Cascadia Mono", "Cascadia Mono", "Adwaita Mono", "Ubuntu Mono", Consola, Consolas, sans-serif',
    system: '"AgentHub CJK Sans", ui-monospace, "SFMono-Regular", "Cascadia Mono", "Adwaita Mono", "Ubuntu Mono", "Liberation Mono", Consolas, sans-serif',
    consolas: '"AgentHub CJK Sans", Consolas, Consola, "Cascadia Mono", "Liberation Mono", sans-serif',
  };
  const root = new URL('.', location.href).pathname;
  const hub = document.querySelector('meta[name="agenthub-mode"]')?.content === 'hub';
  const prefix = hub ? `agenthub.hub.${root}.` : 'agenthub.';
  function apply() {
    let choice = 'ubuntu';
    try { choice = JSON.parse(localStorage.getItem(prefix + 'font')) || choice; } catch {}
    document.documentElement.style.setProperty('--terminal-font', choices[choice] || choices.ubuntu);
  }
  window.AgentHubTypography = {choices, apply};
  apply();
  addEventListener('storage', event => { if (event.key === prefix + 'font' || event.key === null) apply(); });
})();
