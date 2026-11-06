/** The per-client memory that this network needed a relay, over a fake Storage. */
import { describe, expect, it } from "vitest";

import { NEEDS_RELAY_KEY, readNeedsRelay, rememberNeedsRelay } from "./relay_memory";

class FakeStorage implements Storage {
  private readonly items = new Map<string, string>();

  get length(): number {
    return this.items.size;
  }

  clear(): void {
    this.items.clear();
  }

  getItem(key: string): string | null {
    return this.items.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.items.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.items.delete(key);
  }

  setItem(key: string, value: string): void {
    this.items.set(key, value);
  }
}

class ThrowingStorage extends FakeStorage {
  override getItem(): string | null {
    throw new Error("blocked");
  }

  override setItem(): void {
    throw new Error("quota");
  }
}

describe("relay memory", () => {
  it("reads false until a relay was needed, then true across reads", () => {
    const storage = new FakeStorage();
    expect(readNeedsRelay(storage)).toBe(false);
    rememberNeedsRelay(storage);
    expect(readNeedsRelay(storage)).toBe(true);
    expect(storage.getItem(NEEDS_RELAY_KEY)).toBe("1");
  });

  it("reads false with no storage at all", () => {
    expect(readNeedsRelay(null)).toBe(false);
    rememberNeedsRelay(null);
  });

  it("tolerates a store that throws", () => {
    const storage = new ThrowingStorage();
    rememberNeedsRelay(storage);
    expect(readNeedsRelay(storage)).toBe(false);
  });
});
