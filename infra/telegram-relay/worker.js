// Переходник Evactix → Telegram Bot API на Cloudflare Workers.
// Наш сервер не достаёт до api.telegram.org, а до Cloudflare — да.
// Адрес вида https://<имя>.<аккаунт>.workers.dev/<RELAY_KEY>/bot<токен>/<метод>.
// RELAY_KEY — секрет переменной окружения воркера: без него переходник отвечает 404 и не работает как открытый прокси.
export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const parts = url.pathname.split("/");               // ["", key, "bot<токен>", метод]
    if (!env.RELAY_KEY || parts[1] !== env.RELAY_KEY || !(parts[2] || "").startsWith("bot")) {
      return new Response("not found", { status: 404 });
    }
    const target = "https://api.telegram.org/" + parts.slice(2).join("/") + url.search;
    const init = { method: req.method, headers: { "content-type": req.headers.get("content-type") || "application/json" } };
    if (req.method !== "GET" && req.method !== "HEAD") init.body = await req.arrayBuffer();
    const r = await fetch(target, init);
    return new Response(r.body, { status: r.status, headers: { "content-type": r.headers.get("content-type") || "application/json" } });
  },
};
