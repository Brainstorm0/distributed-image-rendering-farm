const loadStatus = document.querySelector("#load-status");
const loadResults = document.querySelector("#load-results");
const testSettings = { width: 256, height: 256, style: "mandelbrot", max_iterations: 100 };

async function timedFetch(url, options) {
  const start = performance.now();
  const response = await fetch(url, { signal: AbortSignal.timeout(10_000), ...options });
  return { response, elapsed: performance.now() - start };
}

async function sendTestRequest() {
  try {
    const { response, elapsed } = await timedFetch("/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(testSettings),
    });
    const job = response.status === 202 ? await response.json() : null;
    return { status: response.status, elapsed, id: job?.job_id };
  } catch {
    return { status: "Network error" };
  }
}

function percentile(values, fraction) {
  if (!values.length) return "—";
  const sorted = [...values].sort((a, b) => a - b);
  return `${sorted[Math.ceil(fraction * sorted.length) - 1].toFixed(2)} ms`;
}

async function checkTestJob(id) {
  const url = `/jobs/${encodeURIComponent(id)}`;
  const deadline = performance.now() + 30_000;
  try {
    while (performance.now() < deadline) {
      const { response } = await timedFetch(url);
      if (!response.ok) return { status: "Polling error" };
      const job = await response.json();
      if (job.status === "failed") return { status: "Failed" };
      if (job.status === "completed") {
        const png = await timedFetch(`${url}/image`);
        const signature = new Uint8Array(await png.response.arrayBuffer(), 0).slice(0, 8);
        const valid = png.response.ok && signature.join(",") === "137,80,78,71,13,10,26,10";
        return { status: "Completed", valid, url };
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    return { status: "Timed out" };
  } catch {
    return { status: "Polling error" };
  }
}

async function readTestStatus(url) {
  try {
    const { response, elapsed } = await timedFetch(url);
    await response.arrayBuffer();
    return { ok: response.ok, cache: response.headers.get("X-Cache"), elapsed };
  } catch {
    return { ok: false };
  }
}

function addMetric(label, value) {
  const name = document.createElement("dt");
  const result = document.createElement("dd");
  name.textContent = label;
  result.textContent = value;
  loadResults.append(name, result);
}

loadButton.addEventListener("click", async () => {
  setBusy(true);
  loadResults.hidden = true;
  loadResults.replaceChildren();
  loadStatus.textContent = "Waiting 5 seconds for the gateway’s rate limit to refill…";
  try {
    await new Promise((resolve) => setTimeout(resolve, 5000));
    loadStatus.textContent = "Submitting 20 requests…";
    const start = performance.now();
    const submissions = [];
    for (let batch = 0; batch < 2; batch++) {
      submissions.push(...await Promise.all(Array.from({ length: 10 }, sendTestRequest)));
    }
    const accepted = submissions.filter(({ status, id }) => status === 202 && id);
    const limited = submissions.filter(({ status }) => status === 429);
    loadStatus.textContent = `Checking ${accepted.length} accepted jobs…`;
    const jobs = await Promise.all(accepted.map(({ id }) => checkTestJob(id)));
    const completed = jobs.filter(({ status }) => status === "Completed");
    const seconds = (performance.now() - start) / 1000;
    const reads = [];
    for (let round = 0; round < 5; round++) {
      reads.push(...await Promise.all(completed.map(({ url }) => readTestStatus(url))));
    }
    const submissionTimes = submissions.flatMap(({ elapsed }) => elapsed === undefined ? [] : [elapsed]);
    const readTimes = reads.filter(({ ok }) => ok).map(({ elapsed }) => elapsed);
    addMetric("Accepted (202)", accepted.length);
    addMetric("Rate-limited (429)", limited.length);
    addMetric("Other submission errors", submissions.length - accepted.length - limited.length);
    addMetric("Completed renders", completed.length);
    addMetric("Failed or unconfirmed jobs", jobs.length - completed.length);
    addMetric("Valid PNGs", completed.filter(({ valid }) => valid).length);
    addMetric("Completed renders per second", (completed.length / seconds).toFixed(2));
    addMetric("Submission latency p50 / p95", `${percentile(submissionTimes, .5)} / ${percentile(submissionTimes, .95)}`);
    addMetric("Cache hits", `${reads.filter(({ ok, cache }) => ok && cache === "HIT").length} of ${reads.length} repeated reads`);
    addMetric("Status read errors", reads.length - readTimes.length);
    addMetric("Status read latency p50 / p95", `${percentile(readTimes, .5)} / ${percentile(readTimes, .95)}`);
    loadStatus.textContent = "Load test finished.";
    loadResults.hidden = false;
  } catch (error) {
    loadStatus.textContent = `Load test could not finish. ${error.message}`;
  } finally {
    setBusy(false);
  }
});
