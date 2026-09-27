"""Reference data belongs in isolated tests, never in application startup."""
def reference_data(db):
    with db.get_db() as conn:
        for name in ('Заказчик 1','Заказчик 2','Заказчик 3'):
            conn.execute('INSERT INTO customers(name) VALUES (?)',(name,))
        for name in ('Модель 1','Модель 2','Модель 3'):
            conn.execute('INSERT INTO models(name) VALUES (?)',(name,))
        for number,name in (('1','Работник 1'),('2','Работник 2')):
            conn.execute('INSERT INTO workers(number,name) VALUES (?,?)',(number,name))
