"""Session context management for EKRS Phase 2b."""

from typing import Protocol


class SessionStore(Protocol):
    """Protocol for session storage backends."""

    def get(self, session_id: str) -> dict | None:
        """Retrieve session context by ID."""
        ...

    def set(self, session_id: str, context: dict) -> None:
        """Store session context."""
        ...

    def delete(self, session_id: str) -> None:
        """Delete session context."""
        ...


class InMemorySessionStore(SessionStore):
    """In-memory dict-based session store (thread-unsafe)."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    def get(self, session_id: str) -> dict | None:
        return self._store.get(session_id)

    def set(self, session_id: str, context: dict) -> None:
        self._store[session_id] = context

    def delete(self, session_id: str) -> None:
        self._store.pop(session_id, None)


class ContextManager:
    """Merges user-provided context with document-inferred and default context.

    Priority order (highest to lowest): user > explicit_doc > inferred_doc > default
    """

    def merge(
        self,
        user_context: dict,
        doc_context: dict,
        inferred: dict,
        default: dict,
    ) -> dict:
        """Deep merge contexts with priority ordering.

        Higher-priority values override lower-priority ones.
        Nested dicts are merged recursively.
        """
        # Merge in priority order: default (lowest) -> inferred -> doc_context -> user_context (highest)
        result = self._deep_merge(default, inferred)
        result = self._deep_merge(result, doc_context)
        result = self._deep_merge(result, user_context)
        return result

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """Recursively merge override into base, returning a new dict."""
        result = base.copy()
        for key, value in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
