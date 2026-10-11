// Cancels a chat-tool task from the Tasks screen. Kept apart from tasks-api.js, which only
// ever reads.
export function cancelUrl(taskId) {
  return `/api/tasks/${encodeURIComponent(taskId)}/cancel`;
}

// Cancels a chat-tool task. The fixed header is what the server requires of every unsafe route
// here; there is no body. Resolves true when the server accepted it.
export async function cancelTask(taskId, { fetchImpl = fetch } = {}) {
  try {
    const response = await fetchImpl(cancelUrl(taskId), {
      method: "POST",
      headers: { "X-Jarvis-Confirm": "1", Accept: "application/json" },
    });
    return response.ok;
  } catch {
    return false;
  }
}
