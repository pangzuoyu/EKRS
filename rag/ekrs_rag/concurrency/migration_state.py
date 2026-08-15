"""Migration suppression state for FTS5 v1→v2 rebuild — Phase 12 F2.

Process-local ContextVar that the FTS5 migration orchestrator sets to True
during the rebuild window. ConsistencyChecker (Phase 10 T10a-2) reads this
flag and skips drift checks while set — during a rebuild, the FTS active
count is temporarily != Qdrant total by design (the rebuild is the source of
truth). Without suppression, every 5-minute check would emit
fts_consistency_drift + drift_total increments, polluting the audit log.

Rationale for ContextVar (over persistent table / Redis):
- Migration runs in a single orchestrated job (not multi-process fan-out).
- ContextVar is process-local by design; cannot leak across worker processes.
- No additional schema (no migration_status table) — keeps FTS5 schema clean.
- Reset is automatic at process exit; no stale state across deploys.

Multi-process migration (parallel workers) is out of scope for F2. If we
later need it, replace this with a persistent sentinel table or Redis key
read by ConsistencyChecker at check time.
"""

from __future__ import annotations

from contextvars import ContextVar

# Phase 12 F2: migration-in-progress flag. Default False (normal operation).
# Set True by migration orchestrator before calling FTSManager.replace_doc
# during v1→v2 rebuild. Cleared after the rebuild loop ends.
#
# Read sites:
# - ConsistencyChecker._check_once: skip drift audit when True.
_migration_in_progress: ContextVar[bool] = ContextVar(
    "fts_migration_in_progress", default=False,
)


def is_migration_in_progress() -> bool:
    """Return True if an FTS5 v1→v2 migration is currently in progress."""
    return _migration_in_progress.get()


def set_migration_in_progress(value: bool) -> object:
    """Set the migration-in-progress flag. Returns a Token for reset.

    Use the returned Token with :func:`reset_migration_in_progress` to
    restore the previous value (standard ContextVar API).
    """
    return _migration_in_progress.set(value)


def reset_migration_in_progress(token: object) -> None:
    """Restore migration flag to its previous value."""
    _migration_in_progress.reset(token)  # type: ignore[arg-type]