// Tiny browser download helpers shared by the Code panel (text exports) and the ERD image export.
// Purely presentational plumbing — no schema logic.

/** Trigger a download of in-memory text as a file. */
export function downloadText(filename: string, content: string, mime = "text/plain"): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  downloadUrl(filename, url);
  URL.revokeObjectURL(url);
}

/** Trigger a download of an existing URL / data-URL (used for ERD image blobs). */
export function downloadUrl(filename: string, url: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
