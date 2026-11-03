// Uplift significance, computed from the group counts the backend already returns.
// Two-proportion z-test (pooled) for the p-value, and a log-ratio interval for relative uplift.

export const CI_LEVEL = 0.95;
const Z = 1.959964; // two-sided 95%

// Abramowitz and Stegun 7.1.26, accurate to about 1e-7
function erf(x: number): number {
  const sign = x < 0 ? -1 : 1;
  const a = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * a);
  const poly = ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t;
  return sign * (1 - poly * Math.exp(-a * a));
}
const normalCdf = (z: number) => 0.5 * (1 + erf(z / Math.SQRT2));

export interface UpliftTest {
  p: number;                       // two-sided p-value, treated vs holdout conversion
  ci: [number, number] | null;     // 95% interval on relative uplift; null if either group has 0 orders
}

/** x = orders, n = resolved decisions. Group 1 is treated, group 2 is holdout. */
export function upliftTest(x1: number, n1: number, x2: number, n2: number): UpliftTest | null {
  if (n1 <= 0 || n2 <= 0) return null;
  const pooled = (x1 + x2) / (n1 + n2);
  if (pooled <= 0 || pooled >= 1) return null;
  const se = Math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2));
  const z = (x1 / n1 - x2 / n2) / se;
  const p = Math.min(1, 2 * (1 - normalCdf(Math.abs(z))));

  let ci: [number, number] | null = null;
  if (x1 > 0 && x2 > 0) {
    const p1 = x1 / n1, p2 = x2 / n2;
    const seLog = Math.sqrt((1 - p1) / (n1 * p1) + (1 - p2) / (n2 * p2));
    const lr = Math.log(p1 / p2);
    ci = [Math.exp(lr - Z * seLog) - 1, Math.exp(lr + Z * seLog) - 1];
  }
  return { p, ci };
}

export const formatP = (p: number) => (p < 0.001 ? "p < 0.001" : `p = ${p.toFixed(3)}`);
