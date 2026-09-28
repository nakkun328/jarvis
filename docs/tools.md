# Tools and skills

Tools are the boundary between the core and external operations. Planned tool metadata includes name, description, input and output schemas, permission level, execution environment, timeout, and error handling. A registry will expose tools to the orchestrator. No external-operation tools are implemented in Phase 0.

Permission levels:

- Green: reads, searches, and status checks.
- Yellow: context-dependent changes such as file moves, shell commands, or non-destructive settings.
- Red: irreversible deletion, external sending, account changes, production publication, purchases, or destructive actions. These require explicit user confirmation.

Skills compose multiple tools into workflows such as research, development, or file organization. Execution records will preserve observations and verification results. A tool reporting success is insufficient to mark a task complete; the orchestrator checks the resulting state when possible.
