/**
 * Runs once when the Next.js server starts. On free hosting both services sleep when idle;
 * nudging the API here wakes it in parallel with this server instead of after the first
 * user request reaches it (one cold start instead of two back to back).
 */
export function register(): void {
  if (process.env.NEXT_RUNTIME !== "nodejs" || !process.env.SF_API_URL) return;
  void fetch(`${process.env.SF_API_URL}/healthz`, { cache: "no-store" }).catch(() => undefined);
}
