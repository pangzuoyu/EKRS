/**
 * Phase 11 T11-2 — X-Admin-Key localStorage helpers.
 *
 * The RAG backend gates `/v1/admin/*` endpoints behind an X-Admin-Key header
 * (see `rag/ekrs_rag/security.py:require_admin_key`). For dev-only operators
 * using the React UI, the key is stored in localStorage so it survives page
 * reloads but never leaves the browser. The key is NEVER sent to any backend
 * other than the configured RAG service (the typed client attaches it only
 * to `/v1/admin/*` requests via `createApiClient({ getAdminKey })`).
 *
 * Behaviour:
 *   - Pure helpers tolerate a missing localStorage (private browsing → null).
 *   - `useAdminKey()` is reactive: it re-renders when the value changes in
 *     this tab (via the custom `ekrs:admin_key_changed` event fired by
 *     `setAdminKey`/`clearAdminKey`) or in another tab (via the browser
 *     `storage` event). `useSyncExternalStore` keeps the read tear-free.
 */
import { useSyncExternalStore } from "react";

export const ADMIN_KEY_STORAGE_KEY = "ekrs.admin_key";
const SAME_TAB_EVENT = "ekrs:admin_key_changed";

function safeRead(): string | null {
  try {
    return localStorage.getItem(ADMIN_KEY_STORAGE_KEY);
  } catch {
    return null;
  }
}

function safeWrite(value: string | null): void {
  try {
    if (value === null) {
      localStorage.removeItem(ADMIN_KEY_STORAGE_KEY);
    } else {
      localStorage.setItem(ADMIN_KEY_STORAGE_KEY, value);
    }
  } catch {
    // localStorage unavailable (private browsing, quota). Best-effort.
  }
  // Fire same-tab notification; storage events don't fire in the writer's tab.
  window.dispatchEvent(new Event(SAME_TAB_EVENT));
}

export function getAdminKey(): string | null {
  return safeRead();
}

export function setAdminKey(value: string): void {
  safeWrite(value);
}

export function clearAdminKey(): void {
  safeWrite(null);
}

export function hasAdminKey(): boolean {
  return safeRead() !== null;
}

// --- React hook -----------------------------------------------------------

const subscribe = (callback: () => void): (() => void) => {
  window.addEventListener(SAME_TAB_EVENT, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(SAME_TAB_EVENT, callback);
    window.removeEventListener("storage", callback);
  };
};

/**
 * Reactive read of the admin key. Re-renders when:
 *   - this tab calls `setAdminKey` / `clearAdminKey` (SAME_TAB_EVENT),
 *   - another tab dispatches a `storage` event (browser-native).
 */
export function useAdminKey(): string | null {
  return useSyncExternalStore(subscribe, safeRead, () => null);
}
