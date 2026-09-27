jail — заказы и партии обувного производства, материалы, выработка и зарплата.
Стек: Flask 3 + SQLite + Jinja2. Рабочее размещение: qr4you.ru/jail/.

Склад: входящие заявки, поставки, материалы и подготовка партий.
Производство: задания партий, выполненная работа и выпуск готовой обуви.
Директор: заказы, расценки, затраты и выплаты зарплаты.
Начисление по операции записывается отдельно от фактической выплаты.
Права и финансовые сведения зависят от роли; вход пока без паролей.

Развёртывание и резервные копии: DEPLOYMENT.md.
Карта системы: docs/PRODUCTION_PAYROLL_MAP.md.
Первое использование и сценарии: docs/USABILITY_REVIEW_061.md.
Зависимости закреплены в requirements.txt.
Проверки: tests/smoke.py, tests/business.py, tests/experience.py.

Установка как раздел /jail внутри основной системы (тот же аккаунт ivnest):
1. Загрузить папку jail в /home/ivnest/jail (внутри: app.py, db.py, __init__.py, templates, static).
2. В WSGI-файле web-app подключить оба приложения через DispatcherMiddleware:

   import sys
   project_home = "/home/ivnest"          # родитель пакета jail
   if project_home not in sys.path:
       sys.path.insert(0, project_home)

   from app import app as main_app        # основная система (как было в текущем WSGI)
   from jail.app import app as jail_app   # новое приложение jail

   from werkzeug.middleware.dispatcher import DispatcherMiddleware
   application = DispatcherMiddleware(main_app, {"/jail": jail_app})

   Строку основного импорта и путь к основной системе оставьте как в вашем текущем
   WSGI, только переименуйте её результат в main_app.
3. Reload на вкладке Web.
4. Открыть ivnest.pythonanywhere.com/jail/

Основная система не меняется — jail отдельное приложение со своей базой jail.db.
Тестовый режим: вход по кнопкам-ролей без пароля. Пароли/PIN — позже.
