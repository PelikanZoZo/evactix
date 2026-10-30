#!/usr/bin/env node
// Точка входа Evactix для хостинга (NGINX Unit запускает именно app.js).
// Пока это заглушка: отдаёт статические файлы из папки public.
const http = require("http");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "public");
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".svg": "image/svg+xml",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

http.createServer((req, res) => {
  let p;
  try { p = decodeURIComponent(new URL(req.url, "http://x").pathname); } catch { p = "/"; }
  if (p.endsWith("/")) p += "index.html";
  let file = path.normalize(path.join(ROOT, p));
  if (!file.startsWith(ROOT)) file = path.join(ROOT, "index.html");
  fs.readFile(file, (err, data) => {
    if (err) {
      // любой неизвестный адрес (в том числе /login, пока его нет) — на заглушку
      return fs.readFile(path.join(ROOT, "index.html"), (e2, html) => {
        res.writeHead(e2 ? 500 : 200, { "Content-Type": TYPES[".html"], "Cache-Control": "no-cache" });
        res.end(e2 ? "Evactix" : html);
      });
    }
    res.writeHead(200, {
      "Content-Type": TYPES[path.extname(file)] || "application/octet-stream",
      "Cache-Control": file.endsWith(".html") ? "no-cache" : "public, max-age=3600",
      "X-Content-Type-Options": "nosniff",
    });
    res.end(data);
  });
}).listen();

// Если app.js обновили при деплое — процесс завершается, Unit поднимет новый.
try {
  fs.watch(__filename, () => process.exit(0));
} catch {}
