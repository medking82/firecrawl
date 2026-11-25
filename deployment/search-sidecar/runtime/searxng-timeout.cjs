'use strict';
// Share a total budget across the search modules; other Axios calls are unchanged.
const deadline = require('./search-deadline.cjs');
const axios = require('axios');
const endpoint = process.env.SEARXNG_ENDPOINT;
if (endpoint) {
  const target = endpoint.replace(/\/$/, '') + '/search';
  const get = axios.get.bind(axios);
  axios.get = async function (url, options = {}) {
    if (url !== target) return get(url, options);
    const started = Date.now();
    const budget = deadline.requestBudget();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.min(4000,budget.remaining));
    try {
      const response = await get(url, {
        ...options,
        timeout: Math.min(options.timeout || 4000, 4000, budget.remaining),
        signal: AbortSignal.any([controller.signal, options.signal, budget.signal].filter(Boolean)),
        maxRedirects: 0,
        proxy: false,
      });
      const rows = Array.isArray(response.data?.results) ? response.data.results : [];
      console.info(JSON.stringify({event: 'searxng_search', status: rows.length ? 'ok' : 'empty',
        count: rows.length, engines: [...new Set(rows.flatMap(r => r.engines || []))].sort(),
        elapsedMs: Date.now() - started}));
      return response;
    } catch (error) {
      const shared = budget.signal?.aborted ? String(budget.signal.reason?.message || '') : '';
      const status = shared === 'search_deadline' ? 'deadline'
        : shared ? 'cancelled'
        : ['ECONNABORTED', 'ERR_CANCELED', 'ETIMEDOUT'].includes(error.code) ? 'timeout' : 'upstream_error';
      console.warn(JSON.stringify({event: 'searxng_search', status,
        httpStatus: error.response?.status || null, elapsedMs: Date.now() - started}));
      // Axios errors retain query parameters and response bodies: never forward those to logs.
      const safe = new Error('searxng_' + status);
      safe.code = status;
      throw safe;
    } finally { clearTimeout(timer); }
  };
}
