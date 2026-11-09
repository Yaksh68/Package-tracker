import "@testing-library/jest-dom/vitest";

// antd reads these; jsdom doesn't provide them.
//
// Screen-size queries answer for a 1280px-wide desktop, so tests see the
// desktop layout. A test can set `window.__screenWidth` (e.g. 390, a phone)
// before rendering, and reset it after.
declare global {
  interface Window {
    __screenWidth?: number;
  }
}
window.matchMedia = (query: string) => {
  const width = window.__screenWidth ?? 1280;
  const min = /min-width:\s*([\d.]+)px/.exec(query);
  const max = /max-width:\s*([\d.]+)px/.exec(query);
  const matches = (!min || width >= Number(min[1])) && (!max || width <= Number(max[1]));
  return {
    matches,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  } as MediaQueryList;
};
if (!("ResizeObserver" in window)) {
  (window as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
