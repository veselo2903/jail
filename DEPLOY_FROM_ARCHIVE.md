# Развёртывание из архива

Инструкция для чистого сервера Debian 12 или Ubuntu 24.04 с systemd и Caddy. Замените `example.com` на свой домен и направьте его DNS на сервер. Путь сайта в примерах — `/jail/`.

## 1. Установить код и зависимости

После распаковки архива рядом находится папка `jail/`. Перейдите в каталог, где она лежит:

```bash
sudo apt update
sudo apt install -y python3 python3-venv caddy
id -u jail >/dev/null 2>&1 || sudo useradd --system --user-group --home /var/lib/jail --shell /usr/sbin/nologin jail
sudo install -d -o root -g jail -m 750 /opt/jail
sudo install -d -o jail -g jail -m 750 /var/lib/jail
sudo cp -a jail/. /opt/jail/
sudo chown -R root:jail /opt/jail
sudo find /opt/jail -type d -exec chmod 750 {} +
sudo find /opt/jail -type f -exec chmod 640 {} +
sudo python3 -m venv /opt/jail/.venv
sudo /opt/jail/.venv/bin/pip install -r /opt/jail/requirements.txt
```

Проверьте `python3 --version`: нужен Python 3.11 или новее. Окружение `.venv/` и база в исходный архив не входят.

## 2. Создать настройки

Скопируйте пример и **сгенерируйте новый ключ**. Не переносите ключ с чужого сервера:

```bash
sudo cp /opt/jail/ops/jail.env.example /etc/jail.env
sudo sed -i "s/REPLACE_WITH_RANDOM_SECRET/$(openssl rand -hex 32)/" /etc/jail.env
sudo chown root:root /etc/jail.env
sudo chmod 600 /etc/jail.env
```

В примере `JAIL_DB_PATH=/var/lib/jail/jail.db` и `JAIL_COOKIE_PATH=/jail`. Если используете иной путь сайта, измените `JAIL_COOKIE_PATH` и `X-Forwarded-Prefix` в Caddy вместе с маршрутом. Домен в коде приложения не задан.

При первом запуске приложение создаст пустую SQLite базу в `/var/lib/jail/`.

## 3. Запустить Gunicorn через systemd

```bash
sudo cp /opt/jail/ops/jail.service.example /etc/systemd/system/jail.service
sudo systemctl daemon-reload
sudo systemctl enable --now jail.service
sudo systemctl status jail.service
```

Служба слушает только `127.0.0.1:8091`; порт наружу открывать не нужно. Для проверки до настройки домена: `curl -I http://127.0.0.1:8091/login`.

## 4. Подключить домен в Caddy

Откройте [`ops/Caddyfile.example`](ops/Caddyfile.example), замените `example.com` на ваш домен и добавьте блок в `/etc/caddy/Caddyfile`. Если Caddy уже обслуживает этот домен, перенесите внутрь его существующего блока только правила для `/jail` из примера.

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -I https://example.com/jail/login
```

Откройте `https://example.com/jail/` в браузере. На чистой установке система предложит создать первые записи; тестовые заказчики и сотрудники не создаются.

## 5. Резервные копии

В папке [`ops/`](ops) есть скрипт резервирования базы и фотографий. Для ежедневного запуска:

```bash
sudo install -m 755 /opt/jail/ops/backup.py /usr/local/sbin/jail-backup.py
sudo install -d -m 700 /srv/backups/jail
sudo cp /opt/jail/ops/jail-backup.service.example /etc/systemd/system/jail-backup.service
sudo cp /opt/jail/ops/jail-backup.timer.example /etc/systemd/system/jail-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now jail-backup.timer
sudo systemctl start jail-backup.service
```

Снимки должны храниться отдельно от сервера. Для переноса **существующих** данных понадобится отдельная копия `/var/lib/jail/jail.db` и `/var/lib/jail/model-photos/`; их нет в архиве исходников. Переносите их только вместе с согласованной резервной копией и остановленной службой.

## Локальная проверка без systemd

```bash
cd jail
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export JAIL_SECRET_KEY="$(openssl rand -hex 32)"
export JAIL_DB_PATH="$(mktemp -d)/jail.db"
.venv/bin/python app.py
```

После запуска открыть `http://127.0.0.1:5001/login`. Это отдельная чистая тестовая база.

**Доступ:** текущий выпуск работает без паролей — посетитель адреса может выбрать любую роль. Перед публичным использованием получатель должен учитывать этот установленный владельцем тестовый режим.
