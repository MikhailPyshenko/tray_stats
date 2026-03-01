# -*- coding: utf-8 -*-
"""Точка входа: python run.py — трей с подсказкой. Запускайте из терминала, чтобы видеть ошибки."""
import atexit
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _silence_tcl_exit():
    """Убирает сообщение Tcl_AsyncDelete при выходе (оно безвредно, если открывали окно показателей)."""
    try:
        sys.stderr = type("_", (), {"write": lambda s, *a: None, "flush": lambda: None})()
    except Exception:
        pass


atexit.register(_silence_tcl_exit)

if __name__ == "__main__":
    try:
        from src.tray_stats import main
        print("Трей запускается. Иконка у часов (или в ^). ПКМ — Выход.")
        main()
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        import traceback
        print("Ошибка:", e)
        traceback.print_exc()
        input("Нажмите Enter для выхода...")
        sys.exit(1)
