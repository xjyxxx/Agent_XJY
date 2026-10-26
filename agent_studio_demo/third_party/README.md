# Third-party snapshots

## llm_agents

`llm_agents/` is a local source snapshot copied into this project from the user-provided `llm_agents-main` checkout.

- Upstream project: `mpaepper/llm_agents`
- License: MIT; see `llm_agents/LICENSE`
- Purpose: Day2 ReAct source-reading reference
- Runtime rule: the Demo does not import code from the external checkout; future integrations must use this in-project snapshot or be implemented in the Demo's own modules.
