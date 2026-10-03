// Chain display and explorers. Anything not listed falls back to the raw id
// rather than a guessed name.

type Chain = { name: string; explorer: string | null };

const CHAINS: Record<number, Chain> = {
  1: { name: "Ethereum", explorer: "https://etherscan.io" },
  10: { name: "Optimism", explorer: "https://optimistic.etherscan.io" },
  130: { name: "Unichain", explorer: "https://uniscan.xyz" },
  137: { name: "Polygon", explorer: "https://polygonscan.com" },
  143: { name: "Monad", explorer: "https://monadscan.com" },
  8453: { name: "Base", explorer: "https://basescan.org" },
  42161: { name: "Arbitrum", explorer: "https://arbiscan.io" },
};

export const chainName = (id: number | null | undefined): string =>
  id == null ? "—" : (CHAINS[id]?.name ?? `Chain ${id}`);

export const txUrl = (id: number | null | undefined, hash: string): string | null => {
  const explorer = id == null ? null : CHAINS[id]?.explorer;
  return explorer ? `${explorer}/tx/${hash}` : null;
};

export const addressUrl = (id: number | null | undefined, address: string): string | null => {
  const explorer = id == null ? null : CHAINS[id]?.explorer;
  return explorer ? `${explorer}/address/${address}` : null;
};
