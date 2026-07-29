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
  clearAdminKey,
  getAdminKey,
  hasAdminKey,
  setAdminKey,
  useAdminKey,
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
