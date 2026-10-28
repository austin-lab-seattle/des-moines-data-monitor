export const DASHBOARD_REFRESH_MS = 5 * 60 * 1000;

// The caller performs its initial load. Poll only visible tabs, without
// overlapping callbacks, and refresh once when a hidden tab becomes visible.
export function startVisiblePolling(callback, delay, env = {}) {
  const doc = env.document ?? document;
  const later = env.setTimeout ?? setTimeout;
  const cancel = env.clearTimeout ?? clearTimeout;
  let timer;
  let stopped = false;
  let running = false;
  const schedule = () => {
    cancel(timer);
    if (!stopped && !doc.hidden) timer = later(run, delay);
  };
  const run = async () => {
    if (stopped || doc.hidden || running) return;
    running = true;
    try { await callback(); } finally { running = false; schedule(); }
  };
  const visibility = () => {
    cancel(timer);
    if (!doc.hidden) void run();
  };
  doc.addEventListener('visibilitychange', visibility);
  schedule();
  return () => {
    stopped = true;
    cancel(timer);
    doc.removeEventListener('visibilitychange', visibility);
  };
}
