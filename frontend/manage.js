"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const caseId = byId("case-id");
  const confirmation = byId("confirm-id");
  const previewPanel = byId("preview");
  const reviewButton = byId("review-button");
  const deleteButton = byId("delete-button");
  const cancelButton = byId("cancel-button");
  let preview = null;
  let requestNumber = 0;
  let controller = null;
  let deleting = false;

  function feedback(message, tone = "") {
    byId("feedback").textContent = message;
    byId("feedback").dataset.tone = tone;
  }

  function updateDeleteButton() {
    deleteButton.disabled = deleting || !preview || confirmation.value !== preview.case_id;
  }

  function resetPreview() {
    preview = null;
    previewPanel.hidden = true;
    confirmation.value = "";
    for (const id of ["preview-id", "description", "case-meta", "observation-count", "test-count", "history-count"]) {
      byId(id).textContent = "";
    }
    updateDeleteButton();
  }

  function cancelLookup() {
    requestNumber += 1;
    controller?.abort();
    controller = null;
    reviewButton.disabled = false;
  }

  async function request(url, options) {
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store", ...options });
    const body = await response.json();
    if (!response.ok) {
      const error = new Error(typeof body.detail === "string" ? body.detail : "Check the case ID and try again.");
      error.status = response.status;
      throw error;
    }
    return body;
  }

  byId("lookup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (deleting) return;
    cancelLookup();
    resetPreview();
    const id = caseId.value.trim();
    if (!id) return;
    const currentRequest = requestNumber;
    controller = new AbortController();
    reviewButton.disabled = true;
    feedback("Loading your case…");
    try {
      const result = await request(`/cases/${encodeURIComponent(id)}/deletion-preview`, { signal: controller.signal });
      if (currentRequest !== requestNumber) return;
      preview = result;
      byId("preview-id").textContent = result.case_id;
      byId("description").textContent = result.player_description;
      byId("case-meta").textContent = `${result.origin === "synthetic" ? "Synthetic demo" : "Player report"} · ${result.status}`;
      byId("observation-count").textContent = result.observation_count;
      byId("test-count").textContent = result.test_result_count;
      byId("history-count").textContent = result.history_event_count;
      previewPanel.hidden = false;
      feedback("Review the case below. Nothing has been deleted.");
      confirmation.focus();
    } catch (error) {
      if (currentRequest !== requestNumber || error.name === "AbortError") return;
      feedback(error.message || "The case could not be loaded.", "error");
    } finally {
      if (currentRequest === requestNumber) reviewButton.disabled = false;
    }
  });

  caseId.addEventListener("input", () => {
    if (deleting) return;
    cancelLookup();
    resetPreview();
    feedback("");
  });
  confirmation.addEventListener("input", updateDeleteButton);
  cancelButton.addEventListener("click", () => {
    if (deleting) return;
    cancelLookup();
    resetPreview();
    feedback("Cancelled. Nothing has been deleted.");
    caseId.focus();
  });

  byId("delete-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (deleting || !preview || confirmation.value !== preview.case_id) return;
    const selected = preview;
    deleting = true;
    caseId.disabled = true;
    confirmation.disabled = true;
    reviewButton.disabled = true;
    cancelButton.disabled = true;
    updateDeleteButton();
    feedback("Deleting the selected case…");
    try {
      await request(`/cases/${encodeURIComponent(selected.case_id)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm_case_id: selected.case_id, deletion_token: selected.deletion_token }),
      });
      resetPreview();
      caseId.value = "";
      feedback("Case and evidence history deleted from the active app.", "success");
    } catch (error) {
      // A lost response may follow a committed deletion. Require a fresh lookup.
      resetPreview();
      feedback(
        error.status ? error.message : "The result could not be confirmed. Review the case again to check its status.",
        "error",
      );
    } finally {
      deleting = false;
      caseId.disabled = false;
      confirmation.disabled = false;
      reviewButton.disabled = false;
      cancelButton.disabled = false;
      updateDeleteButton();
      caseId.focus();
    }
  });
})();
