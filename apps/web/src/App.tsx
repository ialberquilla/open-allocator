import { Shell } from "@/components/Shell";
import { Activity } from "@/pages/Activity";
import { Approve } from "@/pages/Approve";
import { Book } from "@/pages/Book";
import { Overview } from "@/pages/Overview";
import { Performance } from "@/pages/Performance";

// Routes matched by hand; the server serves this page for each of them.
const APPROVE = /^\/approve\/([0-9a-f]{64})\/?$/;

function Page({ pathname }: { pathname: string }) {
  const approve = APPROVE.exec(pathname);
  if (approve?.[1]) return <Approve hash={approve[1]} />;
  switch (pathname.replace(/\/$/, "") || "/") {
    case "/book":
      return <Book />;
    case "/performance":
      return <Performance />;
    case "/activity":
      return <Activity />;
    default:
      return <Overview />;
  }
}

export function App() {
  const pathname = window.location.pathname;
  return (
    <Shell pathname={pathname}>
      <Page pathname={pathname} />
    </Shell>
  );
}
