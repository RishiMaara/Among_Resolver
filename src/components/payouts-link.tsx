/**
 * The Payouts nav link: one component, because it sits in every header and
 * five hand-copied links are five places to miss when it changes.
 */

import { Link } from "@tanstack/react-router";
import { Landmark } from "lucide-react";

export function PayoutsLink({
  className = "flex items-center gap-1.5 whitespace-nowrap text-[14px] text-muted-foreground hover:text-foreground",
}: {
  className?: string;
}) {
  return (
    <Link to="/payouts" className={className}>
      <Landmark className="size-4" />
      <span className="hidden sm:inline">Payouts</span>
    </Link>
  );
}
