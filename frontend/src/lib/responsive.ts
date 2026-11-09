// Screen-size helpers. Phones and narrow windows get the compact layout: the
// menu in a drawer, smaller padding, full-width drawers.
import { Grid } from "antd";

/** Narrower than antd's `md` breakpoint (768px). False until measured, so the
 *  first render is the desktop layout; antd measures before the first paint. */
export function useIsMobile(): boolean {
  return Grid.useBreakpoint().md === false;
}

/** A drawer's width: its usual size, or the whole screen on a phone. */
export function useDrawerWidth(desktop: number): number | string {
  return useIsMobile() ? "100%" : desktop;
}
