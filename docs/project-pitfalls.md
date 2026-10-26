
## 2026-10-26 — requirements.txt вне зон ролей (python-проект)

Политика зон фабрики (role_zone_policy.py, адаптация с wiki-монорепо) не покрывает
`requirements.txt`/`pyproject.toml` для python-проектов: ba имеет только `requirements.md`,
dev — `src/**, tests/**, backend/**...`. Задача 1.1 add-core-diary уперлась:
ZONE_OUTSIDE_POLICY, зависимость python-telegram-bot не объявлена -> review-001-1.1 F-1
(major). Обход: ПМ добавил requirements.txt админ-коммитом. Предложение фабрике:
добавить requirements.txt, pyproject.toml в зону dev для python-проектов (rules_change).
