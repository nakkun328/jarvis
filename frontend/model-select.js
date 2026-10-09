// The chat header's model selector. The server decides which models exist (GET /api/models,
// the JARVIS_MODEL_CHOICES allowlist); the browser only remembers which entry the owner picked
// and sends that exact string with each chat request. With no list from the server nothing is
// shown and chat requests are exactly what they were before.
const STORAGE_KEY = "jarvis.modelChoice";
const MAX_OPTIONS = 8;

export function parseModels(data) {
  if (!Array.isArray(data)) return [];
  const models = [];
  for (const item of data.slice(0, MAX_OPTIONS)) {
    if (
      item && typeof item === "object" &&
      typeof item.id === "string" && item.id &&
      typeof item.provider === "string" && typeof item.model === "string"
    ) {
      models.push({
        id: item.id,
        provider: item.provider,
        model: item.model,
        available: item.available === true,
        isDefault: item.is_default === true,
      });
    }
  }
  return models;
}

// Never throws: any failure (offline, 401, bad body, no endpoint) means "no selector".
export async function fetchModels(fetchImpl = fetch) {
  try {
    const response = await fetchImpl("/api/models", { headers: { Accept: "application/json" } });
    if (!response.ok) return [];
    return parseModels(await response.json());
  } catch {
    return [];
  }
}

function readStored(storage) {
  try {
    return storage?.getItem(STORAGE_KEY) || null;
  } catch {
    return null;
  }
}

function writeStored(storage, value) {
  try {
    if (value) storage?.setItem(STORAGE_KEY, value);
    else storage?.removeItem(STORAGE_KEY);
  } catch {
    // Remembering the choice is a convenience only.
  }
}

// The stored choice if it is still offered and usable, otherwise null (the server default).
export function resolveChoice(models, stored) {
  if (!stored) return null;
  return models.some((model) => model.id === stored && model.available) ? stored : null;
}

export function optionLabel(model) {
  const base = `${model.provider} · ${model.model}`;
  if (!model.available) return `${base}（利用不可）`;
  return model.isDefault ? `${base}（既定）` : base;
}

// Builds the control inside `mount` (text nodes only) and returns { value, setDisabled }.
// `value` is the allowlist entry to send, or null for the default.
export async function mountModelSelect(doc, mount, { fetchImpl = fetch, storage = null } = {}) {
  const models = await fetchModels(fetchImpl);
  let current = null;
  const api = {
    get value() {
      return current;
    },
    setDisabled() {},
  };
  if (!mount || models.length === 0) return api;

  const label = doc.createElement("label");
  label.className = "model-select-label";
  label.htmlFor = "model-choice";
  label.textContent = "モデル";
  const select = doc.createElement("select");
  select.id = "model-choice";
  select.className = "model-select-control";
  const standard = doc.createElement("option");
  standard.value = "";
  standard.textContent = "既定のモデル";
  select.append(standard);
  for (const model of models) {
    const option = doc.createElement("option");
    option.value = model.id;
    option.textContent = optionLabel(model);
    option.disabled = !model.available;
    select.append(option);
  }
  current = resolveChoice(models, readStored(storage));
  select.value = current ?? "";
  select.addEventListener("change", () => {
    current = resolveChoice(models, select.value);
    writeStored(storage, current);
  });
  if (current === null && readStored(storage)) writeStored(storage, null);
  mount.replaceChildren(label, select);
  mount.hidden = false;
  api.setDisabled = (disabled) => {
    select.disabled = Boolean(disabled);
  };
  return api;
}
