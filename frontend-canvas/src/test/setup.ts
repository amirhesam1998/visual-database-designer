import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => cleanup());

// React Flow measures node DOM with ResizeObserver + offset sizes to decide a node is "initialized"
// (only then does it render the edges between them). jsdom reports 0 for all of these, so we stub
// them with sensible non-zero sizes and fire the observer callback once on observe().
class ResizeObserverMock {
  constructor(private cb: ResizeObserverCallback) {}
  observe(target: Element) {
    this.cb([{ target, contentRect: { width: 252, height: 120 } }] as unknown as ResizeObserverEntry[], this);
  }
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverMock as unknown as typeof ResizeObserver;

for (const dim of ["offsetWidth", "offsetHeight"] as const) {
  Object.defineProperty(HTMLElement.prototype, dim, {
    configurable: true,
    get() {
      return dim === "offsetWidth" ? 252 : 120;
    },
  });
}

Object.defineProperty(HTMLElement.prototype, "getBoundingClientRect", {
  configurable: true,
  value() {
    return { x: 0, y: 0, top: 0, left: 0, right: 252, bottom: 120, width: 252, height: 120, toJSON: () => ({}) };
  },
});

if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

// jsdom lacks DOMMatrix / canvas bits React Flow touches when rendered full.
if (!("DOMMatrixReadOnly" in globalThis)) {
  // @ts-expect-error - minimal stub
  globalThis.DOMMatrixReadOnly = class {};
}
