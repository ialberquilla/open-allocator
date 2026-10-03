import { Shell } from "@/components/Shell";
import { Approve } from "@/pages/Approve";
import { Home } from "@/pages/Home";

// Two routes, matched by hand: `/` and `/approve/<hash>`.
const APPROVE = /^\/approve\/([0-9a-f]{64})\/?$/;

export function App() {
  const match = APPROVE.exec(window.location.pathname);
  return <Shell>{match?.[1] ? <Approve hash={match[1]} /> : <Home />}</Shell>;
}
