(() => {
  const byId = id => document.getElementById(id);
  let lastVersions = null;
  let localUpdateStarting = false;

  function setBadge(id, component) {
    const el = byId(id);
    if (!el) return;
    if (!component || component.updateAvailable == null) {
      el.textContent = '● Version check unavailable';
      el.style.color = '';
      el.style.borderColor = '';
      return;
    }
    if (component.updateAvailable) {
      el.textContent = '● Update available';
      el.style.color = '#f4d35e';
      el.style.borderColor = '#b89835';
    } else {
      el.textContent = '● Up to date';
      el.style.color = '';
      el.style.borderColor = '';
    }
  }

  function applyUpdateButtonState() {
    const button = byId('updateNow');
    if (!button) return;
    const server = lastVersions?.server;
    const running = Boolean(server?.updateInProgress || localUpdateStarting);
    const canUpdate = server?.updateAvailable === true && !running;
    button.disabled = !canUpdate;

    if (running) {
      button.title = 'Server update is in progress';
    } else if (server?.updateAvailable === true) {
      button.title = `Update Server: ${server.installed || 'unknown'} → ${server.available}`;
    } else if (server?.updateAvailable === false) {
      button.title = `Server ${server.installed || ''} is already the latest release`.trim();
    } else {
      button.title = 'Update disabled until the installed and available SERVER_VERSION values can be verified';
    }
  }

  async function guardedUpdateClick() {
    const button = byId('updateNow');
    const status = byId('updateStatus');
    const server = lastVersions?.server;

    if (!server || server.updateAvailable !== true) {
      if (status) {
        status.textContent = server?.updateAvailable === false
          ? `Server is up to date (${server.installed}) · Update disabled`
          : 'Server version could not be verified · Update disabled';
      }
      applyUpdateButtonState();
      return;
    }

    if (!confirm(`Update Server ${server.installed || 'unknown'} → ${server.available}? Active streams may be interrupted during activation.`)) return;

    localUpdateStarting = true;
    applyUpdateButtonState();
    if (status) status.textContent = 'Starting verified server update…';

    try {
      const response = await fetch('/api/update', {method: 'POST'});
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || 'Update failed');

      if (status) status.textContent = body.message || 'Server update requested';
      if (typeof window.toast === 'function') {
        window.toast(body.started === false ? 'Server is already up to date' : 'Server update started');
      }
      if (body.started === false) localUpdateStarting = false;
    } catch (error) {
      localUpdateStarting = false;
      if (status) status.textContent = error.message || 'Update failed';
    }

    setTimeout(() => {
      localUpdateStarting = false;
      refreshComponentVersions();
    }, 2500);
  }

  function setupLifecyclePanel() {
    const updateButton = byId('updateNow');
    if (!updateButton) return false;
    const panel = updateButton.closest('.panel');
    if (!panel || panel.dataset.componentLifecycle === '1') return true;
    panel.dataset.componentLifecycle = '1';

    const versionLine = panel.querySelector('.versionline');
    if (versionLine) {
      versionLine.innerHTML = `
        <div><span>Server installed</span><b id="version">—</b></div>
        <div><span>Server available</span><b class="purple" id="githubVersion">Checking…</b></div>
        <div><span>Core installed</span><b id="coreVersion">—</b></div>
        <div><span>WebAdmin installed</span><b id="webadminInstalledVersion">—</b></div>
        <div><span>WebAdmin available</span><b class="purple" id="webadminAvailableVersion">Checking…</b></div>`;
    }

    updateButton.textContent = 'Update Server';
    updateButton.onclick = guardedUpdateClick;
    updateButton.disabled = true;
    updateButton.title = 'Checking SERVER_VERSION…';

    const refreshButton = document.createElement('button');
    refreshButton.className = 'btn ghost';
    refreshButton.id = 'refreshComponentVersions';
    refreshButton.textContent = 'Refresh versions';

    const parent = updateButton.parentElement;
    if (parent && !byId('componentUpdateActions')) {
      const actions = document.createElement('div');
      actions.className = 'toolbarActions';
      actions.id = 'componentUpdateActions';
      parent.replaceChild(actions, updateButton);
      actions.appendChild(updateButton);
      actions.appendChild(refreshButton);
    }

    const updateBox = panel.querySelector('.updatebox');
    if (updateBox && !byId('componentLifecycleCards')) {
      const cards = document.createElement('div');
      cards.className = 'statusgrid';
      cards.id = 'componentLifecycleCards';
      cards.style.marginTop = '18px';
      cards.innerHTML = `
        <div class="statuscard">
          <div class="row"><strong>Streaming Server</strong><span class="badge" id="serverLifecycleBadge">● Checking</span></div>
          <div class="hint" style="margin:12px 0 0">Transactional activation, health validation and automatic rollback. WebAdmin and Pi-hole stay online.</div>
        </div>
        <div class="statuscard">
          <div class="row"><strong>WebAdmin</strong><span class="badge" id="webadminLifecycleBadge">● Checking</span></div>
          <div class="hint" style="margin:12px 0 0">Independent release. Activation is a host-side rebuild of only the WebAdmin service.</div>
        </div>`;
      updateBox.insertAdjacentElement('afterend', cards);
    }

    const notice = panel.querySelector('.notice');
    if (notice) {
      notice.innerHTML = `
        <strong>Independent release lifecycles.</strong><br>
        Server updates come only from <code>emmanique/stremio-libtorrent-server-webadmin</code> and start only when a different <code>SERVER_VERSION</code> is verified.
        A WebAdmin update never triggers a server rebuild automatically. To activate a WebAdmin release on the host, run:<br><br>
        <code id="webadminUpdateCommand">git pull origin main &amp;&amp; docker compose up -d --build --no-deps webadmin</code>
        <button class="mini" id="copyWebadminUpdate" type="button" style="margin-left:8px">Copy command</button>`;
    }

    byId('refreshComponentVersions')?.addEventListener('click', refreshComponentVersions);
    byId('copyWebadminUpdate')?.addEventListener('click', async () => {
      const command = byId('webadminUpdateCommand')?.textContent || '';
      try {
        await navigator.clipboard.writeText(command);
        byId('copyWebadminUpdate').textContent = 'Copied';
        setTimeout(() => {
          const button = byId('copyWebadminUpdate');
          if (button) button.textContent = 'Copy command';
        }, 1500);
      } catch (_) {
        window.prompt('Copy this command:', command);
      }
    });
    return true;
  }

  async function refreshComponentVersions() {
    if (!setupLifecyclePanel()) return;
    try {
      const response = await fetch('/api/component-versions', {cache: 'no-store'});
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      lastVersions = data;

      if (byId('version')) byId('version').textContent = data.server?.installed || 'Unknown';
      if (byId('githubVersion')) byId('githubVersion').textContent = data.server?.available || 'Unavailable';
      if (byId('coreVersion')) byId('coreVersion').textContent = data.core?.installed || 'Unknown';
      if (byId('webadminInstalledVersion')) byId('webadminInstalledVersion').textContent = data.webadmin?.installed || 'Unknown';
      if (byId('webadminAvailableVersion')) byId('webadminAvailableVersion').textContent = data.webadmin?.available || 'Unavailable';

      setBadge('serverLifecycleBadge', data.server);
      setBadge('webadminLifecycleBadge', data.webadmin);

      const command = data.webadmin?.updateCommand;
      if (command && byId('webadminUpdateCommand')) byId('webadminUpdateCommand').textContent = command;

      if (data.server?.updateInProgress) localUpdateStarting = false;
      applyUpdateButtonState();

      const status = byId('updateStatus');
      if (status && !data.server?.updateInProgress && !localUpdateStarting) {
        const serverText = data.server?.updateAvailable === true
          ? `Server update available: ${data.server.installed || 'unknown'} → ${data.server.available}`
          : data.server?.updateAvailable === false
            ? `Server is up to date (${data.server.installed}) · Update disabled`
            : 'Server version check unavailable · Update disabled';
        const webText = data.webadmin?.updateAvailable === true
          ? `WebAdmin update available: ${data.webadmin.installed || 'unknown'} → ${data.webadmin.available}`
          : data.webadmin?.updateAvailable === false
            ? `WebAdmin is up to date (${data.webadmin.installed})`
            : 'WebAdmin version check unavailable';
        status.textContent = `${serverText} · ${webText}`;
      }
    } catch (_) {
      lastVersions = null;
      if (byId('serverLifecycleBadge')) byId('serverLifecycleBadge').textContent = '● Check failed';
      if (byId('webadminLifecycleBadge')) byId('webadminLifecycleBadge').textContent = '● Check failed';
      applyUpdateButtonState();
    }
  }

  function enforceUpdateGuard() {
    applyUpdateButtonState();
  }

  function start() {
    if (!setupLifecyclePanel()) return;
    refreshComponentVersions();
    setInterval(refreshComponentVersions, 30000);
    // The legacy five-second status poll also writes to the same button. Keep
    // the independent SERVER_VERSION gate authoritative in the browser.
    setInterval(enforceUpdateGuard, 500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, {once: true});
  } else {
    start();
  }
})();