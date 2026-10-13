/**
 * localStorage that never throws: private windows, blocked site data, quota errors and server
 * rendering all degrade to "nothing stored". Use it for conveniences only (theme, saved views,
 * the last organization), never for anything that must persist.
 */
export const safeStorage = {
  get(key: string): string | null {
    try {
      return typeof window === "undefined" ? null : window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key: string, value: string): boolean {
    try {
      if (typeof window === "undefined") return false;
      window.localStorage.setItem(key, value);
      return true;
    } catch {
      return false;
    }
  },
  remove(key: string): void {
    try {
      if (typeof window !== "undefined") window.localStorage.removeItem(key);
    } catch {
      // ignore
    }
  },
  getJson<T>(key: string, guard: (value: unknown) => value is T): T | null {
    const raw = this.get(key);
    if (!raw) return null;
    try {
      const value: unknown = JSON.parse(raw);
      return guard(value) ? value : null;
    } catch {
      return null;
    }
  },
  setJson(key: string, value: unknown): boolean {
    try {
      return this.set(key, JSON.stringify(value));
    } catch {
      return false;
    }
  },
};
