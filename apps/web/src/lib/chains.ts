// Chain display and explorers. Anything not listed falls back to the raw id
// rather than a guessed name.

type Chain = { name: string; explorer: string | null; color: string };

const CHAINS: Record<number, Chain> = {
  1: { name: "Ethereum", explorer: "https://etherscan.io", color: "#8ba4f9" },
  10: { name: "Optimism", explorer: "https://optimistic.etherscan.io", color: "#ff5a5a" },
  130: { name: "Unichain", explorer: "https://uniscan.xyz", color: "#f76fc0" },
  137: { name: "Polygon", explorer: "https://polygonscan.com", color: "#a77bf3" },
  143: { name: "Monad", explorer: "https://monadscan.com", color: "#8b6cf6" },
  8453: { name: "Base", explorer: "https://basescan.org", color: "#4f7cf7" },
  42161: { name: "Arbitrum", explorer: "https://arbiscan.io", color: "#54b3e5" },
};

export const chainName = (id: number | null | undefined): string =>
  id == null ? "—" : (CHAINS[id]?.name ?? `Chain ${id}`);

export const chainColor = (id: number | null | undefined): string =>
  (id != null && CHAINS[id]?.color) || "#7c8899";

export const txUrl = (id: number | null | undefined, hash: string): string | null => {
  const explorer = id == null ? null : CHAINS[id]?.explorer;
  return explorer ? `${explorer}/tx/${hash}` : null;
};

export const addressUrl = (id: number | null | undefined, address: string): string | null => {
  const explorer = id == null ? null : CHAINS[id]?.explorer;
  return explorer ? `${explorer}/address/${address}` : null;
};
