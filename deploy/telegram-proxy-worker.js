// Cloudflare Worker: TLS proxy for Telegram Bot API (Session 5.53).
//
// Why: the barber-bot VPS (RU hoster) suffers recurring connection resets
// to api.telegram.org (13+ over 2 days, handoff 5.51). Cloudflare's edge
// reaches api.telegram.org reliably; the VPS reaches Cloudflare reliably.
//
// Security: the worker is NOT an open proxy. Every request must start with
// /<WORKER_SECRET>/ — anything else gets 404. The secret travels in the URL
// prefix produced by aiogram's TelegramAPIServer.from_base(url_with_secret).
// This stops third parties from abusing the worker as a free Telegram relay.
//
// Cost: free tier = 100k req/day. Long polling getUpdates (timeout=30) is
// ~2 req/min ≈ 3k/day — two orders of magnitude below the limit.
//
// Deploy (from this directory, see deploy/README-worker.md):
//   npx wrangler login
//   npx wrangler secret put WORKER_SECRET   # paste a random string
//   npx wrangler deploy
// Then on the VPS .env: TELEGRAM_API_BASE_URL=https://<name>.<subdomain>.workers.dev/<WORKER_SECRET>

const UPSTREAM = "https://api.telegram.org";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const secret = env.WORKER_SECRET;

    // Only /<secret>/... passes; everything else is 404 (not 401 — no hint
    // to scanners that a secret exists at all).
    if (!secret || url.pathname !== `/${secret}` && !url.pathname.startsWith(`/${secret}/`)) {
      return new Response("Not Found", { status: 404 });
    }

    // Strip the secret prefix, rebuild the upstream URL (query preserved).
    const upstreamPath = url.pathname.slice(secret.length + 1);
    const upstreamUrl = `${UPSTREAM}${upstreamPath}${url.search}`;

    // Rebuild the request: same method/headers/body, new URL.
    const upstreamRequest = new Request(upstreamUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body,
      // Telegram accepts both GET and POST; keep the redirect mode strict.
      redirect: "manual",
    });

    return fetch(upstreamRequest);
  },
};
