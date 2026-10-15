#!/usr/bin/env python3
"""Скрипт-проверка timing-safety аутентификации (F5).

Проверяемые инварианты (запуск на backend/ проекта):
1. В коде аутентификации нет прямых сравнений секретов (==, != на паролях/токенах).
2. Пароль проверяется только через bcrypt.checkpw.
3. Есть dummy-проверка для несуществующих логинов (constant-time ответ).
4. Сессионные токены генерируются секретным генератором (secrets), не random.

Выход: exit 0 — все инварианты ок; exit 1 — перечислены нарушения.
Подключение в конвейер: шаг ревью auth-задач (агент может забыть — скрипт нет).
"""
import ast
import re
import sys
from pathlib import Path


def find_auth_module(backend: Path) -> Path:
    for name in ("auth.py", "security.py", "users.py"):
        p = backend / "app" / name
        if p.is_file():
            return p
    raise SystemExit("auth-модуль не найден в backend/app/")


def violations(src: str, tree: ast.AST) -> list[str]:
    out = []

    # 1. Прямые сравнения секретов: любые сравнения строк-переменных с
    #    «подозрительными» именами (password, passwd, secret, token, hash).
    secret_re = re.compile(r"password|passwd|secret|token|hash", re.I)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            names = []
            for side in (node.left, node.comparators[0]):
                if isinstance(side, ast.Name):
                    names.append(side.id)
                elif isinstance(side, ast.Attribute):
                    names.append(side.attr)
            if any(secret_re.search(n or "") for n in names):
                out.append(
                    f"строка {node.lineno}: прямое сравнение (==/!=) секрета: {names}"
                )

    # 2-3. checkpw и dummy.
    if "checkpw" not in src:
        out.append("bcrypt.checkpw не используется в auth-модуле")
    if not re.search(r"dummy", src, re.I):
        out.append("нет dummy-проверки для несуществующих логинов (timing defense)")

    # 4. Токены: secrets.*, не random.*
    if re.search(r"\brandom\.(choice|randint|random|getrandbits|randbytes)", src):
        if "random" in src and "fake_random" not in src:
            out.append("random.* в auth-модуле: токены/секреты только через secrets.*")
    if "secrets." not in src and "token" in src.lower():
        out.append("токены есть, но secrets.* не используется")
    return out


def main() -> int:
    backend = Path(sys.argv[1] if len(sys.argv) > 1 else "backend")
    auth = find_auth_module(backend)
    src = auth.read_text(encoding="utf-8")
    tree = ast.parse(src)
    probs = violations(src, tree)
    rel = auth.relative_to(backend.parent)
    if probs:
        print(f"TIMING-AUTH FAIL ({rel}):")
        for p in probs:
            print(f"  - {p}")
        return 1
    print(f"TIMING-AUTH OK ({rel}): checkpw + dummy + без прямых сравнений секретов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
