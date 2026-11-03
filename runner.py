#!/usr/bin/env python3
"""Запуск программы с замером: runner.py <файл-итога> <программа> [аргументы…]
Ввод и вывод остаются в терминале; в файл-итог пишется « · 0.12 s · 3.4 MB» (время и пик памяти),
код выхода — как у программы. GNU time в системе может не стоять, а wait4 есть везде."""
import os, signal, sys, time

out, cmd = sys.argv[1], sys.argv[2:]
signal.signal(signal.SIGINT, signal.SIG_IGN)   # Ctrl+C достаётся программе, а замер всё равно запишется
t0 = time.monotonic()
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try: os.execvp(cmd[0], cmd)
    except OSError as e: print(f"не удалось запустить {cmd[0]}: {e.strerror}", file=sys.stderr); os._exit(127)
_, status, ru = os.wait4(pid, 0)
dt = time.monotonic() - t0
with open(out, "w") as f: f.write(f" · {dt:.2f} s · {ru.ru_maxrss / 1024:.1f} MB")
sys.exit(os.waitstatus_to_exitcode(status) % 256 if os.WIFEXITED(status) else 128 + os.WTERMSIG(status))
