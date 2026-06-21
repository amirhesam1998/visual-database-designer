import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Minimal CSS tooltip (shadcn aesthetic without the Radix dependency). Wrap a trigger and pass
 * `label`; it shows on hover/focus. Good enough for the read-only canvas toolbar + field hints.
 */
export function Tooltip({
  label,
  children,
  side = "bottom",
  className,
}: {
  label: React.ReactNode;
  children: React.ReactNode;
  side?: "top" | "bottom";
  className?: string;
}) {
  return (
    <span className="group/tt relative inline-flex">
      {children}
      <span
        role="tooltip"
        className={cn(
          "pointer-events-none absolute left-1/2 z-50 -translate-x-1/2 whitespace-nowrap rounded-md",
          "border border-border bg-card px-2 py-1 text-2xs text-card-foreground shadow-md",
          "opacity-0 transition-opacity duration-150 group-hover/tt:opacity-100",
          side === "bottom" ? "top-full mt-1.5" : "bottom-full mb-1.5",
          className,
        )}
      >
        {label}
      </span>
    </span>
  );
}
