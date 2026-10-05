// SVG donut, no chart dependency. Segment values are in basis points.
export type DonutSegment = {
  label: string;
  weightBps: number;
  color: string;
  sub?: string;
};

const bpsPct = (bps: number) => `${(bps / 100).toFixed(1)}%`;

export function DonutChart({
  segments,
  centerLabel,
  centerSub,
  weightSuffix,
  size = 120,
}: {
  segments: DonutSegment[];
  centerLabel?: string;
  centerSub?: string;
  weightSuffix?: string;
  size?: number;
}) {
  const SIZE = size;
  const STROKE = Math.round(size * 0.15);
  const R = (SIZE - STROKE) / 2;
  const C = 2 * Math.PI * R;
  const centerFontSize = Math.round(size * 0.133);
  const subFontSize = Math.round(size * 0.067);

  const total = segments.reduce((sum, seg) => sum + seg.weightBps, 0) || 1;
  const fracs = segments.map((seg) => seg.weightBps / total);
  const offsets = fracs.map((_, i) => fracs.slice(0, i).reduce((sum, f) => sum + f, 0));

  return (
    <div className="flex flex-col items-center gap-4 sm:flex-row sm:gap-5">
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        className="max-w-full shrink-0"
        style={{ height: SIZE, width: SIZE }}
        role="img"
        aria-label="allocation donut"
      >
        {/* Rotate only the ring so the first segment starts at 12 o'clock; the
            centre label stays upright. */}
        <g transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}>
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={R}
            fill="none"
            stroke="hsl(var(--muted) / 0.4)"
            strokeWidth={STROKE}
          />
          {segments.map((seg, i) => {
            const dash = (fracs[i] ?? 0) * C;
            return (
              <circle
                key={seg.label}
                cx={SIZE / 2}
                cy={SIZE / 2}
                r={R}
                fill="none"
                stroke={seg.color}
                strokeWidth={STROKE}
                strokeDasharray={`${dash} ${C - dash}`}
                strokeDashoffset={-(offsets[i] ?? 0) * C}
              >
                <title>{`${seg.label}: ${bpsPct(seg.weightBps)}`}</title>
              </circle>
            );
          })}
        </g>
        {centerLabel && (
          <text
            x={SIZE / 2}
            y={SIZE / 2 - (centerSub ? size * 0.05 : 0)}
            textAnchor="middle"
            dominantBaseline="central"
            className="fill-foreground"
            fontSize={centerFontSize}
            fontWeight="700"
          >
            {centerLabel}
          </text>
        )}
        {centerSub && (
          <text
            x={SIZE / 2}
            y={SIZE / 2 + size * 0.083}
            textAnchor="middle"
            dominantBaseline="central"
            className="fill-muted-foreground"
            fontSize={subFontSize}
          >
            {centerSub}
          </text>
        )}
      </svg>
      {/* min-w-0 on the row AND on the label: a flex item defaults to
          min-width:auto, so without it `truncate` never shrinks and the legend
          pushes the whole card past the viewport on a phone. */}
      <div className="w-full min-w-0 flex-1 space-y-1.5">
        {[...segments]
          .sort((a, b) => b.weightBps - a.weightBps)
          .map((seg) => (
            <div key={seg.label} className="flex min-w-0 items-center gap-2 text-sm">
              <span
                className="inline-block size-2.5 shrink-0 rounded-full"
                style={{ backgroundColor: seg.color }}
              />
              <span className="min-w-0 truncate text-foreground">{seg.label}</span>
              {seg.sub && (
                <span className="shrink-0 text-xs text-muted-foreground">{seg.sub}</span>
              )}
              <span className="ml-auto shrink-0 tabular-nums text-muted-foreground">
                {bpsPct(seg.weightBps)}
                {weightSuffix ? ` ${weightSuffix}` : ""}
              </span>
            </div>
          ))}
      </div>
    </div>
  );
}
