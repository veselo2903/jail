# jail — пакет. Позволяет монтировать приложение как jail.app рядом с основной системой.
#
# Установка обновлений «за один раз»: страница обновления кладёт новые файлы в папку _next,
# а применяются они здесь — при запуске процесса, ДО загрузки app.py. Поэтому работающий сайт
# никогда не видит смесь старого кода и новых шаблонов: до перезапуска — целиком старая версия,
# после — целиком новая.
import os as _os
import shutil as _shutil
import time as _time

_BASE = _os.path.dirname(_os.path.abspath(__file__))


def _apply_staged():
    nxt = _os.path.join(_BASE, "_next")
    work = _os.path.join(_BASE, "_applying")
    if _os.path.isdir(work):
        # другой процесс прямо сейчас применяет обновление — ждём (или убираем зависший остаток)
        try:
            stale = _time.time() - _os.path.getmtime(work) > 60
        except OSError:
            stale = False
        if stale:
            _shutil.rmtree(work, ignore_errors=True)
        else:
            for _ in range(50):
                if not _os.path.isdir(work):
                    break
                _time.sleep(0.2)
    if not _os.path.isdir(nxt):
        return
    try:
        _os.rename(nxt, work)          # атомарно: применяет только один процесс
    except OSError:
        return
    try:
        files = []
        for root, _dirs, names in _os.walk(work):
            for fn in names:
                files.append(_os.path.join(root, fn))
        files.sort(key=lambda p: _os.path.basename(p) == "VERSION")   # номер версии — последним
        for src in files:
            rel = _os.path.relpath(src, work)
            dst = _os.path.join(_BASE, rel)
            _os.makedirs(_os.path.dirname(dst), exist_ok=True)
            _shutil.copyfile(src, dst + ".part")
            _os.replace(dst + ".part", dst)
    finally:
        _shutil.rmtree(work, ignore_errors=True)


try:
    _apply_staged()
except Exception:
    # не роняем сайт, но записываем — будет видно на странице «Обновление системы»
    try:
        import traceback as _tb
        with open(_os.path.join(_BASE, "errors.log"), "a", encoding="utf-8") as _f:
            _f.write("\n==== установка обновления при перезапуске не удалась\n" + _tb.format_exc())
    except Exception:
        pass
