(() => {
  "use strict";

  const API_ROOT = "/api/jobs";
  const STORAGE_KEY = "shengyi.activeJobId";
  const TERMINAL_STATES = new Set(["completed", "complete", "succeeded", "success", "failed", "error", "cancelled", "canceled"]);
  const SUCCESS_STATES = new Set(["completed", "complete", "succeeded", "success"]);
  const FAILURE_STATES = new Set(["failed", "error"]);
  const CANCELLED_STATES = new Set(["cancelled", "canceled"]);
  const REFERENCE_WAIT_STATES = new Set([
    "awaiting_reference",
    "waiting_for_reference",
    "reference_required",
  ]);
  const STATUS_LABELS = {
    idle: "等待任务",
    pending: "等待开始",
    queued: "排队中",
    running: "处理中",
    processing: "处理中",
    awaiting_reference: "等待选择声音",
    waiting_for_reference: "等待选择声音",
    reference_required: "等待选择声音",
    completed: "已完成",
    complete: "已完成",
    succeeded: "已完成",
    success: "已完成",
    failed: "处理失败",
    error: "处理失败",
    cancelled: "已取消",
    canceled: "已取消",
  };
  const STEP_LABELS = [
    "正在准备…",
    "下载 / 导入视频",
    "提取音频",
    "Whisper 转写",
    "整理字幕",
    "翻译中文",
    "CosyVoice 中文配音",
    "时间轴同步",
    "音轨混合",
    "合成最终视频",
  ];
  const KNOWN_ARTIFACTS = [
    "final_zh.mp4",
    "final_zh_subtitle.mp4",
    "final_zh_burned.mp4",
    "zh.srt",
    "zh.ass",
    "original.srt",
    "transcript.json",
    "translation.json",
    "dubbed_voice.wav",
  ];

  const dom = {
    form: document.querySelector("#job-form"),
    sourceTabs: [...document.querySelectorAll(".source-tab")],
    youtubePane: document.querySelector("#youtube-source"),
    localPane: document.querySelector("#local-source"),
    youtubeUrl: document.querySelector("#youtube-url"),
    videoFile: document.querySelector("#video-file"),
    videoFileName: document.querySelector("#video-file-name"),
    videoDropzone: document.querySelector("#video-dropzone"),
    translationProvider: document.querySelector("#translation-provider"),
    translationModel: document.querySelector("#translation-model"),
    ollamaModelList: document.querySelector("#ollama-model-list"),
    dubbing: document.querySelector("#enable-dubbing"),
    voiceSettings: document.querySelector("#voice-settings"),
    voiceModes: [...document.querySelectorAll('input[name="voice_mode"]')],
    segmentReference: document.querySelector("#segment-reference"),
    uploadReference: document.querySelector("#upload-reference"),
    referenceAudio: document.querySelector("#reference-audio"),
    referenceFileName: document.querySelector("#reference-file-name"),
    referenceText: document.querySelector("#reference-text"),
    formError: document.querySelector("#form-error"),
    submit: document.querySelector("#submit-button"),
    emptyState: document.querySelector("#empty-state"),
    jobView: document.querySelector("#job-view"),
    statusBadge: document.querySelector("#job-status-badge"),
    jobId: document.querySelector("#job-id"),
    refresh: document.querySelector("#refresh-button"),
    cancel: document.querySelector("#cancel-button"),
    retry: document.querySelector("#retry-button"),
    currentStep: document.querySelector("#current-step-label"),
    progressPercent: document.querySelector("#progress-percent"),
    progressTrack: document.querySelector(".progress-track"),
    progressFill: document.querySelector("#progress-fill"),
    stepCount: document.querySelector("#step-count"),
    segmentCount: document.querySelector("#segment-count"),
    elapsed: document.querySelector("#elapsed-time"),
    referencePicker: document.querySelector("#reference-picker"),
    referencePickerState: document.querySelector("#reference-picker-state"),
    referenceSegmentList: document.querySelector("#reference-segment-list"),
    referencePickerRefresh: document.querySelector("#reference-picker-refresh"),
    steps: [...document.querySelectorAll("#pipeline-steps li")],
    logOutput: document.querySelector("#log-output"),
    clearLogs: document.querySelector("#clear-log-button"),
    resultSection: document.querySelector("#result-section"),
    videoPreview: document.querySelector("#video-preview"),
    resultVideo: document.querySelector("#result-video"),
    artifactList: document.querySelector("#artifact-list"),
    toastRegion: document.querySelector("#toast-region"),
  };

  const state = {
    source: "youtube",
    jobId: null,
    jobStatus: "idle",
    eventSource: null,
    elapsedTimer: null,
    startedAt: null,
    endedAt: null,
    lastProgress: 0,
    lastStep: 0,
    files: [],
    seenLogIds: new Set(),
    ollamaModels: [],
    ollamaDefault: "qwen2.5:14b",
    referencePickerJobId: null,
    referenceSegments: [],
    referenceSegmentsLoaded: false,
    referenceLoading: false,
    referenceSubmitting: false,
  };

  function setSource(source) {
    state.source = source;
    dom.sourceTabs.forEach((tab) => {
      const active = tab.dataset.source === source;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    dom.youtubePane.hidden = source !== "youtube";
    dom.localPane.hidden = source !== "local";
    hideFormError();
  }

  function updateSelectedVideo(file) {
    dom.videoFileName.hidden = !file;
    dom.videoFileName.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "";
  }

  function updateVoiceControls() {
    const enabled = dom.dubbing.checked;
    dom.voiceSettings.classList.toggle("is-disabled", !enabled);
    dom.voiceSettings.setAttribute("aria-disabled", String(!enabled));
    const voiceMode = dom.voiceModes.find((input) => input.checked)?.value || "auto";
    dom.segmentReference.hidden = voiceMode !== "segment";
    dom.uploadReference.hidden = voiceMode !== "upload";
  }

  function updateModelHint() {
    const currentModel = dom.translationModel.value.trim();
    const wasOllamaDefault = currentModel === state.ollamaDefault
      || state.ollamaModels.includes(currentModel)
      || currentModel === "qwen2.5:14b";
    const wasOpenAIDefault = currentModel === "gpt-4.1-mini";
    if (dom.translationProvider.value === "openai" && wasOllamaDefault) {
      dom.translationModel.value = "gpt-4.1-mini";
    } else if (dom.translationProvider.value === "ollama" && wasOpenAIDefault) {
      dom.translationModel.value = state.ollamaDefault;
    }
  }

  async function loadEnvironment() {
    try {
      const response = await fetch("/api/health", { headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const payload = await response.json();
      const ollama = payload?.ollama || {};
      state.ollamaModels = Array.isArray(ollama.models) ? ollama.models : [];
      const configured = String(ollama.configured_model || "").trim();
      state.ollamaDefault = state.ollamaModels.includes(configured)
        ? configured
        : (state.ollamaModels[0] || configured || "qwen2.5:14b");
      dom.ollamaModelList.replaceChildren(
        ...state.ollamaModels.map((name) => {
          const option = document.createElement("option");
          option.value = name;
          return option;
        }),
      );
      if (
        dom.translationProvider.value === "ollama"
        && state.ollamaModels.length
        && !state.ollamaModels.includes(dom.translationModel.value.trim())
      ) {
        dom.translationModel.value = state.ollamaDefault;
      }
    } catch (_) {
      // The create endpoint performs the authoritative readiness check.
    }
  }

  function validateForm() {
    if (state.source === "youtube") {
      const url = dom.youtubeUrl.value.trim();
      if (!url) return "请粘贴 YouTube 视频 URL。";
      try {
        const parsed = new URL(url);
        if (!/^https?:$/.test(parsed.protocol)) throw new Error("invalid protocol");
      } catch (_) {
        return "视频 URL 格式不正确，请检查后重试。";
      }
    } else if (!dom.videoFile.files[0]) {
      return "请选择一个本地视频文件。";
    }

    if (!dom.translationModel.value.trim()) return "请填写翻译模型名称。";

    if (dom.dubbing.checked) {
      const voiceMode = dom.voiceModes.find((input) => input.checked)?.value;
      if (voiceMode === "upload" && !dom.referenceAudio.files[0]) {
        return "上传声音模式需要选择参考音频。";
      }
      if (voiceMode === "upload" && !dom.referenceText.value.trim()) {
        return "请填写参考音频中准确说出的原文。";
      }
    }
    return null;
  }

  function buildPayload() {
    const data = new FormData();
    data.append("source_type", state.source);
    if (state.source === "youtube") {
      data.append("youtube_url", dom.youtubeUrl.value.trim());
    } else {
      data.append("video_file", dom.videoFile.files[0]);
    }

    const simpleFields = [
      "source_language",
      "target_language",
      "whisper_model",
      "translation_provider",
      "translation_model",
      "separation_mode",
      "subtitle_mode",
      "subtitle_font",
    ];
    simpleFields.forEach((name) => {
      const control = dom.form.elements.namedItem(name);
      data.append(name, String(control?.value ?? ""));
    });

    data.append("enable_dubbing", String(dom.dubbing.checked));
    data.append("preserve_background", String(dom.form.elements.namedItem("preserve_background").checked));
    const voiceMode = dom.voiceModes.find((input) => input.checked)?.value || "auto";
    data.append("voice_mode", voiceMode);
    if (voiceMode === "upload") {
      data.append("reference_audio", dom.referenceAudio.files[0]);
      data.append("reference_text", dom.referenceText.value.trim());
    }
    return data;
  }

  async function submitJob(event) {
    event.preventDefault();
    hideFormError();
    const validationError = validateForm();
    if (validationError) {
      showFormError(validationError);
      return;
    }

    setSubmitting(true);
    try {
      const response = await fetch(API_ROOT, { method: "POST", body: buildPayload() });
      const payload = await parseResponse(response);
      if (!response.ok) throw new Error(apiError(payload, `创建任务失败（HTTP ${response.status}）`));
      const id = payload.id ?? payload.job_id;
      if (!id) throw new Error("服务端未返回任务 ID。");

      beginJob(String(id), payload);
      const voiceMode = dom.voiceModes.find((input) => input.checked)?.value || "auto";
      toast(
        voiceMode === "segment"
          ? "任务已创建；转写完成后会请你选择参考声音。"
          : "任务已创建，正在开始处理。",
        "success",
      );
    } catch (error) {
      showFormError(error.message || "无法创建任务，请确认后端服务已启动。");
    } finally {
      setSubmitting(false);
    }
  }

  function beginJob(id, initialPayload = {}) {
    closeEvents();
    resetReferencePicker();
    state.jobId = id;
    state.jobStatus = "queued";
    state.startedAt = parseDate(initialPayload.started_at ?? initialPayload.created_at) ?? Date.now();
    state.endedAt = null;
    state.lastProgress = 0;
    state.lastStep = 0;
    state.files = [];
    state.seenLogIds.clear();
    localStorage.setItem(STORAGE_KEY, id);

    dom.emptyState.hidden = true;
    dom.jobView.hidden = false;
    dom.jobId.textContent = id;
    dom.logOutput.innerHTML = "";
    dom.resultSection.hidden = true;
    dom.resultVideo.removeAttribute("src");
    dom.resultVideo.load();
    renderJob({ ...initialPayload, id, status: initialPayload.status ?? "queued" });
    appendLog("任务已提交，等待处理队列。", "info");
    connectEvents();
    startElapsedClock();
  }

  function connectEvents() {
    closeEvents();
    if (
      !state.jobId
      || TERMINAL_STATES.has(normalizeStatus(state.jobStatus))
      || isAwaitingReference()
    ) return;

    const events = new EventSource(`${API_ROOT}/${encodeURIComponent(state.jobId)}/events`);
    state.eventSource = events;

    events.onopen = () => appendLog("已连接实时进度。", "info", true);
    events.onmessage = (event) => handleEventData(event.data, "message");
    [
      "update",
      "progress",
      "status",
      "log",
      "awaiting_reference",
      "reference_required",
      "complete",
      "completed",
      "failed",
    ].forEach((type) => {
      events.addEventListener(type, (event) => handleEventData(event.data, type));
    });
    events.onerror = () => {
      if (
        TERMINAL_STATES.has(normalizeStatus(state.jobStatus))
        || isAwaitingReference()
      ) {
        closeEvents();
        return;
      }
      appendLog("实时连接暂时中断，浏览器将自动重连。", "warning", true);
      refreshJob({ quiet: true });
    };
  }

  function handleEventData(raw, eventType) {
    if (!raw) return;
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (_) {
      payload = eventType === "log" || eventType === "message" ? { message: raw } : {};
    }

    if (typeof payload === "string") payload = { message: payload };
    const nested = payload.job && typeof payload.job === "object" ? payload.job : payload;
    renderJob(nested);

    const messages = extractLogs(payload);
    messages.forEach((entry) => appendLog(entry.message, entry.level, false, entry.time, entry.id));

    if ((eventType === "complete" || eventType === "completed") && !nested.status) {
      renderJob({ ...nested, status: "completed", progress: 100, step: 9 });
    } else if (eventType === "failed" && !nested.status) {
      renderJob({ ...nested, status: "failed" });
    }
  }

  async function refreshJob({ quiet = false } = {}) {
    if (!state.jobId) return;
    if (!quiet) dom.refresh.classList.add("is-spinning");
    try {
      const response = await fetch(`${API_ROOT}/${encodeURIComponent(state.jobId)}`, { cache: "no-store" });
      const payload = await parseResponse(response);
      if (!response.ok) throw new Error(apiError(payload, `刷新失败（HTTP ${response.status}）`));
      renderJob(payload.job ?? payload);
      extractLogs(payload).forEach((entry) => appendLog(entry.message, entry.level, true, entry.time, entry.id));
    } catch (error) {
      if (!quiet) toast(error.message || "刷新任务失败。", "error");
    } finally {
      dom.refresh.classList.remove("is-spinning");
    }
  }

  async function cancelJob() {
    if (!state.jobId || TERMINAL_STATES.has(normalizeStatus(state.jobStatus))) return;
    dom.cancel.disabled = true;
    try {
      const response = await fetch(`${API_ROOT}/${encodeURIComponent(state.jobId)}/cancel`, { method: "POST" });
      const payload = await parseResponse(response);
      if (!response.ok) throw new Error(apiError(payload, `取消失败（HTTP ${response.status}）`));
      renderJob(payload.job ?? { ...payload, status: payload.status ?? "cancelled" });
      appendLog("已请求取消任务。", "warning");
    } catch (error) {
      toast(error.message || "无法取消任务。", "error");
    } finally {
      dom.cancel.disabled = false;
    }
  }

  async function retryJob() {
    if (!state.jobId) return;
    dom.retry.disabled = true;
    try {
      const retryData = new FormData();
      retryData.append("translation_provider", dom.translationProvider.value);
      retryData.append("translation_model", dom.translationModel.value.trim());
      const response = await fetch(`${API_ROOT}/${encodeURIComponent(state.jobId)}/retry`, {
        method: "POST",
        body: retryData,
      });
      const payload = await parseResponse(response);
      if (!response.ok) throw new Error(apiError(payload, `重试失败（HTTP ${response.status}）`));
      state.startedAt = Date.now();
      state.endedAt = null;
      renderJob(payload.job ?? { ...payload, status: payload.status ?? "queued" });
      appendLog("任务已从缓存继续。", "info");
      connectEvents();
      startElapsedClock();
    } catch (error) {
      toast(error.message || "无法重试任务。", "error");
    } finally {
      dom.retry.disabled = false;
    }
  }

  function isAwaitingReference(status = state.jobStatus) {
    return REFERENCE_WAIT_STATES.has(normalizeStatus(status));
  }

  function resetReferencePicker() {
    state.referencePickerJobId = null;
    state.referenceSegments = [];
    state.referenceSegmentsLoaded = false;
    state.referenceLoading = false;
    state.referenceSubmitting = false;
    dom.referencePicker.hidden = true;
    dom.referenceSegmentList.replaceChildren();
    dom.referencePickerState.hidden = false;
    dom.referencePickerState.classList.remove("is-error");
    dom.referencePickerState.textContent = "正在读取转写片段…";
    dom.referencePickerRefresh.hidden = true;
  }

  function renderReferencePicker(status) {
    const awaiting = isAwaitingReference(status);
    dom.referencePicker.hidden = !awaiting;
    if (!awaiting || !state.jobId) return;

    if (state.referencePickerJobId !== state.jobId) {
      state.referencePickerJobId = state.jobId;
      state.referenceSegments = [];
      state.referenceSegmentsLoaded = false;
      state.referenceLoading = false;
      state.referenceSubmitting = false;
      dom.referenceSegmentList.replaceChildren();
      void loadReferenceSegments();
    }
  }

  async function loadReferenceSegments({ force = false } = {}) {
    if (!state.jobId || !isAwaitingReference() || state.referenceSubmitting) return;
    if (state.referenceLoading || (!force && state.referenceSegmentsLoaded)) return;

    const jobId = state.jobId;
    state.referencePickerJobId = jobId;
    state.referenceLoading = true;
    state.referenceSegmentsLoaded = false;
    dom.referenceSegmentList.replaceChildren();
    dom.referencePickerState.hidden = false;
    dom.referencePickerState.classList.remove("is-error");
    dom.referencePickerState.textContent = "正在读取转写片段…";
    dom.referencePickerRefresh.hidden = true;

    try {
      const response = await fetch(
        `${API_ROOT}/${encodeURIComponent(jobId)}/segments`,
        { cache: "no-store", headers: { Accept: "application/json" } },
      );
      const payload = await parseResponse(response);
      if (!response.ok) {
        throw new Error(apiError(payload, `读取片段失败（HTTP ${response.status}）`));
      }
      if (state.jobId !== jobId) return;
      state.referenceSegments = normalizeReferenceSegments(payload);
      state.referenceSegmentsLoaded = true;
      renderReferenceSegments();
    } catch (error) {
      if (state.jobId !== jobId) return;
      state.referenceSegments = [];
      dom.referencePickerState.hidden = false;
      dom.referencePickerState.classList.add("is-error");
      dom.referencePickerState.textContent = error.message || "无法读取参考声音片段。";
      dom.referencePickerRefresh.hidden = false;
    } finally {
      if (state.jobId === jobId) state.referenceLoading = false;
    }
  }

  function normalizeReferenceSegments(payload) {
    const values = Array.isArray(payload)
      ? payload
      : payload?.segments
        ?? payload?.items
        ?? payload?.data?.segments
        ?? payload?.transcript?.segments
        ?? [];
    if (!Array.isArray(values)) return [];

    return values.map((item, index) => {
      const id = Number(item?.id ?? item?.segment_id ?? index);
      const start = Number(item?.start ?? item?.start_time ?? 0);
      const end = Number(item?.end ?? item?.end_time ?? start);
      return {
        id,
        start,
        end,
        text: String(item?.text ?? item?.original_text ?? item?.transcript ?? "").trim(),
      };
    }).filter((item) => (
      Number.isInteger(item.id)
      && item.id >= 0
      && Number.isFinite(item.start)
      && Number.isFinite(item.end)
      && item.end > item.start
    ));
  }

  function renderReferenceSegments() {
    dom.referenceSegmentList.replaceChildren();
    dom.referencePickerState.classList.remove("is-error");
    if (!state.referenceSegments.length) {
      dom.referencePickerState.hidden = false;
      dom.referencePickerState.textContent = "没有找到可选片段。可刷新任务后重新加载。";
      dom.referencePickerRefresh.hidden = false;
      return;
    }

    dom.referencePickerState.hidden = true;
    dom.referencePickerRefresh.hidden = true;
    dom.referenceSegmentList.replaceChildren(
      ...state.referenceSegments.map(createReferenceSegmentOption),
    );
  }

  function createReferenceSegmentOption(segment) {
    const item = document.createElement("div");
    item.className = "reference-segment-item";
    item.setAttribute("role", "listitem");

    const button = document.createElement("button");
    button.type = "button";
    button.className = "reference-segment-option";
    button.dataset.segmentId = String(segment.id);
    button.setAttribute("aria-label", `选择 segment ${segment.id} 作为参考声音`);

    const meta = document.createElement("span");
    meta.className = "reference-segment-meta";
    meta.textContent = `#${segment.id} · ${formatSegmentTime(segment.start)} → ${formatSegmentTime(segment.end)} · ${(segment.end - segment.start).toFixed(1)} 秒`;

    const action = document.createElement("span");
    action.className = "reference-segment-action";
    action.textContent = "选择这段";

    const text = document.createElement("span");
    text.className = "reference-segment-text";
    text.textContent = segment.text || "（该片段没有可显示的原文）";

    button.append(meta, action, text);
    button.addEventListener("click", () => submitReferenceSegment(segment.id, button));
    item.append(button);
    return item;
  }

  async function submitReferenceSegment(segmentId, selectedButton) {
    if (!state.jobId || !isAwaitingReference() || state.referenceSubmitting) return;
    const jobId = state.jobId;
    state.referenceSubmitting = true;
    const buttons = [...dom.referenceSegmentList.querySelectorAll("button")];
    buttons.forEach((button) => { button.disabled = true; });
    selectedButton.classList.add("is-submitting");
    const action = selectedButton.querySelector(".reference-segment-action");
    if (action) action.textContent = "正在提交…";

    try {
      const data = new FormData();
      data.append("segment_id", String(segmentId));
      let response = await fetch(
        `${API_ROOT}/${encodeURIComponent(jobId)}/reference`,
        { method: "POST", body: data },
      );
      let payload = await parseResponse(response);
      // The current contract is multipart.  Accept an older JSON-body backend
      // during an in-place upgrade so an already-open page can still resume.
      if (response.status === 422 && isReferenceBodyValidationError(payload)) {
        response = await fetch(
          `${API_ROOT}/${encodeURIComponent(jobId)}/reference`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ segment_id: segmentId }),
          },
        );
        payload = await parseResponse(response);
      }
      if (!response.ok) {
        throw new Error(apiError(payload, `提交参考片段失败（HTTP ${response.status}）`));
      }
      if (state.jobId !== jobId) return;

      const returnedJob = payload?.job && typeof payload.job === "object"
        ? payload.job
        : payload;
      if (returnedJob && typeof returnedJob === "object" && returnedJob.status) {
        renderJob(returnedJob);
      }
      appendLog(`已选择 segment ${segmentId} 作为参考声音，任务继续。`, "info");
      toast(`已选择 segment ${segmentId}，任务正在继续。`, "success");
      await refreshJob({ quiet: true });
      if (
        !TERMINAL_STATES.has(normalizeStatus(state.jobStatus))
        && !isAwaitingReference()
      ) {
        connectEvents();
        startElapsedClock();
      }
    } catch (error) {
      if (state.jobId !== jobId) return;
      toast(error.message || "无法提交参考声音片段。", "error");
      appendLog(error.message || "无法提交参考声音片段。", "error");
    } finally {
      if (state.jobId === jobId) {
        state.referenceSubmitting = false;
        if (isAwaitingReference()) renderReferenceSegments();
      }
    }
  }

  function isReferenceBodyValidationError(payload) {
    if (!Array.isArray(payload?.detail)) return false;
    return payload.detail.some((item) => (
      Array.isArray(item?.loc)
      && item.loc.includes("body")
      && ["json_invalid", "model_attributes_type", "missing"].includes(item?.type)
    ));
  }

  function formatSegmentTime(value) {
    const totalMilliseconds = Math.max(0, Math.round(Number(value || 0) * 1000));
    const totalSeconds = Math.floor(totalMilliseconds / 1000);
    const milliseconds = totalMilliseconds % 1000;
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const base = hours
      ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
      : `${pad(minutes)}:${pad(seconds)}`;
    return `${base}.${String(milliseconds).padStart(3, "0")}`;
  }

  function renderJob(raw = {}) {
    const job = normalizeJob(raw);
    if (job.id && state.jobId !== String(job.id)) {
      resetReferencePicker();
      state.jobId = String(job.id);
    }
    if (job.status) state.jobStatus = job.status;
    if (job.startedAt) state.startedAt = job.startedAt;
    if (job.updatedAt && TERMINAL_STATES.has(normalizeStatus(job.status))) state.endedAt = job.updatedAt;
    if (job.step !== null) state.lastStep = job.step;
    if (job.progress !== null) state.lastProgress = job.progress;
    if (job.files.length) state.files = job.files;

    const status = normalizeStatus(state.jobStatus);
    dom.statusBadge.textContent = STATUS_LABELS[status] ?? status;
    dom.statusBadge.className = `job-status status-${statusClass(status)}`;
    dom.jobId.textContent = state.jobId || "—";

    const step = clamp(state.lastStep, 0, 9);
    let progress = clamp(state.lastProgress, 0, 100);
    if (!progress && step > 0) progress = Math.round(((step - 1) / 9) * 100);
    if (SUCCESS_STATES.has(status)) {
      progress = 100;
      state.lastStep = 9;
    }
    state.lastProgress = progress;

    dom.progressPercent.textContent = `${Math.round(progress)}%`;
    dom.progressFill.style.width = `${progress}%`;
    dom.progressTrack.setAttribute("aria-valuenow", String(Math.round(progress)));
    dom.stepCount.textContent = `${SUCCESS_STATES.has(status) ? 9 : step} / 9`;
    dom.currentStep.textContent = job.stepLabel || STEP_LABELS[SUCCESS_STATES.has(status) ? 9 : step] || "正在处理…";

    if (job.segmentTotal !== null) {
      dom.segmentCount.textContent = `${job.segmentDone ?? 0} / ${job.segmentTotal}`;
    } else if (job.segmentDone !== null) {
      dom.segmentCount.textContent = String(job.segmentDone);
    }

    renderSteps(step, status);
    renderActions(status);
    renderReferencePicker(status);
    if (isAwaitingReference(status)) closeEvents();
    if (state.files.length) renderArtifacts(state.files);

    if (TERMINAL_STATES.has(status)) {
      closeEvents();
      stopElapsedClock();
      if (SUCCESS_STATES.has(status)) {
        dom.resultSection.hidden = false;
        if (!state.files.length) renderArtifacts(KNOWN_ARTIFACTS);
      }
    }
  }

  function normalizeJob(raw) {
    const progressObject = raw.progress && typeof raw.progress === "object" ? raw.progress : {};
    const scalarProgress = typeof raw.progress === "number" ? raw.progress : null;
    const percentCandidate = firstNumber(
      scalarProgress,
      raw.progress_percent,
      raw.percent,
      progressObject.percent,
      progressObject.percentage,
    );
    const stepCandidate = firstNumber(
      raw.step,
      raw.current_step,
      raw.step_index,
      progressObject.step,
      progressObject.current_step,
    );

    let step = stepCandidate;
    if (step !== null && step === 0 && (percentCandidate ?? 0) > 0) step = 1;
    if (step !== null && step > 9 && step <= 100) step = Math.min(9, Math.ceil(step / (100 / 9)));

    const status = normalizeStatus(raw.status ?? raw.state ?? progressObject.status ?? state.jobStatus);
    const files = normalizeFiles(
      raw.files
      ?? raw.artifacts
      ?? raw.outputs
      ?? raw.output_files
      ?? raw.result?.files
      ?? raw.result?.artifacts
      ?? raw.result?.outputs
      ?? extractResultFiles(raw.result),
    );
    const startedAt = parseDate(raw.started_at ?? raw.startedAt ?? raw.created_at ?? raw.createdAt);
    const updatedAt = parseDate(raw.updated_at ?? raw.updatedAt ?? raw.finished_at ?? raw.completed_at);

    return {
      id: raw.id ?? raw.job_id,
      status,
      step: step === null ? null : clamp(Math.trunc(step), 0, 9),
      stepLabel: raw.current_step_name
        ?? raw.step_name
        ?? (typeof raw.current_step === "string" ? raw.current_step : undefined)
        ?? raw.message
        ?? progressObject.message
        ?? "",
      progress: percentCandidate === null ? null : normalizePercent(percentCandidate),
      segmentDone: firstNumber(raw.segments_done, raw.completed_segments, raw.segment_done, progressObject.completed, progressObject.segments_done),
      segmentTotal: firstNumber(raw.segments_total, raw.total_segments, raw.segment_total, progressObject.total, progressObject.segments_total),
      files,
      startedAt,
      updatedAt,
    };
  }

  function renderSteps(currentStep, status) {
    const effectiveStep = SUCCESS_STATES.has(status) ? 10 : currentStep;
    dom.steps.forEach((item, index) => {
      const number = index + 1;
      const stepState = item.querySelector(".step-state");
      item.classList.remove("is-active", "is-complete", "is-error");
      if (number < effectiveStep) {
        item.classList.add("is-complete");
        stepState.textContent = "完成";
      } else if (number === effectiveStep) {
        if (FAILURE_STATES.has(status)) {
          item.classList.add("is-error");
          stepState.textContent = "失败";
        } else if (CANCELLED_STATES.has(status)) {
          item.classList.add("is-error");
          stepState.textContent = "取消";
        } else if (REFERENCE_WAIT_STATES.has(status)) {
          item.classList.add("is-active");
          stepState.textContent = "等待选择";
        } else {
          item.classList.add("is-active");
          stepState.textContent = "进行中";
        }
      } else {
        stepState.textContent = "等待";
      }
    });
  }

  function renderActions(status) {
    const terminal = TERMINAL_STATES.has(status);
    dom.cancel.hidden = terminal;
    dom.retry.hidden = !(FAILURE_STATES.has(status) || CANCELLED_STATES.has(status));
  }

  function renderArtifacts(files) {
    const normalized = normalizeFiles(files);
    const seen = new Set();
    const usable = normalized.filter((file) => {
      if (!file.name || seen.has(file.name)) return false;
      seen.add(file.name);
      return true;
    });
    if (!usable.length || !state.jobId) return;

    dom.resultSection.hidden = false;
    dom.artifactList.replaceChildren(...usable.map(createArtifactLink));
    const preview = usable.find((file) => /final_zh(?:_subtitle)?\.mp4$/i.test(file.name))
      ?? usable.find((file) => /\.mp4$/i.test(file.name));
    if (preview) {
      const url = artifactUrl(preview);
      if (dom.resultVideo.getAttribute("src") !== url) {
        dom.resultVideo.src = url;
        dom.resultVideo.load();
      }
      dom.videoPreview.hidden = false;
    } else {
      dom.videoPreview.hidden = true;
    }
  }

  function createArtifactLink(file) {
    const link = document.createElement("a");
    link.className = "artifact-link";
    link.href = artifactUrl(file);
    link.download = file.name;
    link.title = `下载 ${file.name}`;
    link.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m0 0 4-4m-4 4-4-4M5 19h14"/></svg><span></span>`;
    link.querySelector("span").textContent = file.label || file.name;
    return link;
  }

  function artifactUrl(file) {
    if (file.url) return file.url;
    return `${API_ROOT}/${encodeURIComponent(state.jobId)}/files/${encodeURIComponent(file.name)}`;
  }

  function normalizeFiles(files) {
    if (!files) return [];
    if (!Array.isArray(files) && typeof files === "object") {
      return Object.entries(files).map(([name, value]) => {
        if (typeof value === "string") return { name, url: value };
        return { name, ...(value || {}) };
      });
    }
    if (!Array.isArray(files)) return [];
    return files.map((file) => {
      if (typeof file === "string") return { name: basename(file) };
      const path = file.name ?? file.filename ?? file.file ?? file.path ?? "";
      return {
        name: basename(path),
        url: file.url ?? file.download_url ?? null,
        label: file.label ?? file.display_name ?? null,
      };
    }).filter((file) => file.name);
  }

  function extractResultFiles(result) {
    if (!result || typeof result !== "object") return [];
    const mediaExtension = /\.(?:mp4|srt|ass|json|wav)$/i;
    return Object.entries(result).flatMap(([label, value]) => {
      if (typeof value === "string" && mediaExtension.test(value)) {
        return [{ name: basename(value), label: humanizeArtifactLabel(label) }];
      }
      if (Array.isArray(value)) return normalizeFiles(value);
      return [];
    });
  }

  function humanizeArtifactLabel(label) {
    const known = {
      final_video: "中文配音视频",
      final_soft: "软字幕视频",
      final_burned: "压制字幕视频",
      subtitle: "中文字幕",
      original_subtitle: "原文字幕",
      transcript: "转写 JSON",
      translation: "翻译 JSON",
      dubbed_voice: "中文配音轨",
    };
    return known[label] ?? String(label).replaceAll("_", " ");
  }

  function extractLogs(payload) {
    const entries = [];
    const add = (value, fallbackLevel = "info") => {
      if (!value) return;
      if (typeof value === "string") entries.push({ message: value, level: fallbackLevel });
      else if (typeof value === "object") {
        const message = value.message ?? value.text ?? value.log;
        if (message) entries.push({
          message: String(message),
          level: value.level ?? fallbackLevel,
          time: value.time ?? value.timestamp ?? value.created_at,
          id: value.id ?? null,
        });
      }
    };
    if (Array.isArray(payload.logs)) payload.logs.forEach((log) => add(log));
    else if (payload.log) add(payload.log, payload.level);
    if (payload.message && !payload.current_step_name && !payload.step_name) add(payload.message, payload.level);
    if (payload.error && typeof payload.error === "string") add(payload.error, "error");
    return entries;
  }

  function appendLog(message, level = "info", deduplicate = false, suppliedTime = null, logId = null) {
    if (!message) return;
    const text = String(message).trim();
    if (!text) return;
    if (logId !== null && logId !== undefined) {
      const identity = String(logId);
      if (state.seenLogIds.has(identity)) return;
      state.seenLogIds.add(identity);
    }
    const last = dom.logOutput.lastElementChild;
    if (deduplicate && last?.dataset.message === text) return;
    dom.logOutput.querySelector(".log-placeholder")?.remove();

    const line = document.createElement("div");
    const normalizedLevel = String(level || "info").toLowerCase();
    line.className = `log-line is-${normalizedLevel}`;
    line.dataset.message = text;
    const time = document.createElement("time");
    time.textContent = formatLogTime(suppliedTime);
    const content = document.createElement("span");
    content.textContent = text;
    line.append(time, content);
    dom.logOutput.append(line);

    while (dom.logOutput.children.length > 500) dom.logOutput.firstElementChild.remove();
    dom.logOutput.scrollTop = dom.logOutput.scrollHeight;
  }

  function startElapsedClock() {
    stopElapsedClock();
    updateElapsed();
    state.elapsedTimer = window.setInterval(updateElapsed, 1000);
  }

  function stopElapsedClock() {
    if (state.elapsedTimer) window.clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
    updateElapsed();
  }

  function updateElapsed() {
    if (!state.startedAt) {
      dom.elapsed.textContent = "00:00";
      return;
    }
    const endpoint = state.endedAt ?? Date.now();
    const seconds = Math.max(0, Math.floor((endpoint - state.startedAt) / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    dom.elapsed.textContent = hours
      ? `${pad(hours)}:${pad(minutes)}:${pad(rest)}`
      : `${pad(minutes)}:${pad(rest)}`;
  }

  function closeEvents() {
    if (state.eventSource) state.eventSource.close();
    state.eventSource = null;
  }

  function setSubmitting(loading) {
    dom.submit.disabled = loading;
    dom.submit.classList.toggle("is-loading", loading);
  }

  function showFormError(message) {
    dom.formError.textContent = message;
    dom.formError.hidden = false;
    dom.formError.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function hideFormError() {
    dom.formError.hidden = true;
    dom.formError.textContent = "";
  }

  function toast(message, type = "info") {
    const element = document.createElement("div");
    element.className = `toast ${type === "error" ? "is-error" : ""}`;
    element.textContent = message;
    dom.toastRegion.append(element);
    window.setTimeout(() => element.remove(), 4200);
  }

  async function restoreJob() {
    const id = localStorage.getItem(STORAGE_KEY);
    if (!id) return;
    state.jobId = id;
    dom.emptyState.hidden = true;
    dom.jobView.hidden = false;
    dom.jobId.textContent = id;
    appendLog("正在恢复上次任务…", "info");
    await refreshJob({ quiet: true });
    if (!TERMINAL_STATES.has(normalizeStatus(state.jobStatus))) {
      if (!isAwaitingReference()) connectEvents();
      startElapsedClock();
    }
  }

  function normalizeStatus(value) {
    return String(value || "idle").toLowerCase().replaceAll(/[\s-]+/g, "_");
  }

  function statusClass(status) {
    if (SUCCESS_STATES.has(status)) return "completed";
    if (FAILURE_STATES.has(status)) return "failed";
    if (CANCELLED_STATES.has(status)) return "cancelled";
    if (REFERENCE_WAIT_STATES.has(status)) return "awaiting-reference";
    if (status === "pending" || status === "queued") return "queued";
    if (status === "running" || status === "processing") return "running";
    return "idle";
  }

  function normalizePercent(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    return numeric > 0 && numeric <= 1 ? numeric * 100 : numeric;
  }

  function firstNumber(...values) {
    for (const value of values) {
      if (value === null || value === undefined || value === "") continue;
      const numeric = Number(value);
      if (Number.isFinite(numeric)) return numeric;
    }
    return null;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, Number(value) || 0));
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function parseDate(value) {
    if (!value) return null;
    if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function formatLogTime(value) {
    const date = value ? new Date(value) : new Date();
    if (Number.isNaN(date.getTime())) return new Date().toLocaleTimeString("zh-CN", { hour12: false });
    return date.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function basename(path) {
    return String(path || "").replaceAll("\\", "/").split("/").pop();
  }

  async function parseResponse(response) {
    const type = response.headers.get("content-type") || "";
    if (type.includes("application/json")) return response.json();
    const text = await response.text();
    if (!text) return {};
    try { return JSON.parse(text); } catch (_) { return { detail: text }; }
  }

  function apiError(payload, fallback) {
    if (!payload) return fallback;
    if (typeof payload === "string") return payload;
    if (typeof payload.detail === "string") return payload.detail;
    if (Array.isArray(payload.detail)) return payload.detail.map((item) => item.msg ?? JSON.stringify(item)).join("；");
    return payload.message ?? payload.error ?? fallback;
  }

  dom.sourceTabs.forEach((tab) => tab.addEventListener("click", () => setSource(tab.dataset.source)));
  dom.videoFile.addEventListener("change", () => updateSelectedVideo(dom.videoFile.files[0]));
  ["dragenter", "dragover"].forEach((type) => dom.videoDropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dom.videoDropzone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((type) => dom.videoDropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dom.videoDropzone.classList.remove("is-dragging");
  }));
  dom.videoDropzone.addEventListener("drop", (event) => {
    const file = [...event.dataTransfer.files].find((candidate) => candidate.type.startsWith("video/") || /\.(mp4|mov|mkv|webm|m4v)$/i.test(candidate.name));
    if (!file) {
      toast("拖入的文件不是支持的视频格式。", "error");
      return;
    }
    const transfer = new DataTransfer();
    transfer.items.add(file);
    dom.videoFile.files = transfer.files;
    updateSelectedVideo(file);
  });
  dom.translationProvider.addEventListener("change", updateModelHint);
  dom.dubbing.addEventListener("change", updateVoiceControls);
  dom.voiceModes.forEach((input) => input.addEventListener("change", updateVoiceControls));
  dom.referenceAudio.addEventListener("change", () => {
    dom.referenceFileName.textContent = dom.referenceAudio.files[0]?.name || "选择参考音频";
  });
  dom.form.addEventListener("submit", submitJob);
  dom.refresh.addEventListener("click", () => refreshJob());
  dom.referencePickerRefresh.addEventListener("click", () => {
    void loadReferenceSegments({ force: true });
  });
  dom.cancel.addEventListener("click", cancelJob);
  dom.retry.addEventListener("click", retryJob);
  dom.clearLogs.addEventListener("click", () => {
    dom.logOutput.innerHTML = '<div class="log-placeholder">日志显示已清空，新消息会继续出现。</div>';
  });
  window.addEventListener("beforeunload", closeEvents);

  updateVoiceControls();
  loadEnvironment().finally(restoreJob);
})();
