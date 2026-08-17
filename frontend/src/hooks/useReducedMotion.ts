import { useEffect, useState } from "react";

/**
 * Whether the viewer has asked the OS for reduced motion.
 *
 * Charts animate on mount, which is pleasant by default and actively unwanted by
 * anyone who has set that preference -- vestibular disorders are the reason the
 * media query exists, and a dashboard that redraws every 5s is a poor place to
 * ignore it.
 *
 * It also makes the charts deterministic to capture. Recharts animates a line by
 * tweening `stroke-dasharray` from `0px Npx` (invisible) to `Npx 0px`, so a
 * screenshot taken before the tween runs shows correct path geometry and no
 * visible line -- which is exactly what happened while producing the README
 * screenshots, and looked for all the world like a broken chart.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => matches());

  useEffect(() => {
    // jsdom has no matchMedia; guard rather than weaken the call site.
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(QUERY);
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return reduced;
}

const QUERY = "(prefers-reduced-motion: reduce)";

function matches(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia(QUERY).matches;
}
