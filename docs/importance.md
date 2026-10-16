# Memory importance (JAR-28)

The design scale is **0 to 5**; the stored `importance` stays **0 to 1** (schema, validation, API and UI are unchanged). The mapping is `score = level / 5`, and `round(score * 5)` recovers the level.

| Level | Meaning |
| --- | --- |
| 0 | noise: nothing worth keeping |
| 1 | minor: harmless detail, may be forgotten |
| 2 | useful: occasionally relevant context |
| 3 | important: shapes ordinary answers about the user or a project |
| 4 | high: stated explicitly by the user and affects many answers |
| 5 | critical: the user asked to remember it and it is a standing fact or rule |

`backend/memory/importance.py` provides `assess_importance(category, origin, content)`, a deterministic rule set that returns the level, the stored score and the reasons:

- base by category: user/project 3, work_state/self 2, conversation/temporary 1;
- origin: `user_explicit` +1, `research` and `ai_inference` -1, `discussion` -2;
- "remember"-style request +1; standing rule (いつも, 今後, always...) +1; hedge (たぶん, maybe...) -1;
- caps: `ai_inference` at 2, `discussion` at 1, `research` at 3, so an AI opinion never outranks a user statement.

Limits: this is a recommended scale, not a measured one. The existing writers (candidate detection, chat auto memory, research, discussion) keep their fixed values; nothing calls `assess_importance` yet, and no stored value is changed. A reviewer can still edit importance in the vault note. Wiring it into a writer, or changing the public schema to 0-5, is a separate decision for the maintainer (JAR-88).
