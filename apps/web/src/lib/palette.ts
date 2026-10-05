// Categorical series colours. Ordered so that adjacent slices in a donut stay
// distinguishable, and picked to sit on the dark card without vibrating.
export const SERIES = [
  "#5eead4", // teal — the primary
  "#a78bfa", // violet
  "#c6f24e", // neon
  "#60a5fa", // blue
  "#f472b6", // pink
  "#fbbf24", // amber
  "#34d399", // green
  "#fb7185", // rose
];

export const seriesColor = (index: number): string => SERIES[index % SERIES.length] ?? "#5eead4";

export const OK = "#c6f24e";
export const WARN = "#fbbf24";
export const BAD = "#f87171";
export const MUTED = "#6b7280";
