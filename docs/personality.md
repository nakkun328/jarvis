# Personality settings

JARVIS keeps one identity across devices, so personality is managed by the core backend, never by a client. Six items control tone and style. Each item selects one level from a fixed list; the backend turns the selected levels into fixed, reviewed instruction sentences in the system prompt. Settings never carry prompt text of their own.

## Items, levels, and initial values

| Item | Levels (initial value in bold) | Effect |
| --- | --- | --- |
| `formality` | `casual`, **`polite`**, `formal` | Register of address. `polite` is plain and friendly and avoids a formal butler voice. |
| `humor` | `off`, **`light`**, `moderate` | `light` allows humor only when it fits. |
| `sarcasm` | **`off`**, `light` | `light` allows mild, dry sarcasm only when clearly harmless and never aimed at the user. |
| `initiative` | `none`, **`one`**, `few` | How many relevant next steps JARVIS may offer unprompted: none, at most one, or at most three. A suggestion is only an offer; JARVIS never acts on it without a request. |
| `verbosity` | `brief`, **`concise`**, `detailed` | Reply length and depth. |
| `caution` | **`standard`**, `high` | `standard` states uncertainty plainly. `high` also says what should be verified and asks a short clarifying question when a misread request could cause harm or be hard to undo. There is no level below `standard`. |

The initial values reproduce the original single prompt: calm, polite, concise, capable, light humor only when it fits, no exaggerated enthusiasm, uncertainty stated plainly, and at most one relevant next step. Sarcasm starts `off` because the original prompt never asked for it; the design notes allow light sarcasm only when fitting, so `light` is available as an opt-in.

## What settings cannot change

These sentences are always rendered, in every combination, and no setting can remove or weaken them:

- JARVIS replies in the user's language and keeps the user's request central.
- JARVIS does not claim to have searched, remembered, used a tool, or completed an action unless that actually happened.
- Enthusiasm and praise stay restrained.
- The style lines are defaults for wording. The user's explicit requests about tone or length take priority over them, and they never change the fixed rules.

Personality settings do not grant or restrict tools, permissions, memory access, or provider behavior. The retrieved-memory notice added by the chat service is also independent of them. The file has no free-text field, so it cannot inject instructions; any unknown key is an error.

## File format and location

Set `JARVIS_PERSONALITY_PATH` to a TOML file. Without the variable, the initial values above are used. The variable is read from the process environment like other settings; `.env` is not loaded automatically. A relative path is resolved against the process working directory, and `~` is expanded.

```toml
version = 1

[personality]
formality = "polite"
humor = "light"
sarcasm = "off"
initiative = "one"
verbosity = "concise"
caution = "standard"
```

Every key is optional; an omitted item keeps its initial value, and an empty file means all initial values. `version` is optional and, when present, must be `1`. Level names are exact lowercase strings.

## Validation

The file is read once at startup, and an invalid file stops the application from starting (`ConfigError`) rather than falling back silently. It is rejected when:

- `JARVIS_PERSONALITY_PATH` is set but empty, missing, unreadable, a directory or other non-regular file, or a symlink. Symlinks are refused in the final path component only; parent directories are resolved by the operating system. A configured path that does not exist is an error, because a mistyped path would otherwise quietly leave the initial values in place.
- It is larger than 4,096 bytes, not valid UTF-8, or not valid TOML (including duplicate keys).
- A top-level key other than `version` and `[personality]`, or an item name other than the six above, appears.
- A value is not a string or not one of the listed levels (case matters).

Error messages name the item and the allowed levels, but do not repeat file contents or the file path. At startup the log shows the selected level names and whether they came from `file` or `default`.

## Not included yet

Changes require editing the file and restarting the service. Saving settings across devices, a UI for editing them, and per-conversation overrides are later steps; the file is read by the backend host only, so all clients served by that backend share the same personality.
