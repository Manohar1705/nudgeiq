import { api } from "../api/client";
import type { UpliftDimension, UpliftSlice } from "../api/types";
import { normalizeSlices } from "./normalize";

export type DimSlice = UpliftSlice & { dimension: UpliftDimension };
export const DIMENSIONS: UpliftDimension[] = ["offer", "timing", "channel"];

/** Every offer, timing and channel slice in one list. A dimension that fails is reported, not fatal. */
export async function loadSlices() {
  const results = await Promise.allSettled(DIMENSIONS.map((d) => api.getUpliftBy(d)));
  const rows: DimSlice[] = [];
  const failed: UpliftDimension[] = [];
  results.forEach((r, i) => {
    if (r.status === "rejected") { failed.push(DIMENSIONS[i]); return; }
    rows.push(...normalizeSlices(r.value).map((s) => ({ ...s, dimension: DIMENSIONS[i] })));
  });
  return { rows, failed };
}
