# Obsidian Markdown vault adapter

`backend.memory.obsidian.ObsidianVault` stores one long-term memory note per UUID as
`<uuid>.md` in a configured vault directory. It is an API-key-free storage component;
the memory model and retrieval pipeline will call it in later tasks.

```python
from pathlib import Path
from uuid import uuid4

from backend.memory.obsidian import ObsidianVault

vault = ObsidianVault(Path("/path/to/Obsidian/JARVIS"))
note = vault.create(uuid4(), "# Project\n\nThe user's edited text.\n", {"category": "project"})
current = vault.read(note.memory_id)
if current is not None:
    vault.update(
        current.memory_id,
        "# Project\n\nUpdated text.\n",
        current.metadata,
        expected_revision=current.revision,
    )
```

The first field in YAML frontmatter is `id`; additional fields may contain strings,
finite numbers, booleans, null, or lists of strings. Values are written using JSON
scalar/list syntax, which YAML accepts. People may edit the Markdown body and these
simple frontmatter values in Obsidian. Other YAML forms are not yet parsed by this
adapter. Keep the `id` equal to the filename UUID.

Retrieval uses human edits to the body, importance, confidence, tags, and project.
Category, source, origin, ID, and creation time are provenance fields; changing them
requires review before the note can be returned as an approved memory.

`create` fails if the UUID note already exists. `update` requires the SHA-256 revision
returned by `create` or `read`, and raises `VaultConflictError` when the file changed.
Callers should re-read and resolve changes rather than blindly retrying. Writes use a
completed temporary file in the vault and an atomic filesystem link or replacement.
The note and root must not be symlinks; the adapter accepts only UUID note IDs, so a
caller cannot supply a path. External editors do not share the adapter's lock, so a
simultaneous edit between its final revision check and replacement remains possible.
