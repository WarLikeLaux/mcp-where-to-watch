"""Smoke-тест MCP-сервера where-to-watch: initialize -> tools/list -> tools/call.

Запуск:  python3 test_smoke.py
Требует: сеть (ходит в JustWatch, kinopoisk.ru и amediateka.ru) и установленный uv.
Сторонних python-зависимостей у самого теста нет.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")

proc = subprocess.Popen(
    [UV, "run", "--directory", str(ROOT), "server.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, bufsize=1,
)

def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()

def recv(want_id):
    while True:
        line = proc.stdout.readline()
        if not line:
            print("STDOUT закрыт, сервер упал?", file=sys.stderr)
            sys.exit(1)
        msg = json.loads(line)
        if msg.get("id") == want_id:
            return msg

def call(req_id, name, arguments, label):
    send({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
          "params": {"name": name, "arguments": arguments}})
    result = recv(req_id)["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        print(f"== {label}: ОШИБКА ИНСТРУМЕНТА:\n{text}", file=sys.stderr)
        sys.exit(1)
    print(f"== {label}:\n{text}")

try:
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "smoke", "version": "0"}}})
    info = recv(1)["result"]["serverInfo"]
    print(f"== initialize OK: {info['name']} {info['version']}")
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = [t["name"] for t in recv(2)["result"]["tools"]]
    print(f"== tools/list OK: {tools}")

    call(3, "where_to_watch", {"query": "Начало", "kind": "movie", "limit": 3},
         "where_to_watch('Начало')")
    call(4, "popular_on", {"service": "кинопоиск", "kind": "movie", "limit": 5},
         "popular_on('кинопоиск')")
    call(5, "where_to_watch", {"query": "Прибытие"}, "where_to_watch('Прибытие')")
    call(6, "popular_on", {"service": "несуществующий"}, "popular_on(бред) -> понятная ошибка")
    call(7, "check_kinopoisk", {"query": "Трасса 60"}, "check_kinopoisk('Трасса 60') -> ожидаем доступен")
    call(8, "check_kinopoisk", {"query": "Реквием по мечте"}, "check_kinopoisk('Реквием') -> ожидаем недоступен")
    call(9, "check_amediateka", {"query": "Поймать Мэри"}, "check_amediateka('Поймать Мэри') -> ожидаем найдено")
    print("\n== ВСЕ ТЕСТЫ ПРОШЛИ")
finally:
    proc.terminate()
