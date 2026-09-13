(() => {
  const esc = value => {
    const node = document.createElement('div');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  };

  const style = document.createElement('style');
  style.textContent = `
    .transcodePanel .inner{display:grid;gap:14px}
    .transcodeCards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
    .transcodeCard{padding:12px;border:1px solid rgba(232,207,105,.25);border-radius:10px;background:rgba(13,30,21,.95)}
    .transcodeCard span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}
    .transcodeCard b{display:block;margin-top:5px;font-size:15px;overflow-wrap:anywhere}
    .transcodeSummary{padding:12px 14px;border:1px solid rgba(242,201,76,.25);border-radius:10px;background:rgba(4,18,10,.75);color:#f7edc1}
    .transcodeHw{display:flex;gap:7px;flex-wrap:wrap}
    .transcodeChip{border:1px solid #52613b;border-radius:99px;padding:4px 9px;color:var(--muted);font-size:10px}
    .transcodeChip.on{border-color:#3da864;color:var(--green);background:#123f25}
    .transcodeChip.warn{border-color:#a98735;color:#f4d35e;background:#473c16}
    .transcodeSessions{display:grid;gap:8px}
    .transcodeSession{padding:12px;border:1px solid rgba(232,207,105,.2);border-radius:9px;background:rgba(12,25,18,.92)}
    .transcodeSessionHead{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
    .transcodeSessionHead strong{font-size:13px}
    .transcodeSessionGrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:9px}
    .transcodeKv{padding:8px;border-radius:7px;background:rgba(28,49,34,.75)}
    .transcodeKv span{display:block;color:var(--muted);font-size:9px;text-transform:uppercase}
    .transcodeKv b{display:block;margin-top:3px;font-size:11px;overflow-wrap:anywhere}
    .transcodeLatest{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .transcodeLatest>div{padding:10px;border:1px solid rgba(232,207,105,.18);border-radius:8px;background:rgba(7,18,11,.72)}
    .transcodeLatest small{display:block;color:var(--muted);margin-bottom:4px}
    .transcodeLatest code{color:#fff7d6;white-space:normal;overflow-wrap:anywhere}
    .transcodePolicyIntro{margin:0 12px 12px;padding:14px;border:1px solid rgba(242,201,76,.28);border-radius:10px;background:linear-gradient(110deg,rgba(17,72,39,.82),rgba(67,21,25,.75))}
    .transcodePolicyIntro strong{display:block;color:#f4d35e;margin-bottom:5px}
    .transcodePolicyIntro p{margin:0 0 10px;color:#e6dfbd;font-size:12px}
    .transcodePolicyFlow{display:flex;gap:7px;align-items:center;flex-wrap:wrap;color:var(--muted);font-size:11px}
    .transcodePolicyFlow b{color:#fff7d6}
    .configItem.transcodingPolicy{box-shadow:inset 3px 0 0 rgba(242,201,76,.58)}
    .configItem select{width:100%;min-width:0;background:#24212f;border:1px solid #3a3547;color:var(--text);border-radius:7px;padding:9px}
    @media(max-width:900px){.transcodeCards,.transcodeSessionGrid{grid-template-columns:1fr 1fr}.transcodeLatest{grid-template-columns:1fr}}
    @media(max-width:540px){.transcodeCards,.transcodeSessionGrid{grid-template-columns:1fr}}
  `;
  document.head.appendChild(style);

  function installDashboardPanel() {
    if (document.getElementById('transcodingPanel')) return;
    const stacks = document.querySelectorAll('#dashboard > .stack');
    if (stacks.length < 2) return;
    const panel = document.createElement('article');
    panel.className = 'panel transcodePanel';
    panel.id = 'transcodingPanel';
    panel.innerHTML = `
      <div class="title row"><span>TRANSCODING POLICY &amp; ACCELERATION</span><span class="badge" id="tdPolicyState">Loading…</span></div>
      <div class="inner">
        <div class="transcodeCards">
          <div class="transcodeCard"><span>Policy mode</span><b id="tdMode">—</b></div>
          <div class="transcodeCard"><span>Active FFmpeg</span><b id="tdActive">—</b></div>
          <div class="transcodeCard"><span>Active engine</span><b id="tdEngine">—</b></div>
          <div class="transcodeCard"><span>Encoder load</span><b id="tdEncoderLoad">—</b></div>
        </div>
        <div class="transcodeSummary" id="tdSummary">Loading effective transcoding policy…</div>
        <div class="transcodeHw" id="tdHardware"></div>
        <div class="transcodeSessions" id="tdSessions"><div class="empty">No active FFmpeg sessions</div></div>
        <div class="transcodeLatest">
          <div><small>Latest policy decision</small><code id="tdDecision">—</code></div>
          <div><small>Latest FFmpeg progress</small><code id="tdProgress">—</code></div>
        </div>
      </div>
      <div class="foot">Direct Stream vs transcoding, source → target codecs, VAAPI/NVENC/CPU selection and live FFmpeg progress.</div>`;
    const settings = stacks[1].querySelector('.panel.settings');
    stacks[1].insertBefore(panel, settings || null);
  }

  function chip(label, enabled, warning = false) {
    return `<span class="transcodeChip ${enabled ? 'on' : warning ? 'warn' : ''}">${esc(label)} · ${enabled ? 'ready' : warning ? 'missing' : 'no'}</span>`;
  }

  function sessionHtml(session, index) {
    const action = session.action === 'direct-stream' ? 'DIRECT STREAM' : session.action === 'transcoding' ? 'TRANSCODING' : 'FFMPEG';
    const progress = session.progress || {};
    const video = `${session.sourceVideo || '?'} → ${session.targetVideo || '?'}`;
    const audio = `${session.sourceAudio || '?'} → ${session.targetAudio || '?'}`;
    const speed = progress.speed || '—';
    const fps = progress.fps == null ? '—' : `${progress.fps.toFixed(1)} fps`;
    return `<div class="transcodeSession">
      <div class="transcodeSessionHead"><strong>${session.jobId ? `Job ${esc(session.jobId)}` : `FFmpeg session ${index + 1}`}</strong><span class="badge">${action}</span></div>
      <div class="transcodeSessionGrid">
        <div class="transcodeKv"><span>Video</span><b>${esc(video)}</b></div>
        <div class="transcodeKv"><span>Audio</span><b>${esc(audio)}</b></div>
        <div class="transcodeKv"><span>Engine</span><b>${esc((session.engine || 'none').toUpperCase())}</b></div>
        <div class="transcodeKv"><span>Progress</span><b>${esc(fps)} · ${esc(speed)}</b></div>
      </div>
    </div>`;
  }

  function renderTranscoding(data) {
    if (!document.getElementById('transcodingPanel')) installDashboardPanel();
    if (!data || !data.available) {
      document.getElementById('tdPolicyState').textContent = 'Unavailable';
      document.getElementById('tdSummary').textContent = data?.message || 'Transcoding telemetry is unavailable.';
      return;
    }
    const policy = data.policy || {};
    const active = data.active || {};
    const hw = data.hardware || {};
    const sessions = active.sessions || [];
    const engines = active.engines || [];
    document.getElementById('tdPolicyState').textContent = `● ${(policy.transcoding_mode || 'auto').toUpperCase()}`;
    document.getElementById('tdMode').textContent = `${String(policy.transcoding_mode || 'auto').toUpperCase()} · ${String(policy.transcoding_hwaccel || 'auto').toUpperCase()}`;
    document.getElementById('tdActive').textContent = `${active.total || 0} · ${active.transcoding || 0} transcode · ${active.directStream || 0} direct`;
    document.getElementById('tdEngine').textContent = engines.length ? engines.map(x => x.toUpperCase()).join(' + ') : 'Idle';
    const utilisation = hw.encoderUtilizationPercent;
    if (utilisation != null) {
      document.getElementById('tdEncoderLoad').textContent = `${Number(utilisation).toFixed(1)}% · NVENC`;
    } else if ((active.transcoding || 0) > 0 && engines.includes('vaapi')) {
      document.getElementById('tdEncoderLoad').textContent = 'Active · VAAPI';
    } else if ((active.transcoding || 0) > 0) {
      document.getElementById('tdEncoderLoad').textContent = 'Active · no HW metric';
    } else {
      document.getElementById('tdEncoderLoad').textContent = 'Idle';
    }
    document.getElementById('tdSummary').textContent = data.policySummary || '—';
    document.getElementById('tdHardware').innerHTML = [
      chip(`VAAPI ${hw.vaapiDevice || ''}`, Boolean(hw.vaapiDevicePresent && (hw.h264Vaapi || hw.hevcVaapi)), !hw.vaapiDevicePresent),
      chip('H.264 VAAPI', Boolean(hw.h264Vaapi)),
      chip('HEVC VAAPI', Boolean(hw.hevcVaapi)),
      chip('H.264 NVENC', Boolean(hw.h264Nvenc)),
      chip('HEVC NVENC', Boolean(hw.hevcNvenc)),
      chip('libx264 fallback', Boolean(hw.libx264))
    ].join('');
    document.getElementById('tdSessions').innerHTML = sessions.length ? sessions.map(sessionHtml).join('') : '<div class="empty">No active FFmpeg sessions</div>';
    document.getElementById('tdDecision').textContent = data.latestDecision?.decision || 'No policy decision recorded yet';
    const progress = data.latestProgress;
    document.getElementById('tdProgress').textContent = progress
      ? `${progress.fps == null ? '—' : progress.fps.toFixed(1) + ' fps'} · ${progress.bitrate || '—'} · speed ${progress.speed || '—'}`
      : 'No FFmpeg progress recorded yet';
  }

  async function loadTranscoding() {
    try {
      const response = await fetch('/api/transcoding/status', {cache: 'no-store'});
      renderTranscoding(await response.json());
    } catch (error) {
      renderTranscoding({available: false, message: 'Could not read transcoding telemetry.'});
    }
  }

  function installConfigurationControls() {
    if (typeof configGroups === 'undefined' || typeof configInput === 'undefined') return;
    const index = configGroups.findIndex(group => group.id === 'transcode');
    if (index >= 0) {
      configGroups.splice(index, 1,
        {id: 'transcoding-policy', title: 'Transcoding policy & hardware', match: name => name.startsWith('transcoding_')},
        {id: 'transcode-runtime', title: 'Transcoding runtime & lifecycle', match: name => name.startsWith('transcode_')}
      );
    }

    const baseConfigInput = configInput;
    const choices = {
      transcoding_mode: [['auto','Auto'],['copy','Copy / passthrough'],['h264','Force H.264'],['hevc','Force HEVC'],['software','Force software']],
      transcoding_hwaccel: [['auto','Auto detect'],['vaapi','VAAPI'],['nvenc','NVIDIA NVENC'],['cpu','CPU']],
      transcoding_video_codec: [['h264_vaapi','H.264 VAAPI'],['hevc_vaapi','HEVC VAAPI'],['h264_nvenc','H.264 NVENC'],['hevc_nvenc','HEVC NVENC'],['libx264','H.264 software'],['libx265','HEVC software']],
      transcoding_audio_codec: [['aac','AAC'],['ac3','AC-3'],['libopus','Opus']],
      transcoding_fallback_codec: [['libx264','H.264 / libx264'],['libx265','HEVC / libx265']]
    };

    configInput = function(item) {
      if (choices[item.name]) {
        const disabled = item.editable ? '' : 'disabled';
        const current = String(item.value ?? '');
        const list = [...choices[item.name]];
        if (current && !list.some(([value]) => value === current)) list.unshift([current, `${current} (current)`]);
        return `<select data-config="${esc(item.name)}" data-scale="1" ${disabled}>${list.map(([value,label]) => `<option value="${esc(value)}" ${value===current?'selected':''}>${esc(label)}</option>`).join('')}</select>`;
      }
      if (item.name === 'transcoding_video_quality') {
        return `<input data-config="${esc(item.name)}" data-scale="1" type="number" min="0" max="51" step="1" value="${esc(item.displayValue ?? item.value ?? 22)}" ${item.editable?'':'disabled'}>`;
      }
      return baseConfigInput(item);
    };

    const baseRenderConfig = renderConfig;
    renderConfig = function() {
      baseRenderConfig();
      decorateTranscodingConfig();
    };
    const search = document.getElementById('configSearch');
    if (search) search.addEventListener('input', () => queueMicrotask(decorateTranscodingConfig));
    const configuration = document.getElementById('configuration');
    if (configuration) configuration.addEventListener('input', refreshPolicyPreview);
    if (configuration) configuration.addEventListener('change', refreshPolicyPreview);
  }

  function currentConfigValue(name) {
    const input = document.querySelector(`[data-config="${name}"]`);
    if (input) return input.type === 'checkbox' ? input.checked : input.value;
    if (typeof configItems !== 'undefined') return configItems.find(item => item.name === name)?.value;
    return undefined;
  }

  function policyPreviewText() {
    const mode = currentConfigValue('transcoding_mode') || 'auto';
    const hw = currentConfigValue('transcoding_hwaccel') || 'auto';
    const video = currentConfigValue('transcoding_video_codec') || 'h264_vaapi';
    const quality = currentConfigValue('transcoding_video_quality') || 22;
    const audio = currentConfigValue('transcoding_audio_codec') || 'aac';
    const bitrate = currentConfigValue('transcoding_audio_bitrate') || '192k';
    const fallback = currentConfigValue('transcoding_fallback_codec') || 'libx264';
    if (mode === 'copy') return 'COPY: the wrapper does not replace Direct Stream codec decisions.';
    return `${String(mode).toUpperCase()}: compatible codecs remain Direct Stream; incompatible video → ${video} (${hw}, quality ${quality}); audio → ${audio} ${bitrate}; hardware failure → ${fallback}.`;
  }

  function refreshPolicyPreview() {
    const preview = document.getElementById('transcodePolicyPreview');
    if (preview) preview.textContent = policyPreviewText();
  }

  function decorateTranscodingConfig() {
    const groups = [...document.querySelectorAll('.configGroup')];
    const policyGroup = groups.find(group => group.querySelector('summary')?.textContent.includes('Transcoding policy & hardware'));
    if (!policyGroup) return;
    policyGroup.querySelectorAll('.configItem').forEach(card => card.classList.add('transcodingPolicy'));
    if (!policyGroup.querySelector('.transcodePolicyIntro')) {
      const intro = document.createElement('div');
      intro.className = 'transcodePolicyIntro';
      intro.innerHTML = `<strong>FFmpeg compatibility policy</strong><p id="transcodePolicyPreview"></p><div class="transcodePolicyFlow"><b>Compatible</b> → Direct Stream <span>·</span> <b>Incompatible video</b> → selected HW/software encoder <span>·</span> <b>Incompatible audio</b> → selected audio codec <span>·</span> <b>HW unavailable</b> → software fallback</div>`;
      const grid = policyGroup.querySelector('.configGroupGrid');
      policyGroup.insertBefore(intro, grid || null);
    }
    refreshPolicyPreview();
  }

  installDashboardPanel();
  installConfigurationControls();
  loadTranscoding();
  setInterval(loadTranscoding, 5000);
})();
