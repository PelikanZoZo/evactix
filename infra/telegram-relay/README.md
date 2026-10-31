# Переходник в Telegram

Сервер VPS-1 не достаёт до `api.telegram.org`, а до Cloudflare — достаёт. Ана и Дирижёр ходят в Telegram через этот воркер.

1. Cloudflare → Workers & Pages → Create → Worker → имя `evactix-tg` → Deploy.
2. Edit code → вставить `worker.js` целиком → Deploy.
3. Settings → Variables and Secrets → Add → тип Secret, имя `RELAY_KEY`, значение — длинная случайная строка.
4. В GitHub-секрет `TELEGRAM_API_BASE` положить `https://evactix-tg.<аккаунт>.workers.dev/<RELAY_KEY>` (без косой черты в конце).

Без правильного ключа в адресе воркер отвечает 404 — открытым прокси он не работает.
