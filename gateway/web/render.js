const form = document.querySelector("#render-form");
const style = form.elements.style;
const iterations = form.elements.max_iterations;
const seed = form.elements.seed;
const button = form.querySelector("button");
const seedBatchButton = document.querySelector("#seed-batch-button");
const batchResults = document.querySelector("#batch-results");
const statusLine = document.querySelector("#status");
const details = document.querySelector("#job-details");
const preview = document.querySelector("#preview");
const previous = document.querySelector("#previous");
const previousFigure = document.querySelector("#previous-figure");
const download = document.querySelector("#download");
const zoomHelp = document.querySelector("#zoom-help");
const zoomCenter = document.querySelector("#zoom-center");
const activity = document.querySelector("#activity");
const activityEvents = document.querySelector("#activity-events");
const loadButton = document.querySelector("#load-button");
const fractalStyles = new Set(["mandelbrot", "seahorse"]);
let currentImage;

style.addEventListener("change", () => {
  iterations.disabled = !fractalStyles.has(style.value);
  document.querySelector("#iterations-field").hidden = iterations.disabled;
  seed.disabled = style.value !== "skyline";
  document.querySelector("#seed-field").hidden = seed.disabled;
});

function readSettings() {
  const values = new FormData(form);
  const settings = { style: values.get("style"), width: Number(values.get("width")),
    height: Number(values.get("height")) };
  if (values.has("max_iterations")) settings.max_iterations = Number(values.get("max_iterations"));
  if (values.has("seed")) settings.seed = Number(values.get("seed"));
  return settings;
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  if (response.status === 429) {
    const retry = response.headers.get("Retry-After");
    throw new Error(retry ? `Too many requests. Try again in ${retry} seconds.`
                          : "Too many requests. Try again shortly.");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : `Request failed (HTTP ${response.status}).`);
  }
  return { body: await response.json(), response };
}

async function submit(settings) {
  const { body, response } = await requestJson("/render", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  return { id: body.job_id, url: `/jobs/${encodeURIComponent(body.job_id)}`,
    traceId: response.headers.get("X-Trace-ID"), status: response.status };
}

async function waitForJob(submission, update, observe = () => {}) {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 750));
    const { body: job, response } = await requestJson(submission.url);
    observe(job, response);
    if (job.status === "failed") throw new Error("Rendering failed.");
    if (job.status === "completed") return job;
    update(job.status === "processing" ? "Rendering…" : "Queued…");
  }
  throw new Error("The job is still running. Check its status using the job ID.");
}

function setBusy(busy) {
  button.disabled = seedBatchButton.disabled = zoomCenter.disabled = loadButton.disabled = busy;
}

function addActivity(text) {
  const item = document.createElement("li");
  item.textContent = text;
  activityEvents.append(item);
}

function updateZoomControls() {
  const canZoom = !!currentImage && fractalStyles.has(currentImage.settings.style)
    && (currentImage.settings.viewport?.scale ?? 1) > 1 / 1024;
  preview.classList.toggle("zoomable", canZoom);
  zoomHelp.hidden = zoomCenter.hidden = !canZoom;
}

async function renderOne(settings, label, before) {
  setBusy(true);
  currentImage = undefined;
  updateZoomControls();
  details.hidden = preview.hidden = download.hidden = batchResults.hidden = true;
  activity.hidden = true;
  activityEvents.replaceChildren();
  previousFigure.hidden = !before;
  if (before) previous.src = before.url;
  statusLine.classList.remove("error");
  statusLine.textContent = "Submitting job…";

  try {
    const submission = await submit(settings);
    document.querySelector("#job-id").textContent = submission.id;
    document.querySelector("#trace-id").textContent = submission.traceId || "Unavailable";
    details.hidden = false;
    activity.hidden = false;
    activity.open = true;
    addActivity(`HTTP ${submission.status} Accepted · Job saved in PostgreSQL`);
    statusLine.textContent = "Queued…";
    let lastStatus;
    const job = await waitForJob(submission, (message) => { statusLine.textContent = message; }, (current) => {
      if (current.status === lastStatus) return;
      lastStatus = current.status;
      if (current.started_at) {
        const worker = current.worker_name || "Worker";
        if (current.status === "processing" || !activityEvents.querySelector("[data-worker-start]")) {
          addActivity(`${worker} started rendering at ${new Date(current.started_at).toLocaleTimeString()}`);
          activityEvents.lastElementChild.dataset.workerStart = "";
        }
      }
      if (current.status === "queued") addActivity("API reported the job as queued.");
      if (current.finished_at) addActivity(`Job ${current.status} at ${new Date(current.finished_at).toLocaleTimeString()}`);
    });
    const url = `${submission.url}/image`;
    const image = new Image();
    image.src = url;
    await image.decode();
    addActivity("Browser loaded the PNG.");
    preview.src = url;
    preview.alt = label;
    download.href = url;
    download.download = `${settings.style}-${submission.id}.png`;
    preview.hidden = download.hidden = false;
    currentImage = { url, settings, label };
    updateZoomControls();
    statusLine.textContent = job.worker_name ? `Completed. Worker ${job.worker_name}` : "Completed.";
    try {
      const { response: statusResponse } = await requestJson(submission.url);
      const cache = statusResponse.headers.get("X-Cache");
      if (cache) addActivity(`Repeated status read · X-Cache: ${cache}`);
    } catch {
      addActivity("Repeated status check failed.");
    }
  } catch (error) {
    statusLine.classList.add("error");
    statusLine.textContent = error.message;
    if (before) {
      currentImage = before;
      preview.src = before.url;
      preview.alt = before.label;
      preview.hidden = false;
      previousFigure.hidden = true;
      updateZoomControls();
    }
  } finally {
    setBusy(false);
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!button.disabled) renderOne(readSettings(), style.selectedOptions[0].textContent);
});

function zoom(x, y) {
  if (button.disabled || zoomHelp.hidden) return;
  const { settings, label } = currentImage;
  const viewport = settings.viewport ?? { x: .5, y: .5, scale: 1 };
  const clamp = (value) => Math.max(0, Math.min(1, value));
  renderOne({ ...settings, viewport: {
    x: clamp(viewport.x + (x - .5) * viewport.scale),
    y: clamp(viewport.y + (y - .5) * viewport.scale),
    scale: viewport.scale / 2,
  } }, label, currentImage);
}

preview.addEventListener("click", (event) => {
  const bounds = preview.getBoundingClientRect();
  zoom((event.clientX - bounds.left) / bounds.width, (event.clientY - bounds.top) / bounds.height);
});
zoomCenter.addEventListener("click", () => zoom(.5, .5));

async function renderBatchItem(settings, label) {
  const item = document.createElement("li");
  const line = document.createElement("div");
  item.append(line);
  batchResults.append(item);
  line.textContent = `${label} · Submitting…`;
  try {
    const submission = await submit(settings);
    const title = `${label} · Job ${submission.id}`;
    line.textContent = `${title} · Queued…`;
    const job = await waitForJob(submission, (message) => { line.textContent = `${title} · ${message}`; });
    line.textContent = `${title} · Completed · Worker ${job.worker_name ?? "unavailable"}`;
    const image = new Image();
    image.src = `${submission.url}/image`;
    await image.decode();
    image.alt = label;
    const link = document.createElement("a");
    link.href = image.src;
    link.download = `${settings.style}-${submission.id}.png`;
    link.textContent = "Download PNG";
    item.append(image, link);
    return true;
  } catch (error) {
    line.textContent += ` · ${error.message}`;
    line.classList.add("error");
    return false;
  }
}

async function renderBatch(makeEntries) {
  if (!form.reportValidity()) return;
  const { width, height } = readSettings();
  const entries = makeEntries({ width, height });
  setBusy(true);
  currentImage = undefined;
  updateZoomControls();
  details.hidden = preview.hidden = previousFigure.hidden = download.hidden = true;
  activity.hidden = true;
  batchResults.replaceChildren();
  batchResults.hidden = false;
  statusLine.classList.remove("error");
  statusLine.textContent = "Rendering batch…";
  try {
    const results = await Promise.all(entries.map(([settings, label]) => renderBatchItem(settings, label)));
    statusLine.textContent = `${results.filter(Boolean).length} of ${entries.length} images completed.`;
  } finally {
    setBusy(false);
  }
}

seedBatchButton.addEventListener("click", () => renderBatch((size) =>
  [42, 99, 42].map((value) => [{ ...size, style: "skyline", seed: value }, `Skyline seed ${value}`])
));
