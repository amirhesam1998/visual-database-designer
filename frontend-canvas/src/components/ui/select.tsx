import * as React from "react";
import { cn } from "@/lib/utils";

// A styled native <select> — keeps the editor dependency-free and accessible (spec §6: minimal,
// no-jump editing). Used for the semantic-type picker and the relation-type picker.
export const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, children, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        "h-8 w-full rounded-md border border-input bg-card px-2 text-xs text-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  ),
);
Select.displayName = "Select";
