const form = document.querySelector("#job-form");
const urlInput = document.querySelector("#video-url");
const cookiesInput = document.querySelector("#youtube-cookies");
const resultsSection = document.querySelector("#results-section");
const progressBar = document.querySelector("#progress-bar");
const clipsGrid = document.querySelector("#clips-grid");
let activeJobId = null;
let pollTimer = null;

const explainError = (message) => {
  if (/sign in to confirm|not a bot|--cookies/i.test(message || "")) {
    return "رفض YouTube تنزيل الفيديو من خادم الموقع وطلب تسجيل الدخول للتحقق. جرّب رابطًا عامًا آخر أو أعد المحاولة لاحقًا. هذه المشكلة من YouTube وليست من OpenRouter.";
  }
  return message || "تعذر تجهيز هذا الفيديو.";
};

const api = async (path, options = {}) => {
  const headers = {...(options.headers || {})};
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {...options, headers});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "تعذر إكمال الطلب.");
  return body;
};

const showResults = () => {
  resultsSection.classList.remove("hidden");
  resultsSection.scrollIntoView({behavior: "smooth", block: "start"});
};

const setProgress = (job) => {
  document.querySelector("#progress-stage").textContent = job.stage || "جارٍ تجهيز الفيديو";
  document.querySelector("#progress-percent").textContent = `${job.progress || 0}%`;
  progressBar.style.width = `${job.progress || 0}%`;
  if (job.title) document.querySelector("#processing-title").textContent = job.title;
  if (job.status === "completed") {
    document.querySelector("#results-title").innerHTML = 'جاهزة <em>للنشر.</em>';
    document.querySelector("#processing-elapsed").textContent = "اكتملت المعالجة";
    document.querySelector("#progress-percent").textContent = "100%";
    progressBar.style.width = "100%";
    const method = document.querySelector("#method-note");
    method.textContent = `طريقة اختيار اللحظات: ${job.selection_method || "تحليل ذكي"} · تم تفريغ الكلام محليًا.`;
    method.classList.remove("hidden");
    renderClips(job.clips || []);
    clearInterval(pollTimer);
  } else if (job.status === "failed") {
    clearInterval(pollTimer);
    document.querySelector("#results-title").innerHTML = 'نحتاج <em>محاولة أخرى.</em>';
    const error = document.querySelector("#error-card");
    error.textContent = explainError(job.error);
    error.classList.remove("hidden");
  }
};

const renderClips = (clips) => {
  clipsGrid.innerHTML = "";
  for (const clip of clips) {
    const card = document.createElement("article");
    card.className = "clip-card";
    const video = document.createElement("video");
    video.className = "clip-video";
    video.controls = true;
    video.preload = "metadata";
    video.src = clip.download_url;
    const details = document.createElement("div");
    details.className = "clip-details";
    details.innerHTML = `<div class="clip-meta"><span>${formatTime(clip.start)} — ${formatTime(clip.end)}</span><span class="clip-score">✳ ${Math.round(clip.score || 0)}/10</span></div>`;
    const title = document.createElement("h3");
    title.textContent = clip.title || "لحظة مميزة";
    const reason = document.createElement("p");
    reason.textContent = clip.reason || clip.hook || "مقطع جاهز للنشر.";
    const actions = document.createElement("div");
    actions.className = "clip-actions";
    const download = document.createElement("a");
    download.href = clip.download_url;
    download.download = `${clip.id}.mp4`;
    download.textContent = "تنزيل MP4 ↓";
    const subtitles = document.createElement("a");
    subtitles.href = clip.subtitle_url;
    subtitles.download = `${clip.id}.srt`;
    subtitles.className = "secondary";
    subtitles.textContent = "ملف SRT";
    actions.append(download, subtitles);
    details.append(title, reason, actions);
    card.append(video, details);
    clipsGrid.append(card);
  }
};

const formatTime = (seconds) => {
  const mins = Math.floor(seconds / 60).toString().padStart(2, "0");
  const secs = Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${mins}:${secs}`;
};

const poll = async () => {
  if (!activeJobId) return;
  try {
    setProgress(await api(`/api/jobs/${activeJobId}`));
  } catch (error) {
    document.querySelector("#error-card").textContent = error.message;
    document.querySelector("#error-card").classList.remove("hidden");
    clearInterval(pollTimer);
  }
};

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector(".submit-button");
  const settings = document.querySelector("#clip-length").value.split("-").map(Number);
  const payload = {
    url: urlInput.value.trim(),
    clip_count: Number(document.querySelector("#clip-count").value),
    min_seconds: settings[0],
    max_seconds: settings[1],
  };
  button.disabled = true;
  document.querySelector("#error-card").classList.add("hidden");
  document.querySelector("#method-note").classList.add("hidden");
  clipsGrid.innerHTML = "";
  document.querySelector("#results-title").innerHTML = 'المقاطع <em>تتجهّز.</em>';
  try {
    let job;
    const cookiesFile = cookiesInput.files[0];
    if (cookiesFile) {
      const formData = new FormData();
      formData.append("request_json", JSON.stringify(payload));
      formData.append("cookies", cookiesFile, cookiesFile.name);
      job = await api("/api/jobs/with-cookies", {method: "POST", body: formData});
      cookiesInput.value = "";
    } else {
      job = await api("/api/jobs", {method: "POST", body: JSON.stringify(payload)});
    }
    activeJobId = job.id;
    document.querySelector("#progress-stage").textContent = job.stage;
    document.querySelector("#progress-percent").textContent = "0%";
    progressBar.style.width = "0%";
    showResults();
    clearInterval(pollTimer);
    pollTimer = setInterval(poll, 1800);
    poll();
  } catch (error) {
    showResults();
    document.querySelector("#error-card").textContent = error.message;
    document.querySelector("#error-card").classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#paste-button").addEventListener("click", async () => {
  try {
    urlInput.value = await navigator.clipboard.readText();
    urlInput.focus();
  } catch {
    urlInput.focus();
  }
});

document.querySelector("#new-job-button").addEventListener("click", () => {
  activeJobId = null;
  clearInterval(pollTimer);
  form.reset();
  document.querySelector("#results-section").classList.add("hidden");
  document.querySelector("#workspace").scrollIntoView({behavior: "smooth"});
});

const refreshConfig = async () => {
  const note = document.querySelector("#ai-note");
  const config = await api("/api/config");
  const dot = note.querySelector(".status-dot");
  const text = note.querySelector("span:nth-of-type(2)");
  dot.classList.toggle("ready", config.openrouter_configured);
  text.textContent = config.openrouter_configured
    ? `تحليل OpenRouter مفعّل · ${config.model}`
    : "الاختيار الذكي يحتاج مفتاح OpenRouter في ملف .env";
};

document.querySelector("#key-toggle").addEventListener("click", () => {
  document.querySelector("#key-settings").classList.toggle("hidden");
  document.querySelector("#router-key").focus();
});

document.querySelector("#save-key").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const input = document.querySelector("#router-key");
  button.disabled = true;
  try {
    await api("/api/settings", {method: "POST", body: JSON.stringify({api_key: input.value})});
    input.value = "";
    document.querySelector("#key-settings").classList.add("hidden");
    await refreshConfig();
  } catch (error) {
    document.querySelector("#ai-note span:nth-of-type(2)").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

refreshConfig().catch(() => {
  document.querySelector("#ai-note span:nth-of-type(2)").textContent = "تعذر قراءة حالة الإعدادات.";
});
