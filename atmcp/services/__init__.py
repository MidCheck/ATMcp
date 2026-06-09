"""Service layer: one module per concern (identity, presence, knowledge, memory,
tasks, status). Each function owns its transaction and emits events via
``atmcp.events.append`` so mutations follow commit-then-publish."""
