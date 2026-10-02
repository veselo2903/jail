# jail 1.27

Учёт заявок и передач между складом и производством обуви, двух складских зон, приёмки, расхождений и долга производству.

Стек: Flask 3, SQLite, Gunicorn. При первом запуске создаётся пустая база и справочник из восьми операций с начальными расценками. Заказчики, модели и сотрудники не создаются автоматически.

## Локальный запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export JAIL_DB_PATH="$PWD/jail.db"
export JAIL_SECRET_KEY="$(openssl rand -hex 32)"
.venv/bin/flask --app app run
```

Вход в тестовом режиме — выбор роли без пароля. Не размещайте приложение с реальными данными в открытом доступе до настройки авторизации.

Сравнение с прежней версией: [COMPARISON.md](COMPARISON.md). Для развёртывания на сервере: [DEPLOYMENT_QR4YOU.md](DEPLOYMENT_QR4YOU.md). Документ [HANDOFF.md](HANDOFF.md) описывает передачу проекта.

Проверка: `.venv/bin/python -m unittest discover -s tests -v`. Исправления аудита: [FIXES_116.md](FIXES_116.md).
