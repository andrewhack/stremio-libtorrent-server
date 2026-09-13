(() => {
  const $ = id => document.getElementById(id);
  let profileState = null;

  const style = document.createElement('style');
  style.textContent = `
    .simpleTranscode{margin:0 0 18px;border:1px solid rgba(66,212,119,.45);border-radius:14px;background:rgba(7,35,19,.82);overflow:hidden}
    .simpleTranscodeHead{padding:14px 16px;border-bottom:1px solid rgba(66,212,119,.24);display:flex;justify-content:space-between;gap:12px;align-items:center}
    .simpleTranscodeHead strong{color:#7ff0aa;font-size:15px}.simpleTranscodeHead small{color:var(--muted)}
    .simpleTranscodeBody{padding:16px}.simpleTranscodeRule{padding:12px 14px;border-radius:9px;background:rgba(4,18,10,.82);color:#f5edc6;margin-bottom:14px}
    .simpleTranscodeGrid{display:grid;grid-template-columns:minmax(260px,1fr) 150px auto;gap:10px;align-items:end}
    .simpleTranscodeField label{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;margin-bottom:5px}
    .simpleTranscodeField select,.simpleTranscodeField input{width:100%;background:#13291b;border:1px solid #47633d;color:var(--text);border-radius:8px;padding:10px}
    .simpleTranscodeDetail{margin-top:12px;color:#d9d2ad;font-size:12px}
    .simpleTranscodeChecks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:14px}
    .simpleTranscodeCheck{padding:9px 11px;border:1px solid #35482f;border-radius:8px;background:rgba(8,24,14,.72);font-size:11px}
    .simpleTranscodeCheck.ok{border-color:#2d9957;color:#71e79c}.simpleTranscodeCheck.no{color:#d0b7a0}.simpleTranscodeCheck b{display:block;color:inherit;margin-bottom:2px}
    .simpleTranscodeStatus{margin-top:10px;color:var(--muted);font-size:11px}
    @media(max-width:760px){.simpleTranscodeGrid{grid-template-columns:1fr}.simpleTranscodeChecks{grid-template-columns:1fr}}
  `;
  document.head.appendChild(style);

  function install() {
    const configPanel = document.querySelector('#configuration .configPanel');
    if (!configPanel || $('simpleTranscode')) return;
    const node = document.createElement('section');
    node.id = 'simpleTranscode';
    node.className = 'simpleTranscode';
    node.innerHTML = `
      <div class="simpleTranscodeHead"><div><strong>Simple transcoding</strong><br><small>Only runtime-verified encoders can be selected.</small></div><button class="btn ghost" id="simpleTranscodeRefresh">Re-test hardware</button></div>
      <div class="simpleTranscodeBody">
        <div class="simpleTranscodeRule" id="simpleTranscodeRule">Loading verified profiles…</div>
        <div class="simpleTranscodeGrid">
          <div class="simpleTranscodeField"><label>Video execution profile</label><select id="simpleTranscodeProfile"></select></div>
          <div class="simpleTranscodeField"><label>Quality (0–51)</label><input id="simpleTranscodeQuality" type="number" min="0" max="51" step="1" value="22"></div>
          <button class="btn" id="simpleTranscodeSave">Apply profile</button>
        </div>
        <div class="simpleTranscodeDetail" id="simpleTranscodeDetail"></div>
        <div class="simpleTranscodeChecks" id="simpleTranscodeChecks"></div>
        <div class="simpleTranscodeStatus" id="simpleTranscodeStatus"></div>
      </div>`;
    const tools = configPanel.querySelector('.configTools');
    configPanel.insertBefore(node, tools || configPanel.querySelector('.configGrid'));
    $('simpleTranscodeProfile').addEventListener('change', updateDetail);
    $('simpleTranscodeSave').addEventListener('click', saveProfile);
    $('simpleTranscodeRefresh').addEventListener('click', () => loadProfiles(true));
  }

  function hideLegacyControls() {
    document.querySelectorAll('#configuration .configGroup').forEach(group => {
      const title = group.querySelector('summary')?.textContent || '';
      if (title.includes('Transcoding policy & hardware')) group.style.display = 'none';
    });
    document.querySelectorAll('#configuration .configItem label[title]').forEach(label => {
      const name = label.getAttribute('title') || '';
      if (name === 'transcode_profile' || name.startsWith('transcoding_')) {
        label.closest('.configItem').style.display = 'none';
      }
    });
  }

  function selectedItem() {
    const id = $('simpleTranscodeProfile')?.value;
    return profileState?.profiles?.find(item => item.id === id) || null;
  }

  function updateDetail() {
    const item = selectedItem();
    $('simpleTranscodeDetail').textContent = item
      ? `${item.description} Encoder: ${item.encoder || 'unchanged'} · engine: ${item.engine}.`
      : 'Choose one explicit verified profile. No automatic encoder choice is made.';
    $('simpleTranscodeSave').disabled = !item || !item.available;
  }

  function renderProfiles(data) {
    profileState = data;
    install();
    hideLegacyControls();
    $('simpleTranscodeRule').textContent = data.rule;
    $('simpleTranscodeQuality').value = data.quality ?? 22;

    const select = $('simpleTranscodeProfile');
    const options = [];
    if (data.selected === 'legacy') {
      options.push('<option value="" selected disabled>Legacy settings active — choose a verified profile</option>');
    }
    for (const item of data.profiles || []) {
      const selected = item.id === data.selected ? 'selected' : '';
      const disabled = item.available ? '' : 'disabled';
      const suffix = item.available ? 'verified' : 'unavailable';
      options.push(`<option value="${item.id}" ${selected} ${disabled}>${item.label} — ${suffix}</option>`);
    }
    select.innerHTML = options.join('');

    $('simpleTranscodeChecks').innerHTML = (data.profiles || []).filter(item => item.id !== 'preserve').map(item =>
      `<div class="simpleTranscodeCheck ${item.available ? 'ok' : 'no'}"><b>${item.label} · ${item.available ? 'VERIFIED' : 'NOT AVAILABLE'}</b>${item.reason || ''}</div>`
    ).join('');
    $('simpleTranscodeStatus').textContent = `FFmpeg ${data.ffmpeg || 'unavailable'} · VAAPI device ${data.device || 'n/a'} · checked ${new Date(data.checkedAt).toLocaleTimeString()}`;
    updateDetail();
  }

  async function loadProfiles(force = false) {
    install();
    $('simpleTranscodeStatus').textContent = force ? 'Testing encoders with one-frame runtime tests…' : 'Reading verified profiles…';
    try {
      const response = await fetch(force ? '/api/transcoding/profiles/refresh' : '/api/transcoding/profiles', {
        method: force ? 'POST' : 'GET', cache: 'no-store'
      });
      if (!response.ok) throw new Error(await response.text());
      renderProfiles(await response.json());
    } catch (error) {
      $('simpleTranscodeRule').textContent = 'Could not verify transcoding profiles. No profile will be selected automatically.';
      $('simpleTranscodeStatus').textContent = String(error);
      $('simpleTranscodeSave').disabled = true;
    }
  }

  async function saveProfile() {
    const item = selectedItem();
    if (!item || !item.available) return;
    const quality = Number($('simpleTranscodeQuality').value);
    if (!Number.isInteger(quality) || quality < 0 || quality > 51) {
      $('simpleTranscodeStatus').textContent = 'Quality must be an integer from 0 to 51.';
      return;
    }
    $('simpleTranscodeSave').disabled = true;
    $('simpleTranscodeStatus').textContent = 'Applying explicit profile…';
    try {
      const response = await fetch('/api/transcoding/profile', {
        method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({profile: item.id, quality})
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || 'Could not apply profile');
      $('simpleTranscodeStatus').textContent = 'Profile applied. New FFmpeg jobs use it immediately; restart current playback to replace an existing job.';
      if (typeof toast === 'function') toast('Transcoding profile applied');
      await loadProfiles(false);
    } catch (error) {
      $('simpleTranscodeStatus').textContent = String(error);
    } finally {
      updateDetail();
    }
  }

  const observer = new MutationObserver(() => {
    install();
    hideLegacyControls();
  });
  const root = document.getElementById('configuration');
  if (root) observer.observe(root, {childList:true, subtree:true});

  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      if (tab.dataset.page === 'configuration') setTimeout(() => loadProfiles(false), 50);
    });
  });

  install();
  hideLegacyControls();
})();
