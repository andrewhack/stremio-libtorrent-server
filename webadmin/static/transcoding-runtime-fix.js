(() => {
  const style = document.createElement('style');
  style.textContent = `
    .transcodeRuntimeAlert{padding:11px 13px;border-radius:9px;border:1px solid rgba(232,207,105,.28);font-size:11px;line-height:1.5}
    .transcodeRuntimeAlert.ok{border-color:#3da864;background:#123f25;color:#bdf5cf}
    .transcodeRuntimeAlert.warn{border-color:#b58a2f;background:#4b3b12;color:#ffe89a}
    .transcodeRuntimeAlert.bad{border-color:#b94450;background:#4a1319;color:#ffc3c8}
    .transcodeRuntimeAlert b{display:block;margin-bottom:3px;color:inherit}
    .transcodeRuntimeMeta{margin-top:4px;opacity:.82;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
    .badge.runtimeBad{border-color:#b94450!important;background:#4a1319!important;color:#ffc3c8!important}
    .badge.runtimeWarn{border-color:#b58a2f!important;background:#4b3b12!important;color:#ffe89a!important}
  `;
  document.head.appendChild(style);

  const getConfig = name => {
    const input = document.querySelector(`[data-config="${name}"]`);
    if (!input) return undefined;
    return input.type === 'checkbox' ? input.checked : input.value;
  };

  function forcedTarget(mode, hw, preferred, fallback) {
    if (mode === 'software') return fallback;
    if (mode === 'h264') {
      if (hw === 'vaapi') return 'h264_vaapi';
      if (hw === 'nvenc') return 'h264_nvenc';
      if (hw === 'cpu') return fallback;
      return 'auto-selected H.264 encoder';
    }
    if (mode === 'hevc') {
      if (hw === 'vaapi') return 'hevc_vaapi';
      if (hw === 'nvenc') return 'hevc_nvenc';
      if (hw === 'cpu') return 'libx265';
      return 'auto-selected HEVC encoder';
    }
    return preferred;
  }

  function correctPolicyPreview() {
    const preview = document.getElementById('transcodePolicyPreview');
    if (!preview) return;
    const mode = String(getConfig('transcoding_mode') || 'auto').toLowerCase();
    const hw = String(getConfig('transcoding_hwaccel') || 'auto').toLowerCase();
    const preferred = String(getConfig('transcoding_video_codec') || 'h264_vaapi');
    const quality = getConfig('transcoding_video_quality') || 22;
    const audio = String(getConfig('transcoding_audio_codec') || 'aac');
    const bitrate = String(getConfig('transcoding_audio_bitrate') || '192k');
    const fallback = String(getConfig('transcoding_fallback_codec') || 'libx264');
    const directVideo = String(getConfig('transcoding_direct_video_codecs') || 'h264');
    const directAudio = String(getConfig('transcoding_direct_audio_codecs') || 'aac,mp3,ac3');

    if (mode === 'copy') {
      preview.textContent = 'COPY: wrapper policy is disabled; upstream FFmpeg codec decisions pass through unchanged.';
      return;
    }
    if (mode === 'auto') {
      preview.textContent = `AUTO: video [${directVideo}] stays Direct Stream; other video copy decisions → ${preferred} (${hw}, quality ${quality}). Audio [${directAudio}] stays copy; other audio copy decisions → ${audio} ${bitrate}. Existing upstream transcodes are not rewritten; fallback → ${fallback}.`;
      return;
    }
    const target = forcedTarget(mode, hw, preferred, fallback);
    preview.textContent = `${mode.toUpperCase()}: every video stream the core marked as copy is forced → ${target} (quality ${quality}); video already selected for upstream transcoding is left unchanged. Audio policy only changes audio copy decisions → ${audio} ${bitrate}; fallback → ${fallback}.`;
  }

  function ensureDashboardAlert() {
    const panel = document.getElementById('transcodingPanel');
    if (!panel) return null;
    let alert = document.getElementById('tdRuntimeAlert');
    if (!alert) {
      alert = document.createElement('div');
      alert.id = 'tdRuntimeAlert';
      alert.className = 'transcodeRuntimeAlert';
      const inner = panel.querySelector('.inner');
      if (inner) inner.insertBefore(alert, inner.firstChild);
    }
    return alert;
  }

  function ensureConfigAlert() {
    const intro = document.querySelector('.transcodePolicyIntro');
    if (!intro) return null;
    let alert = document.getElementById('transcodeConfigRuntime');
    if (!alert) {
      alert = document.createElement('div');
      alert.id = 'transcodeConfigRuntime';
      alert.className = 'transcodeRuntimeAlert';
      alert.style.marginTop = '10px';
      intro.appendChild(alert);
    }
    return alert;
  }

  function runtimeHtml(data) {
    const runtime = data.runtime || {};
    const hw = data.hardware || {};
    const policy = data.policy || {};
    const server = runtime.serverVersion || 'unknown';
    const target = runtime.targetServerVersion || 'current release';
    if (!runtime.ready) {
      return {
        level: 'bad',
        title: 'Streaming-server runtime update required',
        body: runtime.message || 'The FFmpeg policy wrapper is not active.',
        meta: `running ${server} · required ${target}`
      };
    }
    if (String(policy.transcoding_hwaccel || '').toLowerCase() === 'vaapi' && !hw.vaapiDevicePresent) {
      return {
        level: 'warn',
        title: 'Policy active, but VAAPI device is not mounted',
        body: `Expose ${hw.vaapiDevice || '/dev/dri/renderD128'} with compose.vaapi.yaml (or the GPU overlay) before using VAAPI.`,
        meta: `server ${server} · wrapper active`
      };
    }
    return {
      level: 'ok',
      title: 'Transcoding policy runtime ready',
      body: runtime.message || 'FFmpeg policy wrapper is active.',
      meta: `server ${server} · FFmpeg ${hw.probedFfmpegBinary || runtime.ffmpegBinary || 'detected'}`
    };
  }

  function renderRuntime(data) {
    if (!data || !data.available) return;
    const info = runtimeHtml(data);
    const html = `<b>${info.title}</b>${info.body}<div class="transcodeRuntimeMeta">${info.meta}</div>`;
    const dashboardAlert = ensureDashboardAlert();
    if (dashboardAlert) {
      dashboardAlert.className = `transcodeRuntimeAlert ${info.level}`;
      dashboardAlert.innerHTML = html;
    }
    const configAlert = ensureConfigAlert();
    if (configAlert) {
      configAlert.className = `transcodeRuntimeAlert ${info.level}`;
      configAlert.innerHTML = html;
    }

    const badge = document.getElementById('tdPolicyState');
    if (badge) {
      badge.classList.remove('runtimeBad', 'runtimeWarn');
      if (info.level === 'bad') {
        badge.classList.add('runtimeBad');
        badge.textContent = '● UPDATE SERVER';
      } else if (info.level === 'warn') {
        badge.classList.add('runtimeWarn');
        badge.textContent = '● VAAPI MISSING';
      }
    }

    const engines = data.active?.engines || [];
    const load = document.getElementById('tdEncoderLoad');
    if (load && engines.includes('cpu-audio')) {
      load.textContent = 'CPU · audio transcode';
    }
    const decision = document.getElementById('tdDecision');
    if (decision && data.runtime && !data.runtime.ready && !data.latestDecision) {
      decision.textContent = 'Policy wrapper inactive — current FFmpeg command is upstream-only';
    }
    const summary = document.getElementById('tdSummary');
    if (summary && data.policySummary) summary.textContent = data.policySummary;
    correctPolicyPreview();
  }

  async function refreshRuntime() {
    try {
      const response = await fetch('/api/transcoding/status', {cache: 'no-store'});
      if (!response.ok) return;
      renderRuntime(await response.json());
    } catch (_) {
      // The base dashboard already exposes an unavailable state; do not mask it.
    }
  }

  document.addEventListener('input', () => setTimeout(correctPolicyPreview, 0));
  document.addEventListener('change', () => setTimeout(correctPolicyPreview, 0));
  document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
    setTimeout(() => {
      correctPolicyPreview();
      refreshRuntime();
    }, 50);
  }));

  setTimeout(refreshRuntime, 150);
  setInterval(refreshRuntime, 2000);
})();
