/**
 * Phase 11 T11-2 — auth helper RED tests.
 *
 * `src/lib/auth.ts` exposes:
 *   - ADMIN_KEY_STORAGE_KEY: 'ekrs.admin_key'
 *   - getAdminKey():  string | null   (sync localStorage read)
 *   - setAdminKey(v: string): void
 *   - clearAdminKey(): void
 *   - hasAdminKey(): boolean
 *   - useAdminKey(): string | null    (React hook; reactive across tabs)
 *
 * Behaviour contract:
 *   - Pure helpers tolerate missing localStorage (private browsing → null)
 *   - The hook re-renders when the localStorage value changes in THIS tab
 *     or in another tab (storage event)
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  ADMIN_KEY_STORAGE_KEY,
  PARSER_TOKEN_STORAGE_KEY,
  clearAdminKey,
  clearParserToken,
  getAdminKey,
  getParserToken,
  hasAdminKey,
  hasParserToken,
  setAdminKey,
  setParserToken,
  useAdminKey,
  useParserToken,
} from "./auth";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("auth helpers", () => {
  it("returns null when no key is stored", () => {
    expect(getAdminKey()).toBeNull();
    expect(hasAdminKey()).toBe(false);
  });

  it("round-trips a value through set + get", () => {
    setAdminKey("secret-admin-key-xyz");
    expect(getAdminKey()).toBe("secret-admin-key-xyz");
    expect(hasAdminKey()).toBe(true);
  });

  it("clears the value", () => {
    setAdminKey("anything");
    clearAdminKey();
    expect(getAdminKey()).toBeNull();
    expect(hasAdminKey()).toBe(false);
  });

  it("uses the documented storage key", () => {
    expect(ADMIN_KEY_STORAGE_KEY).toBe("ekrs.admin_key");
  });

  it("overwrites previous value on set", () => {
    setAdminKey("first");
    setAdminKey("second");
    expect(getAdminKey()).toBe("second");
  });
});

describe("useAdminKey hook", () => {
  it("returns null when no key is set", () => {
    const { result } = renderHook(() => useAdminKey());
    expect(result.current).toBeNull();
  });

  it("reflects a value set before mount", () => {
    setAdminKey("preloaded");
    const { result } = renderHook(() => useAdminKey());
    expect(result.current).toBe("preloaded");
  });

  it("re-renders when value changes via setAdminKey in the same tab", () => {
    const { result } = renderHook(() => useAdminKey());
    expect(result.current).toBeNull();
    act(() => {
      setAdminKey("after-mount");
    });
    expect(result.current).toBe("after-mount");
    act(() => {
      clearAdminKey();
    });
    expect(result.current).toBeNull();
  });

  it("re-renders when another tab dispatches a storage event", () => {
    const { result } = renderHook(() => useAdminKey());
    expect(result.current).toBeNull();
    act(() => {
      // Simulate another tab writing to localStorage. The localStorage
      // setter does NOT fire a 'storage' event in the SAME tab, so we
      // dispatch it manually — this matches the cross-tab contract.
      localStorage.setItem(ADMIN_KEY_STORAGE_KEY, "from-other-tab");
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: ADMIN_KEY_STORAGE_KEY,
          newValue: "from-other-tab",
          storageArea: localStorage,
        }),
      );
    });
    expect(result.current).toBe("from-other-tab");
  });

  it("ignores storage events for unrelated keys", () => {
    setAdminKey("initial");
    const { result } = renderHook(() => useAdminKey());
    expect(result.current).toBe("initial");
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "unrelated",
          newValue: "x",
          storageArea: localStorage,
        }),
      );
    });
    expect(result.current).toBe("initial");
  });
});

/**
 * Phase 13c post-closure patch — parser-token auth helpers RED tests.
 *
 * `src/lib/auth.ts` mirrors the admin-key pattern for the X-Parser-Token
 * header used by `/v1/constraints`, `/v1/ingestion/notify`,
 * `/v1/ingestion/status/{hash}`, `/v1/blocks/{id}`. Operators paste their
 * local PARSER_TOKEN into a Settings input (ConstraintsView); the value
 * lives only in localStorage and is attached by the typed client only
 * for the parser-gated paths.
 *
 * Storage key is `ekrs.parser_token` (deliberately distinct from
 * `ekrs.admin_key` so admins and parser-side operators don't collide).
 */
describe("parser-token helpers", () => {
  it("returns null when no token is stored", () => {
    expect(getParserToken()).toBeNull();
    expect(hasParserToken()).toBe(false);
  });

  it("round-trips a value through set + get", () => {
    setParserToken("dev-local-token-32chars-aaaaaaaaaa");
    expect(getParserToken()).toBe("dev-local-token-32chars-aaaaaaaaaa");
    expect(hasParserToken()).toBe(true);
  });

  it("clears the value", () => {
    setParserToken("anything");
    clearParserToken();
    expect(getParserToken()).toBeNull();
    expect(hasParserToken()).toBe(false);
  });

  it("uses the documented storage key", () => {
    expect(PARSER_TOKEN_STORAGE_KEY).toBe("ekrs.parser_token");
  });

  it("does not collide with admin-key storage key", () => {
    expect(PARSER_TOKEN_STORAGE_KEY).not.toBe(ADMIN_KEY_STORAGE_KEY);
  });

  it("overwrites previous value on set", () => {
    setParserToken("first");
    setParserToken("second");
    expect(getParserToken()).toBe("second");
  });
});

describe("useParserToken hook", () => {
  it("returns null when no token is set", () => {
    const { result } = renderHook(() => useParserToken());
    expect(result.current).toBeNull();
  });

  it("reflects a value set before mount", () => {
    setParserToken("preloaded");
    const { result } = renderHook(() => useParserToken());
    expect(result.current).toBe("preloaded");
  });

  it("re-renders when value changes via setParserToken in the same tab", () => {
    const { result } = renderHook(() => useParserToken());
    expect(result.current).toBeNull();
    act(() => {
      setParserToken("after-mount");
    });
    expect(result.current).toBe("after-mount");
    act(() => {
      clearParserToken();
    });
    expect(result.current).toBeNull();
  });

  it("re-renders when another tab dispatches a storage event", () => {
    const { result } = renderHook(() => useParserToken());
    expect(result.current).toBeNull();
    act(() => {
      localStorage.setItem(PARSER_TOKEN_STORAGE_KEY, "from-other-tab");
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: PARSER_TOKEN_STORAGE_KEY,
          newValue: "from-other-tab",
          storageArea: localStorage,
        }),
      );
    });
    expect(result.current).toBe("from-other-tab");
  });

  it("ignores storage events for unrelated keys", () => {
    setParserToken("initial");
    const { result } = renderHook(() => useParserToken());
    expect(result.current).toBe("initial");
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "unrelated",
          newValue: "x",
          storageArea: localStorage,
        }),
      );
    });
    expect(result.current).toBe("initial");
  });
});
