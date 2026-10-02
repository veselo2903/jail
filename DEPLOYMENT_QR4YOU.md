# Развёртывание jail 1.15 под `/jail`

Текущая установка на `qr4you.ru`:

- Код: `/opt/jail4`.
- База: `/var/lib/jail3/jail.db`, путь задаёт `JAIL_DB_PATH` в `/etc/jail3.env`.
- Служба: `jail3.service`, Gunicorn на `127.0.0.1:8092`.
- Caddy в `/etc/caddy/Caddyfile` проксирует `/jail/*` на 8092 и передаёт `X-Forwarded-Prefix: /jail`.
- `JAIL_SECRET_KEY`, `JAIL_COOKIE_PATH=/jail`, `JAIL_COOKIE_SECURE=1` задаются в `/etc/jail3.env`. Значение секретного ключа в репозиторий не включается.
- Ежедневные снимки новой базы создаёт `jail-backup.timer` в `/srv/backups/jail3`. Снимки старой версии остались в `/srv/backups/jail`.

Код умеет читать `JAIL_DB_PATH`, а `ProxyFix` учитывает префикс `/jail`. На этой установке веб-загрузка ZIP-обновлений отключена: переменная `JAIL_ALLOW_WEB_UPDATES` не задана. Код развёртывается администратором сервера. Это важно, пока роли выбираются без пароля.

База версии 0.68 несовместима с 1.08. Старая служба `jail.service` остановлена, старый код остался в `/opt/jail`, база — в `/var/lib/jail/jail.db`. Снимок до переключения: `/root/jail-backups/jail-v068-before-jail3-20261001-124554.db`.

Для отката: заменить порт в маршруте Caddy `/jail/*` с `8092` на `8091`, проверить `caddy validate --config /etc/caddy/Caddyfile`, перезагрузить Caddy и запустить `jail.service`. Сохранённый снимок базы нужен, если старая база будет изменена после переключения.

Обновление 2026-10-02: развёрнут `jail4` 1.15, данные базы 1.08 сохранены и мигрированы. Служба `jail3.service` теперь запускает код из `/opt/jail4`. Снимок до обновления хранится в `/root/jail-backups/jail-before-jail4-*.db`.
