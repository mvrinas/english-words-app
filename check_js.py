#!/usr/bin/env python3
"""Проверяет баланс JS скобок в index.html перед деплоем"""
import sys

with open('frontend/index.html', 'r') as f:
    html = f.read()

start = html.rfind('<script>')
end = html.rfind('</script>')
if start == -1 or end == -1:
    print("ERROR: <script> не найден")
    sys.exit(1)

js = html[start+8:end]
depth = 0
for ch in js:
    if ch == '{': depth += 1
    elif ch == '}': depth -= 1

if depth != 0:
    print(f"ERROR: Баланс скобок JS = {depth} (должен быть 0)")
    sys.exit(1)

print(f"OK: JS баланс = 0, строк = {len(js.splitlines())}")
