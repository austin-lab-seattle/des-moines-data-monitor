const transient = new Set([500, 502, 503, 504]);
const retryableRead = (path, method) => method === 'GET' && /\/(observations|timeseries|summary)$/.test(path.split('?')[0]);

export async function fetchApiRead(path, options = {}, env = {}) {
  const fetcher = env.fetch ?? fetch;
  const delay = env.delay ?? (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const attempts = retryableRead(path, (options.method || 'GET').toUpperCase()) ? 2 : 1;
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (options.signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), env.timeoutMs ?? 30000);
    const abort = () => controller.abort();
    options.signal?.addEventListener('abort', abort, { once: true });
    let retry;
    try {
      const response = await fetcher(path, { ...options, signal: controller.signal });
      retry = attempt + 1 < attempts && transient.has(response.status);
      if (!retry) return response;
      await response.body?.cancel().catch(() => {});
    } catch (error) {
      if (options.signal?.aborted || attempt + 1 >= attempts) throw error;
      retry = true;
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener('abort', abort);
    }
    if (retry) await delay(400);
  }
}
