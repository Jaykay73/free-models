/** No dependencies, Node 18+ and browsers. No inference keys are accepted. */
const cache = new Map();

function validate(data) {
  if (!data || data.schema_version !== 1 || !data.aliases || typeof data.aliases !== 'object' ||
      Array.isArray(data.aliases) || typeof data.valid_until !== 'string' || !Number.isFinite(Date.parse(data.valid_until))) {
    throw new Error('Invalid status document');
  }
  for (const entry of Object.values(data.aliases)) {
    if (!entry || !Array.isArray(entry.fallbacks) || typeof entry.stale !== 'boolean' ||
        (entry.primary !== null && typeof entry.primary !== 'string') ||
        entry.fallbacks.some(m => typeof m !== 'string' || !m)) throw new Error('Invalid alias');
  }
  return data;
}

export async function resolve(alias, {status_url, ttl_s = 300, defaults = {}} = {}) {
  if (!status_url || !Number.isFinite(ttl_s) || ttl_s < 300) throw new Error('status_url required; ttl_s must be at least 300');
  let entry = cache.get(status_url);
  const now = Date.now();
  if (!entry || now - entry.checked >= ttl_s * 1000) {
    // Share pending fetches between concurrent callers.
    if (!entry?.pending) {
      const old = entry;
      const pending = (async () => {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 3000);
        try {
          const response = await fetch(status_url, {signal: controller.signal});
          if (!response.ok) throw new Error('Status fetch failed');
          const text = await response.text();
          if (text.length > 1000000) throw new Error('Status too large');
          return {data: validate(JSON.parse(text)), checked: Date.now(), failed: false};
        } catch {
          return {data: old?.data, checked: Date.now(), failed: true};
        } finally { clearTimeout(timer); }
      })();
      entry = {...old, pending, checked: old?.checked ?? -Infinity};
      cache.set(status_url, entry);
    }
    entry = await entry.pending;
    cache.set(status_url, entry);
  } else if (entry.pending) {
    entry = await entry.pending;
  }
  const route = entry.data?.aliases[alias];
  const names = route?.primary ? [route.primary, ...route.fallbacks] : [];
  if (entry.failed || !names.length || route?.stale || (entry.data && now > Date.parse(entry.data.valid_until))) {
    console.warn('nimstatus: stale status or caller defaults in use');
  }
  return [...new Set(names.length ? names : (defaults[alias] ?? []))];
}

export async function call_with_fallback(alias, fn, {retryable, max_attempts = 5, ...options} = {}) {
  if (typeof retryable !== 'function' || !Number.isInteger(max_attempts) || max_attempts < 1) throw new Error('retryable and positive max_attempts required');
  const models = (await resolve(alias, options)).slice(0, max_attempts);
  if (!models.length) throw new Error('No models available for alias');
  for (let i = 0; i < models.length; i++) {
    try { return await fn(models[i]); }
    catch (error) { if (!retryable(error) || i === models.length - 1) throw error; }
  }
}
