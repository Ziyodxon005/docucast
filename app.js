'use strict';

/* ─────────────────────────────────────────────────────────
   DocuCast v2.0 — Frontend Logic
   API kalitlari .env da — foydalanuvchidan so'ralmaydi
   ───────────────────────────────────────────────────────── */

// ── State ────────────────────────────────────────────────
let selectedFile = null;
let selectedLanguage = 'uz';
let currentJobId = null;
let currentToken = null;
let sseSource = null;
let scriptPdfLoaded = false;
let currentScript = [];
let processViewMode = 'live';
let resultViewMode = 'pdf';
let vizBars = [];
let isSeeking = false;

const LANGUAGE_LABELS = {
  uz: "O'zbekcha",
  en: 'English',
  ru: 'Русский',
  de: 'Deutsch',
};

const LANGUAGE_GENITIVE = {
  uz: "o'zbek tilidagi",
  en: 'ingliz tilidagi',
  ru: 'rus tilidagi',
  de: 'nemis tilidagi',
};

// ── DOM helpers ──────────────────────────────────────────
const $ = id => document.getElementById(id);

// Upload view
const langGrid = $('langGrid');
const selectedLangLabel = $('selectedLangLabel');
const dropZone = $('dropZone');
const fileInput = $('fileInput');
const dropContent = $('dropContent');
const fileSelected = $('fileSelected');
const fileName = $('fileName');
const fileSize = $('fileSize');
const fileRemove = $('fileRemove');
const btnGenerate = $('btnGenerate');
const ttsLabel = $('ttsLabel');

// Process view
const progressFill = $('progressFill');
const progressPct = $('progressPct');
const logBox = $('logBox');
const processPdfPlaceholder = $('processPdfPlaceholder');
const processPlaceholderTitle = $('processPlaceholderTitle');
const processPlaceholderSub = $('processPlaceholderSub');
const processLivePaper = $('processLivePaper');
const processPdfFrame = $('processPdfFrame');
const processModeTabs = $('processModeTabs');
const pdfStatusBadge = $('pdfStatusBadge');
const btnProcessPdfDownload = $('btnProcessPdfDownload');

// Result view
const audioEl = $('audioElement');
const btnPlay = $('btnPlay');
const iconPlay = $('iconPlay');
const iconPause = $('iconPause');
const btnRewind = $('btnRewind');
const btnForward = $('btnForward');
const currentTimeEl = $('currentTime');
const totalTimeEl = $('totalTime');
const seekTrack = $('seekTrack');
const seekFill = $('seekFill');
const seekThumb = $('seekThumb');
const volumeSlider = $('volumeSlider');
const btnDownload = $('btnDownload');
const btnDownloadPdf = $('btnDownloadPdf');
const btnResultPdfIcon = $('btnResultPdfIcon');
const resultPdfFrame = $('resultPdfFrame');
const resultLivePaper = $('resultLivePaper');
const resultModeTabs = $('resultModeTabs');
const resultPdfBadge = $('resultPdfBadge');
const btnNew = $('btnNew');


// ════════════════════════════════════════════════════════
// VIEWS
// ════════════════════════════════════════════════════════
function showView(name) {
  ['viewUpload', 'viewProcess', 'viewResult'].forEach(id => {
    const el = $(id);
    if (el) el.classList.toggle('active', id === 'view' + name.charAt(0).toUpperCase() + name.slice(1));
  });
  const mainEl = document.querySelector('.main');
  if (mainEl) mainEl.classList.toggle('wide-mode', name === 'process' || name === 'result');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}


// ════════════════════════════════════════════════════════
// TOAST
// ════════════════════════════════════════════════════════
function toast(msg, type = 'info', ms = 4500) {
  const icons = { success: '✓', error: '✕', info: 'ℹ', warn: '⚠' };
  const wrap = $('toastContainer');
  if (!wrap) return;
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<strong>${icons[type] || '●'}</strong><span>${msg}</span>`;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.transition = '0.3s ease';
    el.style.opacity = '0';
    el.style.transform = 'translateX(110%)';
    setTimeout(() => el.remove(), 320);
  }, ms);
}


// ════════════════════════════════════════════════════════
// LANGUAGE SELECTION
// ════════════════════════════════════════════════════════
function setLanguage(lang) {
  if (!LANGUAGE_LABELS[lang]) lang = 'uz';
  selectedLanguage = lang;
  try { localStorage.setItem('docucast_lang', lang); } catch {}
  if (selectedLangLabel) selectedLangLabel.textContent = LANGUAGE_LABELS[lang];
  if (langGrid) {
    langGrid.querySelectorAll('.lang-btn').forEach(btn => {
      const isSelected = btn.dataset.lang === lang;
      btn.classList.toggle('active', isSelected);
      btn.setAttribute('aria-checked', String(isSelected));
    });
  }
}

if (langGrid) {
  const savedLang = (() => { try { return localStorage.getItem('docucast_lang'); } catch { return null; } })();
  setLanguage(savedLang || 'uz');

  langGrid.addEventListener('click', e => {
    const btn = e.target.closest('.lang-btn');
    if (!btn) return;
    setLanguage(btn.dataset.lang);
  });
}


// ════════════════════════════════════════════════════════
// FILE HANDLING
// ════════════════════════════════════════════════════════
function formatBytes(b) {
  if (!b) return '0 B';
  const k = 1024, s = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(b) / Math.log(k));
  return `${(b / Math.pow(k, i)).toFixed(1)} ${s[i]}`;
}

function acceptFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    toast('Faqat PDF formatida fayl qabul qilinadi!', 'error');
    return;
  }
  if (file.size > 100 * 1024 * 1024) {
    toast('Fayl hajmi 100 MB dan oshmasligi kerak!', 'error');
    return;
  }
  selectedFile = file;
  dropZone.classList.add('has-file');
  dropContent.classList.add('hidden');
  fileSelected.classList.remove('hidden');
  fileName.textContent = file.name;
  fileSize.textContent = formatBytes(file.size);
}

function clearFile() {
  selectedFile = null;
  dropZone.classList.remove('has-file');
  dropContent.classList.remove('hidden');
  fileSelected.classList.add('hidden');
  fileInput.value = '';
}

// Drag & drop
let isDragging = false;
dropZone.addEventListener('click', e => {
  if (fileRemove && (e.target === fileRemove || fileRemove.contains(e.target))) return;
  fileInput.click();
});
dropZone.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener('change', e => {
  if (e.target.files[0]) acceptFile(e.target.files[0]);
});
fileRemove.addEventListener('click', e => {
  e.stopPropagation();
  clearFile();
});
dropZone.addEventListener('dragover', e => {
  e.preventDefault();
  if (!isDragging) { isDragging = true; dropZone.classList.add('drag-over'); }
});
dropZone.addEventListener('dragleave', () => {
  isDragging = false;
  dropZone.classList.remove('drag-over');
});
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  isDragging = false;
  dropZone.classList.remove('drag-over');
  if (e.dataTransfer.files[0]) acceptFile(e.dataTransfer.files[0]);
});


// ════════════════════════════════════════════════════════
// SERVER HEALTH CHECK
// ════════════════════════════════════════════════════════
async function checkHealth() {
  const statusEl = $('serverStatus');
  const statusTxt = $('statusText');
  try {
    const r = await fetch('/api/health', { signal: AbortSignal.timeout(5000) });
    const d = await r.json();
    if (d.status === 'ok') {
      if (d.api_configured) {
        statusEl.className = 'server-status online';
        statusTxt.textContent = d.api_keys_count > 1 ? `Tayyor (${d.api_keys_count} kalit)` : 'Tayyor';
      } else {
        statusEl.className = 'server-status warning';
        statusTxt.textContent = 'API kalit kerak';
        toast('.env faylida GEMINI_API_KEY sozlanmagan!', 'warn', 8000);
      }
      if (ttsLabel && d.tts) {
        ttsLabel.textContent = d.tts === 'google-cloud'
          ? 'TTS: Google Cloud (Premium)'
          : 'TTS: Edge Neural (Bepul)';
      }
    }
  } catch {
    if (statusEl) statusEl.className = 'server-status offline';
    if (statusTxt) statusTxt.textContent = 'Ulanmadi';
    toast('Backend serverga ulanib bo\'lmadi. python app.py ni ishga tushiring.', 'error', 8000);
  }
}


// ════════════════════════════════════════════════════════
// GENERATE
// ════════════════════════════════════════════════════════
function setBtnLoading(on) {
  btnGenerate.disabled = on;
  const textEl = btnGenerate.querySelector('.btn-text');
  const arrow = btnGenerate.querySelector('.btn-arrow');
  if (textEl) textEl.textContent = on ? 'Yuklanmoqda...' : 'Podkast Yaratish';
  if (arrow) arrow.style.opacity = on ? '0' : '0.7';
}

btnGenerate.addEventListener('click', async () => {
  if (!selectedFile) {
    toast('Avval PDF faylni tanlang!', 'error');
    dropZone.style.boxShadow = '0 0 0 3px rgba(239,68,68,0.4)';
    setTimeout(() => dropZone.style.boxShadow = '', 2000);
    return;
  }

  const fd = new FormData();
  fd.append('pdf', selectedFile);
  fd.append('language', selectedLanguage);

  setBtnLoading(true);
  try {
    const res = await fetch('/api/generate', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Xato: ${res.status}`);

    currentJobId = data.job_id;
    currentToken = data.token;

    showView('process');
    resetProcessView();
    startSSE(currentJobId);
    toast('Jarayon boshlandi!', 'success');
  } catch (err) {
    toast(err.message, 'error', 7000);
    setBtnLoading(false);
  }
});


// ════════════════════════════════════════════════════════
// PROCESSING
// ════════════════════════════════════════════════════════
async function downloadScriptPdf(jobId) {
  if (!jobId || !currentToken) return;
  const url = `/api/download-pdf/${jobId}?token=${encodeURIComponent(currentToken)}`;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      let errMsg = 'Ssenariy PDF yuklab olishda xato';
      try {
        const errData = await res.json();
        if (errData.error) errMsg = errData.error;
      } catch {}
      throw new Error(errMsg);
    }
    const blob = await res.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = 'docucast_script.pdf';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 4000);
    toast('Ssenariy PDF yuklab olindi!', 'success');
  } catch (err) {
    toast(err.message || 'PDF yuklab olib bo\'lmadi', 'error');
  }
}

function renderScriptPaper(container, script, langCode) {
  if (!container || !Array.isArray(script) || !script.length) return;
  const langLabel = LANGUAGE_LABELS[langCode] || "O'zbekcha";
  container.innerHTML = '';

  const sheet = document.createElement('div');
  sheet.className = 'pdf-paper';

  const header = document.createElement('div');
  header.className = 'paper-header';
  const title = document.createElement('div');
  title.className = 'paper-title';
  title.textContent = 'DocuCast — Podkast Ssenariysi';
  const meta = document.createElement('div');
  meta.className = 'paper-meta';
  meta.textContent = `Til: ${langLabel} · ${script.length} ta dialog · Shaxnoza & Mustafo`;
  header.append(title, meta);
  sheet.appendChild(header);

  script.forEach((turn, idx) => {
    const speaker = (turn.speaker || 'Shaxnoza').trim();
    const text = (turn.text || '').trim();
    const isHost = speaker.toLowerCase() === 'shaxnoza';

    const card = document.createElement('div');
    card.className = `paper-turn ${isHost ? 'host' : 'expert'}`;
    card.dataset.turnIdx = String(idx);

    const top = document.createElement('div');
    top.className = 'paper-turn-top';

    const spEl = document.createElement('span');
    spEl.className = 'paper-speaker';
    spEl.textContent = `#${String(idx + 1).padStart(2, '0')}  ${speaker}`;

    const stateEl = document.createElement('span');
    stateEl.className = 'paper-turn-state';
    top.append(spEl, stateEl);

    const body = document.createElement('div');
    body.className = 'paper-text';
    body.textContent = text;

    card.append(top, body);
    sheet.appendChild(card);
  });

  container.appendChild(sheet);
}

function updateActiveTurnUI(container, currentTurn) {
  if (!container) return;
  const cards = container.querySelectorAll('.paper-turn');
  let activeCard = null;
  cards.forEach(card => {
    const idx = Number(card.dataset.turnIdx);
    const stateEl = card.querySelector('.paper-turn-state');
    const isActive = idx === currentTurn;
    const isDone = currentTurn >= 0 && idx < currentTurn;
    card.classList.toggle('speaking', isActive);
    card.classList.toggle('spoken', isDone);
    if (stateEl) {
      if (isActive) stateEl.textContent = "🎙️ O'qilmoqda...";
      else if (isDone) stateEl.textContent = '✓';
      else stateEl.textContent = '';
    }
    if (isActive) activeCard = card;
  });
  if (activeCard && processViewMode === 'live') {
    activeCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function applyProcessMode(mode) {
  processViewMode = mode;
  if (processModeTabs) {
    processModeTabs.querySelectorAll('.pdf-tab-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.mode === mode);
    });
  }
  const hasScript = currentScript.length > 0;
  if (processLivePaper) processLivePaper.classList.toggle('hidden', !(hasScript && mode === 'live'));
  if (processPdfFrame) processPdfFrame.classList.toggle('hidden', !(scriptPdfLoaded && mode === 'pdf'));
}

function applyResultMode(mode) {
  resultViewMode = mode;
  if (resultModeTabs) {
    resultModeTabs.querySelectorAll('.pdf-tab-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.mode === mode);
    });
  }
  if (resultPdfFrame) resultPdfFrame.classList.toggle('hidden', mode !== 'pdf');
  if (resultLivePaper) resultLivePaper.classList.toggle('hidden', mode !== 'live');
}

if (processModeTabs) {
  processModeTabs.addEventListener('click', e => {
    const btn = e.target.closest('.pdf-tab-btn');
    if (btn) applyProcessMode(btn.dataset.mode);
  });
}
if (resultModeTabs) {
  resultModeTabs.addEventListener('click', e => {
    const btn = e.target.closest('.pdf-tab-btn');
    if (btn) applyResultMode(btn.dataset.mode);
  });
}

function loadScriptPdfPreview(jobId, turns) {
  if (!jobId || !currentToken || scriptPdfLoaded) return;
  scriptPdfLoaded = true;
  const pdfUrl = `/api/script-pdf/${jobId}?token=${encodeURIComponent(currentToken)}#view=FitH`;

  if (processPdfFrame) processPdfFrame.src = pdfUrl;
  if (processModeTabs) processModeTabs.classList.remove('hidden');
  if (processPdfPlaceholder) processPdfPlaceholder.classList.add('hidden');
  if (btnProcessPdfDownload) {
    btnProcessPdfDownload.classList.remove('hidden');
    btnProcessPdfDownload.onclick = () => downloadScriptPdf(jobId);
  }
  if (resultPdfBadge) {
    resultPdfBadge.textContent = turns ? `${turns} ta dialog` : 'Tayyor';
  }
  applyProcessMode(processViewMode);
}

function resetProcessView() {
  scriptPdfLoaded = false;
  currentScript = [];
  processViewMode = 'live';
  progressFill.style.width = '0%';
  progressPct.textContent = '0%';
  logBox.innerHTML = '<div class="log-line">Jarayon boshlanmoqda...</div>';
  ['step1', 'step2', 'step3'].forEach(id => {
    const el = $(id);
    if (el) el.classList.remove('active', 'done');
  });
  if (processPlaceholderTitle) {
    processPlaceholderTitle.textContent = selectedFile
      ? `${selectedFile.name} o'qilmoqda...`
      : 'Ssenariy tayyorlanmoqda...';
  }
  if (processPlaceholderSub) {
    processPlaceholderSub.textContent = "AI hujjatni o'qib, ssenariyni yozishi bilan bu yerda jonli ko'rinadi va hozir qaysi qism o'qilayotgani ajratib ko'rsatiladi.";
  }
  if (processPdfPlaceholder) processPdfPlaceholder.classList.remove('hidden');
  if (processLivePaper) { processLivePaper.innerHTML = ''; processLivePaper.classList.add('hidden'); }
  if (processPdfFrame) { processPdfFrame.src = ''; processPdfFrame.classList.add('hidden'); }
  if (processModeTabs) processModeTabs.classList.add('hidden');
  if (resultPdfFrame) resultPdfFrame.src = '';
  if (resultLivePaper) { resultLivePaper.innerHTML = ''; resultLivePaper.classList.add('hidden'); }
  if (pdfStatusBadge) {
    pdfStatusBadge.textContent = 'Yozilmoqda...';
    pdfStatusBadge.classList.remove('ready');
  }
  if (btnProcessPdfDownload) btnProcessPdfDownload.classList.add('hidden');
}

function updateSteps(pct) {
  const s1 = $('step1'), s2 = $('step2'), s3 = $('step3');
  if (!s1) return;
  if (pct < 15) {
    s1.classList.add('active'); s1.classList.remove('done');
    s2.classList.remove('active', 'done');
    s3.classList.remove('active', 'done');
  } else if (pct < 38) {
    s1.classList.remove('active'); s1.classList.add('done');
    s2.classList.add('active'); s2.classList.remove('done');
    s3.classList.remove('active', 'done');
  } else if (pct < 92) {
    s1.classList.add('done'); s1.classList.remove('active');
    s2.classList.add('done'); s2.classList.remove('active');
    s3.classList.add('active'); s3.classList.remove('done');
  } else {
    s1.classList.add('done'); s1.classList.remove('active');
    s2.classList.add('done'); s2.classList.remove('active');
    s3.classList.add('done'); s3.classList.remove('active');
  }
}

function addLog(msg) {
  const line = document.createElement('div');
  line.className = 'log-line';
  line.textContent = msg;
  logBox.appendChild(line);
  while (logBox.children.length > 8) logBox.removeChild(logBox.firstChild);
  logBox.scrollTop = logBox.scrollHeight;
}

function startSSE(jobId) {
  if (sseSource) { sseSource.close(); sseSource = null; }
  sseSource = new EventSource(`/api/status/${jobId}`);

  sseSource.onmessage = e => {
    try {
      const job = JSON.parse(e.data);
      const pct = Math.min(job.progress || 0, 100);
      progressFill.style.width = `${pct}%`;
      progressPct.textContent = `${pct}%`;
      updateSteps(pct);
      if (job.message) {
        addLog(job.message);
        if (!currentScript.length && processPlaceholderSub) {
          processPlaceholderSub.textContent = job.message;
        }
      }
      if (Array.isArray(job.script) && job.script.length && !currentScript.length) {
        currentScript = job.script;
        renderScriptPaper(processLivePaper, currentScript, job.language || selectedLanguage);
        renderScriptPaper(resultLivePaper, currentScript, job.language || selectedLanguage);
        if (processPdfPlaceholder) processPdfPlaceholder.classList.add('hidden');
        applyProcessMode(processViewMode);
      }
      if (job.script_ready) loadScriptPdfPreview(jobId, job.script_turns);
      if (currentScript.length && typeof job.current_turn === 'number') {
        updateActiveTurnUI(processLivePaper, job.current_turn);
        if (pdfStatusBadge) {
          const total = currentScript.length;
          if (job.current_turn >= total) {
            pdfStatusBadge.textContent = `Tayyor (${total})`;
            pdfStatusBadge.classList.add('ready');
          } else if (job.current_turn >= 0) {
            pdfStatusBadge.textContent = `O'qilmoqda: ${job.current_turn + 1}/${total}`;
          }
        }
      }

      if (job.status === 'done') {
        sseSource.close(); sseSource = null;
        setTimeout(() => showResultView(jobId), 500);
      }
      if (job.status === 'error') {
        sseSource.close(); sseSource = null;
        addLog(`❌ ${job.message}`);
        toast(job.message, 'error', 9000);
        setTimeout(() => { setBtnLoading(false); showView('upload'); }, 3000);
      }
    } catch (err) { console.error('SSE parse:', err); }
  };
  sseSource.onerror = () => console.warn('SSE reconnecting...');
}


// ════════════════════════════════════════════════════════
// RESULT / PLAYER
// ════════════════════════════════════════════════════════
function showResultView(jobId) {
  showView('result');
  buildVizBars();

  const resultSub = $('resultSub');
  if (resultSub) {
    const langText = LANGUAGE_GENITIVE[selectedLanguage] || '';
    resultSub.textContent = `AI yaratgan ${langText} audio podkastingizni eshiting`;
  }

  audioEl.src = `/api/stream/${jobId}?token=${encodeURIComponent(currentToken)}`;
  audioEl.load();

  if (resultPdfFrame) {
    resultPdfFrame.src = `/api/script-pdf/${jobId}?token=${encodeURIComponent(currentToken)}#view=FitH`;
  }
  if (currentScript.length && resultLivePaper) {
    if (!resultLivePaper.children.length) {
      renderScriptPaper(resultLivePaper, currentScript, selectedLanguage);
    }
    updateActiveTurnUI(resultLivePaper, currentScript.length);
  }
  applyResultMode('pdf');

  btnDownload.onclick = () => {
    const a = document.createElement('a');
    a.href = `/api/download/${jobId}?token=${encodeURIComponent(currentToken)}`;
    a.download = 'docucast_podcast.mp3';
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast('Yuklab olish boshlandi!', 'success');
  };

  if (btnDownloadPdf) btnDownloadPdf.onclick = () => downloadScriptPdf(jobId);
  if (btnResultPdfIcon) btnResultPdfIcon.onclick = () => downloadScriptPdf(jobId);

  toast('🎉 Podkastingiz tayyor!', 'success', 5000);
}

function buildVizBars() {
  const container = $('playerViz');
  if (!container) return;
  container.innerHTML = '';
  vizBars = [];
  const count = window.innerWidth < 500 ? 50 : 90;
  for (let i = 0; i < count; i++) {
    const bar = document.createElement('div');
    bar.className = 'viz-bar';
    bar.style.height = `${6 + Math.random() * 52}px`;
    container.appendChild(bar);
    vizBars.push(bar);
  }
  container.addEventListener('click', e => {
    const rect = container.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    if (audioEl.duration) audioEl.currentTime = frac * audioEl.duration;
  });
}

function fmtTime(s) {
  if (isNaN(s) || !isFinite(s)) return '0:00';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function updateSeekUI(frac) {
  const pct = `${frac * 100}%`;
  seekFill.style.width = pct;
  seekThumb.style.left = pct;
  const idx = Math.floor(frac * vizBars.length);
  vizBars.forEach((b, i) => b.classList.toggle('played', i < idx));
}

// Audio events
audioEl.addEventListener('loadedmetadata', () => {
  totalTimeEl.textContent = fmtTime(audioEl.duration);
});
audioEl.addEventListener('timeupdate', () => {
  const frac = audioEl.currentTime / (audioEl.duration || 1);
  currentTimeEl.textContent = fmtTime(audioEl.currentTime);
  if (!isSeeking) updateSeekUI(frac);
});
audioEl.addEventListener('play', () => { iconPlay.classList.add('hidden'); iconPause.classList.remove('hidden'); });
audioEl.addEventListener('pause', () => { iconPlay.classList.remove('hidden'); iconPause.classList.add('hidden'); });
audioEl.addEventListener('ended', () => {
  iconPlay.classList.remove('hidden'); iconPause.classList.add('hidden');
  audioEl.currentTime = 0; updateSeekUI(0);
});

// Seek bar (click + drag)
function doSeek(clientX) {
  const rect = seekTrack.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  updateSeekUI(frac);
  if (audioEl.duration) audioEl.currentTime = frac * audioEl.duration;
}
seekTrack.addEventListener('mousedown', e => { isSeeking = true; doSeek(e.clientX); });
document.addEventListener('mousemove', e => { if (isSeeking) doSeek(e.clientX); });
document.addEventListener('mouseup', () => { isSeeking = false; });
seekTrack.addEventListener('touchstart', e => doSeek(e.touches[0].clientX), { passive: true });
seekTrack.addEventListener('touchmove', e => doSeek(e.touches[0].clientX), { passive: true });

// Controls
btnPlay.addEventListener('click', () => audioEl.paused ? audioEl.play() : audioEl.pause());
btnRewind.addEventListener('click', () => { audioEl.currentTime = Math.max(0, audioEl.currentTime - 15); });
btnForward.addEventListener('click', () => { audioEl.currentTime = Math.min(audioEl.duration || 0, audioEl.currentTime + 15); });
volumeSlider.addEventListener('input', () => { audioEl.volume = volumeSlider.value; });

// New podcast
btnNew.addEventListener('click', () => {
  if (sseSource) { sseSource.close(); sseSource = null; }
  audioEl.pause(); audioEl.src = '';
  if (processPdfFrame) processPdfFrame.src = '';
  if (resultPdfFrame) resultPdfFrame.src = '';
  selectedFile = currentJobId = currentToken = null;
  scriptPdfLoaded = false;
  currentScript = [];
  clearFile();
  setBtnLoading(false);
  showView('upload');
  toast('Yangi podkast uchun tayyor!', 'info');
});


// ════════════════════════════════════════════════════════
// KEYBOARD SHORTCUTS (result view only)
// ════════════════════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
  const inResult = $('viewResult') && $('viewResult').classList.contains('active');
  if (!inResult) return;
  if (e.code === 'Space') { e.preventDefault(); btnPlay.click(); }
  if (e.code === 'ArrowLeft') { e.preventDefault(); btnRewind.click(); }
  if (e.code === 'ArrowRight') { e.preventDefault(); btnForward.click(); }
  if (e.code === 'ArrowUp') { e.preventDefault(); volumeSlider.value = Math.min(1, +volumeSlider.value + 0.1); audioEl.volume = +volumeSlider.value; }
  if (e.code === 'ArrowDown') { e.preventDefault(); volumeSlider.value = Math.max(0, +volumeSlider.value - 0.1); audioEl.volume = +volumeSlider.value; }
});


// ════════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════════
checkHealth();
