import { Database, Loader2, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-3 px-6 text-center">
      {children}
    </div>
  );
}

export function LoadingState() {
  return (
    <Centered>
      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      <p className="text-sm text-muted-foreground">Loading schema…</p>
    </Centered>
  );
}

export function EmptyState() {
  return (
    <Centered>
      <div className="rounded-full bg-muted p-4">
        <Database className="h-8 w-8 text-muted-foreground" />
      </div>
      <h2 className="text-base font-semibold">No schema yet</h2>
      <p className="max-w-sm text-sm text-muted-foreground">
        Nothing to display. Open a design session or import a database, then view it here.
      </p>
    </Centered>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Centered>
      <div className="rounded-full bg-muted p-4">
        <TriangleAlert className="h-8 w-8 text-muted-foreground" />
      </div>
      <h2 className="text-base font-semibold">Couldn't load the schema</h2>
      <p className="max-w-sm text-sm text-muted-foreground">{message}</p>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </Centered>
  );
}
