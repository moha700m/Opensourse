const form = document.querySelector('#job-form');
const urlInput = document.querySelector('#video-url');
const videoInput = document.querySelector('#video-file');
const cookiesInput = document.querySelector('#youtube-cookies');
const resultsSection = document.querySelector('#results-section');
const progressBar = document.querySelector('#progress-bar');
const clipsGrid = document.querySelector('#clips-grid');
const fallbackButton = document.querySelector('#upload-fallback');
const ACTIVE_KEY = 'openshorts.activeJob';
const UPLOAD_KEY = 'openshorts.upload';
const config = {max_upload_bytes: 1024 ** 3, upload_chunk_bytes: 8 * 1024 ** 2};
let sourceMode = 'upload';
let activeJobId = null;
let pollTimer = null;
let busy = false;

const readSaved = (key) => { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } };
const save = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} };
const forget = (key) => { try { localStorage.removeItem(key); } catch {} };
const delay = (milliseconds) => new Promise(resolve => setTimeout(resolve, milliseconds));
const youtubeBlocked = (message) => /sign in to confirm|not a bot|--cookies/i.test(message || '');
const explainError = (message) => youtubeBlocked(message)
  ? 'طلب YouTube تسجيل الدخول للتحقق ومنع تنزيل الرابط من الخادم. اختر ملف الفيديو من الجوال لبدء التحويل.'
  : message || 'تعذر تجهيز هذا الفيديو.';

const api = async (path, options = {}) => {
  const headers = {...(options.headers || {})};
  if (!(options.body instanceof FormData)) headers['Content-Type'] ??= options.body instanceof Blob ? 'application/octet-stream' : 'application/json';
  const response = await fetch(path, {...options, headers});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof body.detail === 'string' ? body.detail : 'تعذر إكمال الطلب. تحقق من اتصالك وحاول مجددًا.');
    error.status = response.status;
    error.detail = body.detail;
    throw error;
  }
  return body;
};

const setMode = (mode) => {
  sourceMode = mode;
  for (const kind of ['upload', 'youtube']) {
    const selected = kind === mode;
    document.querySelector(`#${kind}-tab`).classList.toggle('active', selected);
    document.querySelector(`#${kind}-tab`).setAttribute('aria-selected', String(selected));
    document.querySelector(`#${kind}-panel`).classList.toggle('hidden', !selected);
  }
  urlInput.required = mode === 'youtube';
  videoInput.required = mode === 'upload';
  urlInput.disabled = mode !== 'youtube';
  cookiesInput.disabled = mode !== 'youtube';
  videoInput.disabled = mode !== 'upload';
};

const showResults = () => {
  resultsSection.classList.remove('hidden');
  resultsSection.scrollIntoView({behavior: 'smooth', block: 'start'});
};
const showError = (message) => {
  const error = document.querySelector('#error-card');
  error.textContent = explainError(message);
  error.classList.remove('hidden');
  fallbackButton.classList.toggle('hidden', !youtubeBlocked(message));
};
const formatTime = (seconds) => `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;

const renderClips = (clips) => {
  clipsGrid.replaceChildren();
  for (const clip of clips) {
    const card = document.createElement('article');
    card.className = 'clip-card';
    const video = document.createElement('video');
    video.className = 'clip-video';
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.src = clip.download_url;
    const details = document.createElement('div');
    details.className = 'clip-details';
    const meta = document.createElement('div');
    meta.className = 'clip-meta';
    meta.textContent = `${formatTime(clip.start)} — ${formatTime(clip.end)} · ${Math.round(clip.score || 0)}/10`;
    const title = document.createElement('h3');
    title.textContent = clip.title || 'لحظة مميزة';
    const reason = document.createElement('p');
    reason.textContent = clip.reason || clip.hook || 'مقطع جاهز للنشر.';
    const actions = document.createElement('div');
    actions.className = 'clip-actions';
    const download = document.createElement('a');
    download.href = clip.download_url;
    download.download = `${clip.id}.mp4`;
    download.textContent = 'حفظ الفيديو ↓';
    const subtitles = document.createElement('a');
    subtitles.href = clip.subtitle_url;
    subtitles.download = `${clip.id}.srt`;
    subtitles.className = 'secondary';
    subtitles.textContent = 'ملف الترجمة SRT';
    actions.append(download, subtitles);
    details.append(meta, title, reason, actions);
    card.append(video, details);
    clipsGrid.append(card);
  }
};

const setProgress = (job) => {
  document.querySelector('#progress-stage').textContent = job.stage || 'جارٍ تجهيز الفيديو';
  document.querySelector('#progress-percent').textContent = `${job.progress || 0}%`;
  progressBar.style.width = `${job.progress || 0}%`;
  if (job.title) document.querySelector('#processing-title').textContent = job.title;
  if (job.status === 'completed') {
    document.querySelector('#results-title').innerHTML = 'جاهزة <em>للنشر.</em>';
    document.querySelector('#processing-elapsed').textContent = 'اكتملت المعالجة';
    const method = document.querySelector('#method-note');
    method.textContent = `طريقة اختيار اللحظات: ${job.selection_method || 'تحليل المحتوى'}`;
    method.classList.remove('hidden');
    renderClips(job.clips || []);
    clearTimeout(pollTimer);
  } else if (job.status === 'failed') {
    clearTimeout(pollTimer);
    document.querySelector('#results-title').innerHTML = 'نحتاج <em>محاولة أخرى.</em>';
    showError(job.error);
  }
};

const poll = async () => {
  clearTimeout(pollTimer);
  const requestedId = activeJobId;
  if (!requestedId || busy) return;
  try {
    const job = await api(`/api/jobs/${requestedId}`);
    if (requestedId !== activeJobId) return;
    setProgress(job);
    if (job.status === 'queued' || job.status === 'processing') pollTimer = setTimeout(poll, 3000);
  } catch (error) {
    if (requestedId !== activeJobId) return;
    if (error.status === 404) {
      forget(ACTIVE_KEY);
      showError('انتهت جلسة الخادم. اختر الفيديو مجددًا لبدء عملية جديدة.');
    } else {
      document.querySelector('#progress-stage').textContent = 'نعيد الاتصال بالخادم؛ احتفظ بالصفحة مفتوحة';
      pollTimer = setTimeout(poll, 7000);
    }
  }
};
const adoptJob = (job) => {
  activeJobId = job.id;
  save(ACTIVE_KEY, {id: job.id});
  document.querySelector('#processing-elapsed').textContent = 'جارٍ التحليل على الخادم';
  setProgress(job);
};

const uploadVideo = async (file, options) => {
  if (file.size > config.max_upload_bytes) throw new Error('حجم الفيديو يتجاوز الحد المسموح الظاهر بجانب اختيار الملف.');
  if (!file.size) throw new Error('ملف الفيديو فارغ. اختر فيديو آخر.');
  const digest = await crypto.subtle.digest('SHA-256', await file.slice(0, 65536).arrayBuffer());
  const fingerprint = `${file.name}:${file.size}:${Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, '0')).join('')}`;
  const saved = readSaved(UPLOAD_KEY);
  let job;
  if (saved?.fingerprint === fingerprint) {
    try { job = await api(`/api/jobs/${saved.id}`); }
    catch (error) { if (error.status !== 404) throw error; }
    if (job && job.status !== 'uploading') {
      forget(UPLOAD_KEY);
      return job;
    }
  }
  if (!job) job = await api('/api/uploads', {method: 'POST', body: JSON.stringify({...options, filename: file.name, size: file.size})});
  save(UPLOAD_KEY, {id: job.id, fingerprint});
  activeJobId = job.id;
  save(ACTIVE_KEY, {id: job.id});
  let offset = job.received_bytes || 0;
  while (offset < file.size) {
    const percent = Math.floor(100 * offset / file.size);
    setProgress({status: 'uploading', progress: Math.round(25 * offset / file.size), stage: `رفع الفيديو من الجوال · ${percent}%`});
    document.querySelector('#processing-title').textContent = file.name;
    document.querySelector('#processing-elapsed').textContent = 'أبقِ الصفحة مفتوحة حتى ينتهي الرفع';
    for (let attempt = 0; ; attempt++) {
      try {
        job = await api(`/api/uploads/${job.id}?offset=${offset}`, {method: 'PUT', body: file.slice(offset, offset + config.upload_chunk_bytes)});
        offset = job.received_bytes;
        break;
      } catch (error) {
        if (attempt >= 3 || (error.status && error.status < 500 && error.status !== 409)) throw error;
        document.querySelector('#progress-stage').textContent = 'انقطع الاتصال؛ نحاول استكمال الرفع تلقائيًا';
        await delay(1000 * (attempt + 1));
        try {
          const latest = await api(`/api/jobs/${job.id}`);
          if (latest.status !== 'uploading') return latest;
          if (latest.received_bytes !== offset) { offset = latest.received_bytes; break; }
        } catch (resumeError) { if (resumeError.status === 404) throw resumeError; }
      }
    }
  }
  const result = await api(`/api/uploads/${job.id}/complete`, {method: 'POST'});
  forget(UPLOAD_KEY);
  return result;
};

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  clearTimeout(pollTimer);
  activeJobId = null;
  forget(ACTIVE_KEY);
  const file = videoInput.files[0];
  const mode = sourceMode;
  const [min_seconds, max_seconds] = document.querySelector('#clip-length').value.split('-').map(Number);
  const options = {clip_count: Number(document.querySelector('#clip-count').value), min_seconds, max_seconds};
  const controls = form.querySelectorAll('input, select, button');
  controls.forEach(control => { control.disabled = true; });
  document.querySelector('#new-job-button').disabled = true;
  document.querySelector('#error-card').classList.add('hidden');
  document.querySelector('#method-note').classList.add('hidden');
  fallbackButton.classList.add('hidden');
  clipsGrid.replaceChildren();
  document.querySelector('#results-title').innerHTML = 'المقاطع <em>تتجهّز.</em>';
  setProgress({progress: 0, stage: mode === 'upload' ? 'تجهيز الفيديو للرفع' : 'إرسال رابط YouTube'});
  showResults();
  try {
    let job;
    if (mode === 'upload') {
      if (!file) throw new Error('اختر فيديو من الاستديو أولًا.');
      job = await uploadVideo(file, options);
    } else {
      const payload = {...options, url: urlInput.value.trim()};
      if (cookiesInput.files[0]) {
        const data = new FormData();
        data.append('request_json', JSON.stringify(payload));
        data.append('cookies', cookiesInput.files[0]);
        job = await api('/api/jobs/with-cookies', {method: 'POST', body: data});
        cookiesInput.value = '';
      } else job = await api('/api/jobs', {method: 'POST', body: JSON.stringify(payload)});
    }
    adoptJob(job);
  } catch (error) {
    document.querySelector('#progress-stage').textContent = 'تعذر الإرسال؛ يمكنك المحاولة مجددًا';
    showError(error.message);
  } finally {
    busy = false;
    controls.forEach(control => { control.disabled = false; });
    setMode(sourceMode);
    document.querySelector('#new-job-button').disabled = false;
    if (activeJobId) poll();
  }
});

for (const mode of ['upload', 'youtube']) document.querySelector(`#${mode}-tab`).addEventListener('click', () => setMode(mode));
videoInput.addEventListener('change', () => {
  if (!videoInput.files[0] || busy) return;
  document.querySelector('#video-file-name').textContent = videoInput.files[0].name;
  form.requestSubmit();
});
fallbackButton.addEventListener('click', () => {
  setMode('upload');
  videoInput.click();
});
document.querySelector('#hero-start').addEventListener('click', () => {
  if (busy) return;
  setMode('upload');
  videoInput.click();
});
document.querySelector('#paste-button').addEventListener('click', async () => {
  try { urlInput.value = await navigator.clipboard.readText(); } catch {}
  urlInput.focus();
});
document.querySelector('#new-job-button').addEventListener('click', () => {
  if (busy) return;
  activeJobId = null;
  forget(ACTIVE_KEY);
  clearTimeout(pollTimer);
  form.reset();
  setMode('upload');
  document.querySelector('#video-file-name').textContent = 'اختر الفيديو ليبدأ التحويل تلقائيًا. أبقِ الصفحة مفتوحة حتى ينتهي الرفع.';
  resultsSection.classList.add('hidden');
  document.querySelector('#workspace').scrollIntoView({behavior: 'smooth'});
});

const refreshConfig = async () => {
  Object.assign(config, await api('/api/config'));
  const note = document.querySelector('#ai-note');
  note.querySelector('.status-dot').classList.toggle('ready', config.openrouter_configured);
  note.querySelector('span:nth-of-type(2)').textContent = config.openrouter_configured
    ? `تحليل OpenRouter مفعّل · ${config.model}`
    : 'الاختيار المحلي يعمل تلقائيًا. أضف مفتاح OpenRouter لتحليل النص بالنموذج.';
  document.querySelector('#upload-limit').textContent = `حتى ${Math.round(config.max_upload_bytes / 1024 ** 2)} ميغابايت · يدعم MP4 وMOV`;
};
document.querySelector('#key-toggle').addEventListener('click', () => {
  document.querySelector('#key-settings').classList.toggle('hidden');
  document.querySelector('#router-key').focus();
});
document.querySelector('#save-key').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const input = document.querySelector('#router-key');
  button.disabled = true;
  try {
    await api('/api/settings', {method: 'POST', body: JSON.stringify({api_key: input.value})});
    input.value = '';
    document.querySelector('#key-settings').classList.add('hidden');
    await refreshConfig();
  } catch (error) { document.querySelector('#ai-note span:nth-of-type(2)').textContent = error.message; }
  finally { button.disabled = false; }
});

const restoreJob = async () => {
  const saved = readSaved(ACTIVE_KEY);
  if (!saved?.id) return;
  try {
    const job = await api(`/api/jobs/${saved.id}`);
    if (busy || activeJobId) return;
    activeJobId = job.id;
    showResults();
    setProgress(job);
    if (job.status === 'uploading') document.querySelector('#progress-stage').textContent = 'اختر نفس الفيديو من الاستديو لاستكمال الرفع';
    else poll();
  } catch (error) { if (error.status === 404) { forget(ACTIVE_KEY); forget(UPLOAD_KEY); } }
};
document.addEventListener('visibilitychange', () => { if (!document.hidden && !busy && activeJobId) poll(); });
refreshConfig().catch(() => { document.querySelector('#ai-note span:nth-of-type(2)').textContent = 'تعذر قراءة إعدادات الخادم؛ أعد تحميل الصفحة.'; });
restoreJob();
setMode('upload');
