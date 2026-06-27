#!/usr/bin/env python3
"""Проверяет баланс JS скобок в index.html перед деплоем"""
import sys, re

with open('frontend/index.html', 'r') as f:
    html = f.read()

scripts = re.findall(r'<script[^>]*>([\s\S]*?)</script>', html)
if not scripts:
    print("ERROR: <script> не найден"); sys.exit(1)

errors = []
for i, js in enumerate(scripts):
    depth = 0
    for ch in js:
        if ch == '{': depth += 1
        elif ch == '}': depth -= 1
    if depth != 0:
        errors.append(f"Блок {i+1} ({len(js.splitlines())} строк): баланс = {depth}")

if errors:
    print("ERROR: " + "; ".join(errors)); sys.exit(1)

total = sum(len(s.splitlines()) for s in scripts)
print(f"OK: {len(scripts)} блоков, {total} строк, баланс = 0")
