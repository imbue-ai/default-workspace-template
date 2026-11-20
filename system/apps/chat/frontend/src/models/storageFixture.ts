/** An in-memory Storage for tests that can be told to refuse writes past a character budget, like a full quota. */
export function memoryStorage(charBudget = Infinity): Storage {
  const items = new Map<string, string>();
  const used = (): number => [...items.values()].reduce((sum, value) => sum + value.length, 0);
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: (key) => items.get(key) ?? null,
    key: (index) => [...items.keys()][index] ?? null,
    removeItem: (key) => void items.delete(key),
    setItem: (key, value) => {
      const previous = items.get(key)?.length ?? 0;
      if (used() - previous + value.length > charBudget) {
        throw new DOMException("quota", "QuotaExceededError");
      }
      items.set(key, value);
    },
  };
}
