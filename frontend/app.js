const $ = (id) => document.getElementById(id);

const testNames = {
  compare_data_centers: "Compare data centres",
  compare_background_traffic: "Compare background traffic",
  verify_improvement: "Verify the improvement",
};

let activeCase = null;
let activeTest = null;
let pending = null;
let busy = false;
let activeDecision = null;
let explanationController = null;
let explanationVersion = 0;

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").dataset.error = String(error);
}

function updateControls() {
  document.querySelectorAll("button, input, select, textarea").forEach((control) => {
    control.disabled = busy || pending !== null;
  });

  $("retry").hidden = pending === null;
  $("retry").disabled = busy;
  $("explain-step").disabled =
    busy ||
    pending !== null ||
    !activeCase ||
    !activeDecision ||
    explanationController !== null;
}

async function run(task) {
  if (busy) return;

  busy = true;
  resetExplanation();
  updateControls();
  notice("Working...");

  try {
    await task();
  } catch (error) {
    const retryNote = pending
      ? " Keep this tab open and use Retry saved submission."
      : "";

    notice(error.message + retryNote, true);
  } finally {
    busy = false;
    updateControls();
  }
}

async function api(path, body, signal) {
  const response = await fetch(path, {
    signal,
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined
      ? {}
      : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });

  const data = await response.json().catch(() => null);

  if (!response.ok) {
    const detail = data?.detail;

    const message = Array.isArray(detail)
      ? detail.map((item) => `${item.loc.join(".")}: ${item.msg}`).join("; ")
      : typeof detail === "string"
        ? detail
        : `Request failed (${response.status}).`;

    const error = new Error(message);
    error.status = response.status;
    throw error;
  }

  if (data === null) {
    throw new Error("The server returned an unreadable response.");
  }

  return data;
}

function readingText(entry) {
  return `Latency: ${entry.latency_ms ?? "unknown"} ms; packet loss: ${entry.packet_loss_pct ?? "unknown"}%; FPS: ${entry.fps ?? "unknown"}.`;
}

function eventText(event) {
  const entry = event.entry;

  if (event.event_type === "answer") {
    return `Symptom recorded: ${entry.symptom.replaceAll("_", " ")}.`;
  }

  if (event.event_type === "observation") {
    return `${entry.context}\n${readingText(entry)}`;
  }

  if (event.event_type === "test") {
    return `${testNames[entry.test_id] ?? entry.test_id}: ${entry.outcome}\n${entry.conditions}\n${entry.notes}`;
  }

  return "Recorded update.";
}

function renderGuidance(cards) {
  const container = $("guidance");
  container.replaceChildren();
  $("guidance-panel").hidden = cards.length === 0;

  for (const card of cards) {
    const block = document.createElement("div");
    const title = document.createElement("h4");
    const label = document.createElement("p");
    const text = document.createElement("p");

    title.textContent = card.title;

    label.className = "muted";
    label.textContent = card.basis === "official_guidance"
      ? "Summary of official guidance"
      : "ApexMedic workflow policy";

    text.textContent = card.text;
    block.append(title, label, text);

    for (const source of card.citations) {
      const reference = document.createElement("p");
      const link = document.createElement("a");

      link.href = source.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = `${source.publisher}: ${source.title}`;

      reference.append(
        link,
        document.createTextNode(
          ` — reviewed ${source.reviewed_on}`,
        ),
      );

      block.append(reference);
    }

    container.append(block);
  }
}

async function loadCase(id, message = "Case loaded.") {
  if (activeCase?.case_id !== id) {
    ["answer-form", "reading-form", "test-form"].forEach((name) => {
      $(name).reset();
    });
  }

  $("case-panel").hidden = true;
  $("open-id").value = id;

  activeCase = null;
  activeTest = null;
  activeDecision = null;

  const path = `/cases/${encodeURIComponent(id)}`;

  const [record, decision, events] = await Promise.all([
    api(path),
    api(`${path}/next-step`),
    api(`${path}/history`),
  ]);

  activeCase = record;
  activeDecision = decision;

  $("case-meta").textContent =
    `${record.case_id} | ${record.origin === "synthetic" ? "Synthetic demo" : "Player report"} | ${record.status}`;

  $("case-description").textContent = record.player_description;

  const latest = record.observations.at(-1);

  $("latest-reading").textContent = latest
    ? `Latest recorded reading: ${readingText(latest)}`
    : "No readings recorded.";

  $("next-kind").textContent = {
    question: "Next question",
    test: "Next test",
    report: "Review",
  }[decision.kind];

  $("next-message").textContent = decision.message;
  $("next-reason").textContent = decision.reason;

  renderGuidance(decision.guidance ?? []);

  const editable = record.status === "investigating";

  $("answer-form").hidden =
    !(editable && decision.action_id === "clarify_symptom");

  $("reading-form").hidden =
    !(editable && record.symptom !== "unknown");

  const candidate = decision.kind === "test"
    ? decision.action_id
    : decision.action_id === "clarify_test_result"
      ? record.completed_tests.at(-1)?.test_id
      : null;

  activeTest = editable && Object.hasOwn(testNames, candidate)
    ? candidate
    : null;

  $("test-form").hidden = activeTest === null;
  $("test-name").textContent = activeTest ? testNames[activeTest] : "";

  $("history").replaceChildren();

  for (const event of events) {
    const item = document.createElement("li");
    const time = document.createElement("strong");
    const text = document.createElement("p");

    time.textContent =
      `Recorded ${new Date(event.recorded_at).toLocaleString()}`;

    text.textContent = eventText(event);

    item.append(time, text);
    $("history").append(item);
  }

  if (events.length === 0) {
    const item = document.createElement("li");
    item.textContent = "No updates recorded after creation.";
    $("history").append(item);
  }

  $("case-panel").hidden = false;
  notice(message);
}

async function sendPending() {
  const submission = pending;
  if (!submission) return;

  let receipt;

  try {
    receipt = await api(
      `/cases/${encodeURIComponent(submission.caseId)}/${submission.route}`,
      submission.body,
    );
  } catch (error) {
    if (error.status >= 400 && error.status < 500) {
      pending = null;
    }
    throw error;
  }

  pending = null;
  $(submission.formId).reset();

  try {
    await loadCase(
      submission.caseId,
      receipt.added
        ? "Evidence saved."
        : "Already saved; no duplicate added.",
    );
  } catch (error) {
    throw new Error(
      `Evidence was saved. Use Open case to reload its view. ${error.message}`,
    );
  }
}

async function saveEvent(formId, route, entry) {
  if (!activeCase) {
    throw new Error("Open a case first.");
  }

  pending = {
    caseId: activeCase.case_id,
    route,
    formId,
    body: {
      event_id: crypto.randomUUID(),
      entry,
    },
  };

  await sendPending();
}

function onSubmit(id, task) {
  $(id).addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pending) run(task);
  });
}

function metric(id) {
  const value = $(id).value.trim();
  return value === "" ? null : Number(value);
}

onSubmit("create-form", async () => {
  let record;

  try {
    record = await api("/cases", {
      player_description: $("description").value.trim(),
      origin: $("origin").value,
      connection_type: $("connection").value,
    });
  } catch (error) {
    if (!error.status || error.status >= 500) {
      throw new Error(
        "Creation could not be confirmed. A case may have been saved; creating again could make another case.",
      );
    }
    throw error;
  }

  $("create-form").reset();

  try {
    await loadCase(
      record.case_id,
      "Case created. Keep its ID to reopen it.",
    );
  } catch (error) {
    throw new Error(
      `Case created: ${record.case_id}. Use Open case to reload its view. ${error.message}`,
    );
  }
});

onSubmit("open-form", () => {
  return loadCase($("open-id").value.trim());
});

onSubmit("answer-form", () => {
  return saveEvent("answer-form", "answers", {
    question_id: "clarify_symptom",
    symptom: $("symptom").value,
  });
});

onSubmit("reading-form", () => {
  const entry = {
    source: "player_report",
    context: $("context").value.trim(),
    latency_ms: metric("latency"),
    packet_loss_pct: metric("loss"),
    fps: metric("fps"),
  };

  const measurements = [
    entry.latency_ms,
    entry.packet_loss_pct,
    entry.fps,
  ];

  if (measurements.every((value) => value === null)) {
    throw new Error("Enter at least one measured value.");
  }

  return saveEvent("reading-form", "observations", entry);
});

onSubmit("test-form", () => {
  if (!activeTest) {
    throw new Error("There is no active test to record.");
  }

  return saveEvent("test-form", "tests", {
    test_id: activeTest,
    conditions: $("conditions").value.trim(),
    outcome: $("outcome").value,
    notes: $("notes").value.trim(),
  });
});

$("refresh").addEventListener("click", () => {
  if (activeCase && !pending) {
    run(() => loadCase(activeCase.case_id));
  }
});

$("retry").addEventListener("click", () => {
  run(sendPending);
});

function resetExplanation() {
  explanationVersion += 1;

  explanationController?.abort();
  explanationController = null;

  $("model-explanation").textContent = "";
  $("explanation-status").textContent = "";
}

async function requestExplanation() {
  if (
    busy ||
    pending ||
    !activeCase ||
    !activeDecision ||
    explanationController
  ) {
    return;
  }

  const version = ++explanationVersion;
  const caseId = activeCase.case_id;
  const expected = activeDecision;
  const controller = new AbortController();

  explanationController = controller;
  updateControls();

  $("model-explanation").textContent = "";
  $("explanation-status").textContent =
    expected.kind === "report"
      ? "Loading reviewed guidance."
      : "Selecting supporting guidance for this step.";

  const timer = setTimeout(
    () => controller.abort(),
    130000,
  );

  try {
    const result = await api(
      `/cases/${encodeURIComponent(caseId)}/explanation`,
      { decision_token: expected.decision_token },
      controller.signal,
    );

    if (
      version !== explanationVersion ||
      activeCase?.case_id !== caseId
    ) {
      return;
    }

    if (
      result.case_id !== caseId ||
      result.action_id !== expected.action_id ||
      result.knowledge_revision !== expected.knowledge_revision
    ) {
      throw new Error(
        "The explanation does not match this recommendation. Refresh the case.",
      );
    }

    $("model-explanation").textContent = result.explanation;
    $("explanation-status").textContent =
      result.source === "reviewed_guidance"
        ? "Reviewed guidance. Uses the existing recommendation and reason."
        : "Reviewed wording selected with model assistance. The recommended action is preserved.";

  } catch (error) {
    if (version !== explanationVersion) return;

    $("explanation-status").textContent =
      error.name === "AbortError"
        ? "The explanation request timed out. Your original recommendation is still available."
        : error.message;

  } finally {
    clearTimeout(timer);

    if (version === explanationVersion) {
      explanationController = null;
      updateControls();
    }
  }
}

$("explain-step").addEventListener(
  "click",
  requestExplanation,
);
