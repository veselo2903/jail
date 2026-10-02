# Развёртывание jail 1.27

## Рабочая установка

- Исходники и Git: `/root/jail`.
- Приложение и виртуальное окружение: `/opt/jail`, `/opt/jail/.venv`.
- Данные: `/var/lib/jail/jail.db`.
- Секреты и настройки: `/etc/jail.env` (в репозиторий не включается).
- Служба: `jail.service`, Gunicorn на `127.0.0.1:8092`.
- Caddy: `/etc/caddy/Caddyfile`, маршрут `/jail/*`, заголовок `X-Forwarded-Prefix: /jail`.
- Ежедневный снимок SQLite: `jail-backup.timer`, каталог `/srv/backups/jail`.

`JAIL_DB_PATH=/var/lib/jail/jail.db`, `JAIL_COOKIE_PATH=/jail`, `JAIL_COOKIE_SECURE=1` и `JAIL_SECRET_KEY` задаются вне кода. `JAIL_SKIP_MIGRATIONS=1` задан в службе: перед запуском workers `ExecStartPre` выполняет `python migrate.py`. При ошибке миграции служба не запускается. ZIP-обновления через сайт выключены.

## Обновление

1. Выполнить `.venv/bin/python -m unittest discover -s tests -v` в рабочей копии.
2. Снять SQLite-копию через `sqlite3.Connection.backup`, проверить `PRAGMA integrity_check`.
3. Выполнить `JAIL_DB_PATH=/путь/к/копии.db .venv/bin/python migrate.py` на копии данных. Не запускать проверку миграции на рабочей базе.
4. Остановить `jail.service`, снять окончательный снимок, обновить код `/opt/jail`, сохранив `.venv`, и запустить службу. Не копировать базу или секреты из исходников.
5. Проверить `systemctl status jail.service`, `/jail/version`, страницы трёх ролей и резервное копирование.
6. При отказе остановить службу, вернуть код из предыдущего Git commit и окончательный снимок базы, проверить права `jail:jail`, запустить службу.

Прежние версии исходников доступны в истории Git. Старые каталоги `jail2`, `jail3`, `jail4` удалены. Резервный снимок данных перед обновлением сохраняется отдельно от кода.

## Новый сервер

Создать пользователя `jail`, каталог `/opt/jail` с виртуальным окружением и установить `requirements.txt`. Каталог данных `/var/lib/jail` должен принадлежать `jail:jail`. Настройки `/etc/jail.env` должны быть доступны службе и закрыты для остальных пользователей.

Служба запускается от `jail`, с `WorkingDirectory=/opt/jail`, `EnvironmentFile=/etc/jail.env`, `Environment=JAIL_SKIP_MIGRATIONS=1`, `ExecStartPre=/opt/jail/.venv/bin/python /opt/jail/migrate.py` и `ExecStart=/opt/jail/.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8092 app:app`. Для `ProtectSystem=strict` разрешить запись через `ReadWritePaths=/var/lib/jail`.

Настроить Caddy для выбранного домена: `handle_path /jail/*` проксирует на `127.0.0.1:8092` и передаёт `X-Forwarded-Prefix: /jail`. Домен в приложении не зашит. Без префикса cookie path должен быть `/`, а proxy-prefix не требуется.
