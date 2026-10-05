import { chainName } from "@/lib/chains";

// Protocol and chain marks.
// Anything without a mark gets its initial on a tinted disc, never a wrong logo.

const marks = import.meta.glob<string>("../assets/{protocols,chains}/*", {
  eager: true,
  query: "?url",
  import: "default",
});

const mark = (path: string): string | null => marks[`../assets/${path}`] ?? null;

const PROTOCOLS: Record<string, string> = {
  aave: "protocols/aave.webp",
  avantis: "protocols/avantis.webp",
  compound: "protocols/compound.webp",
  euler: "protocols/euler.svg",
  fluid: "protocols/fluid.webp",
  moonwell: "protocols/moonwell.webp",
  morpho: "protocols/morpho.webp",
  neverland: "protocols/neverland.webp",
  pendle: "protocols/pendle.webp",
  // The API says "Tokemak"; the mark is Auto Finance, the name it rebranded to.
  tokemak: "protocols/autofinance.webp",
};

const CHAINS: Record<number, string> = {
  130: "chains/unichain.jpeg",
  143: "chains/monad.webp",
  8453: "chains/base.svg",
  42161: "chains/arbitrum.svg",
};

/** "Aave V3" and "Morpho Blue" wear the same mark as their family. */
const protocolSrc = (protocol: string): string | null => {
  const name = protocol.toLowerCase();
  const key = Object.keys(PROTOCOLS).find((k) => name === k || name.startsWith(`${k} `));
  const path = key ? PROTOCOLS[key] : undefined;
  return path ? mark(path) : null;
};

function Mark({ src, label, size }: { src: string | null; label: string; size: number }) {
  if (!src) {
    return (
      <span
        aria-label={label}
        className="flex shrink-0 items-center justify-center rounded-full bg-primary/20 font-bold text-primary"
        style={{ width: size, height: size, fontSize: size * 0.5 }}
      >
        {label.charAt(0).toUpperCase()}
      </span>
    );
  }
  return (
    <img
      src={src}
      alt={label}
      width={size}
      height={size}
      className="shrink-0 rounded-full object-contain"
      style={{ width: size, height: size }}
    />
  );
}

export function ProtocolIcon({ protocol, size = 20 }: { protocol: string; size?: number }) {
  return <Mark src={protocolSrc(protocol)} label={protocol} size={size} />;
}

export function ChainIcon({ chainId, size = 16 }: { chainId: number; size?: number }) {
  const path = CHAINS[chainId];
  return <Mark src={path ? mark(path) : null} label={chainName(chainId)} size={size} />;
}

/** A position's mark: its protocol, with the chain it lives on as a badge. */
export function PositionIcon({ protocol, chainId, size = 28 }: { protocol: string; chainId: number; size?: number }) {
  const badge = Math.round(size * 0.46);
  return (
    <span className="relative inline-flex shrink-0" style={{ width: size, height: size }}>
      <ProtocolIcon protocol={protocol} size={size} />
      <span className="absolute -right-1 -bottom-1 rounded-full ring-2 ring-card" style={{ width: badge, height: badge }}>
        <ChainIcon chainId={chainId} size={badge} />
      </span>
    </span>
  );
}
