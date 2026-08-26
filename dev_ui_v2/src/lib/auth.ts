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
 *
 * Phase 13c post-closure patch — added parser-token helpers mirroring the
 * admin-key pattern for the X-Parser-Token header used by `/v1/constraints`,
 * `/v1/ingestion/notify`, `/v1/ingestion/status/{hash}`, `/v1/blocks/{id}`
 * (see `rag/ekrs_rag/security.py:require_parser_token`). Distinct storage
 * key so admin operators and parser-side operators don't collide.
 */
import { useSyncExternalStore } from "react";

export const ADMIN_KEY_STORAGE_KEY = "ekrs.admin_key";
const ADMIN_SAME_TAB_EVENT = "ekrs:admin_key_changed";

export const PARSER_TOKEN_STORAGE_KEY = "ekrs.parser_token";
const PARSER_SAME_TAB_EVENT = "ekrs:parser_token_changed";

// --- Admin-key helpers ----------------------------------------------------

function adminSafeRead(): string | null {
  try {
    return localStorage.getItem(ADMIN_KEY_STORAGE_KEY);
  } catch {
    return null;
  }
}

function adminSafeWrite(value: string | null): void {
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
  window.dispatchEvent(new Event(ADMIN_SAME_TAB_EVENT));
}

export function getAdminKey(): string | null {
  return adminSafeRead();
}

export function setAdminKey(value: string): void {
  adminSafeWrite(value);
}

export function clearAdminKey(): void {
  adminSafeWrite(null);
}

export function hasAdminKey(): boolean {
  return adminSafeRead() !== null;
}

const subscribeAdmin = (callback: () => void): (() => void) => {
  window.addEventListener(ADMIN_SAME_TAB_EVENT, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(ADMIN_SAME_TAB_EVENT, callback);
    window.removeEventListener("storage", callback);
  };
};

/**
 * Reactive read of the admin key. Re-renders when:
 *   - this tab calls `setAdminKey` / `clearAdminKey` (SAME_TAB_EVENT),
 *   - another tab dispatches a `storage` event (browser-native).
 */
export function useAdminKey(): string | null {
  return useSyncExternalStore(subscribeAdmin, adminSafeRead, () => null);
}

// --- Parser-token helpers (Phase 13c post-closure patch) -----------------

function parserSafeRead(): string | null {
  try {
    return localStorage.getItem(PARSER_TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

function parserSafeWrite(value: string | null): void {
  try {
    if (value === null) {
      localStorage.removeItem(PARSER_TOKEN_STORAGE_KEY);
    } else {
      localStorage.setItem(PARSER_TOKEN_STORAGE_KEY, value);
    }
  } catch {
    // localStorage unavailable (private browsing, quota). Best-effort.
  }
  window.dispatchEvent(new Event(PARSER_SAME_TAB_EVENT));
}

export function getParserToken(): string | null {
  return parserSafeRead();
}

export function setParserToken(value: string): void {
  parserSafeWrite(value);
}

export function clearParserToken(): void {
  parserSafeWrite(null);
}

export function hasParserToken(): boolean {
  return parserSafeRead() !== null;
}

const subscribeParser = (callback: () => void): (() => void) => {
  window.addEventListener(PARSER_SAME_TAB_EVENT, callback);
  window.addEventListener("storage", callback);
  return () => {
    window.removeEventListener(PARSER_SAME_TAB_EVENT, callback);
    window.removeEventListener("storage", callback);
  };
};

/**
 * Reactive read of the parser token. Re-renders when:
 *   - this tab calls `setParserToken` / `clearParserToken` (SAME_TAB_EVENT),
 *   - another tab dispatches a `storage` event (browser-native).
 */
export function useParserToken(): string | null {
  return useSyncExternalStore(subscribeParser, parserSafeRead, () => null);
}
