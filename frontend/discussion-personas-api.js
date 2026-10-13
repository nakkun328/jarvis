// Requests of the persona editor. Writes carry the fixed confirm header (see discussion-api.js);
// nothing but the six persona fields is ever sent.
import { DiscussionApiError, request } from "./discussion-api.js";
import { normalizeListing, normalizePreset } from "./discussion-view.js";

export const PERSONAS_URL = "/api/discussions/personas";
export const personaUrl = (name) => `${PERSONAS_URL}/${encodeURIComponent(name)}`;

const FIELDS = ["name", "role", "stance", "checklist", "model", "color"];
const pick = (body) => Object.fromEntries(FIELDS.map((key) => [key, body[key]]));

// { presets, custom, max_custom, personas_store }
export async function listPersonas(options = {}) {
  const data = await request(PERSONAS_URL, options);
  const presets = (Array.isArray(data?.presets) ? data.presets : []).map(normalizePreset).filter((p) => p !== null);
  return { presets, ...normalizeListing(data) };
}

function savedPersona(data) {
  const persona = normalizePreset(data);
  if (persona === null) throw new DiscussionApiError("format");
  return { ...persona, available: data.available === true, custom: true };
}

export async function createPersona(body, options = {}) {
  return savedPersona(await request(PERSONAS_URL, { ...options, method: "POST", body: pick(body) }));
}

export async function updatePersona(name, body, options = {}) {
  return savedPersona(await request(personaUrl(name), { ...options, method: "PUT", body: pick(body) }));
}

export async function deletePersona(name, options = {}) {
  const data = await request(personaUrl(name), { ...options, method: "DELETE" });
  if (data?.deleted !== true) throw new DiscussionApiError("format");
}
