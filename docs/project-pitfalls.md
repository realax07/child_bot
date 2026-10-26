
## 2026-10-26 — requirements.txt вне зон ролей (python-проект)

Политика зон фабрики (role_zone_policy.py, адаптация с wiki-монорепо) не покрывает
`requirements.txt`/`pyproject.toml` для python-проектов: ba имеет только `requirements.md`,
dev — `src/**, tests/**, backend/**...`. Задача 1.1 add-core-diary уперлась:
ZONE_OUTSIDE_POLICY, зависимость python-telegram-bot не объявлена -> review-001-1.1 F-1
(major). Обход: ПМ добавил requirements.txt админ-коммитом. Предложение фабрике:
добавить requirements.txt, pyproject.toml в зону dev для python-проектов (rules_change).

## 2026-10-26 — provenance-привязка мешает merge_task (SHA==HEAD)

Семплирование: sidecar (record-review) обязан ссылаться на ТЕКУЩИЙ HEAD main и дифф
HEAD^..HEAD. Но merge_task и есть создание этого коммита: пока слияние не сделано —
HEAD чужой; после — sidecar устарел. Для соло-ПМ-циклаmerge_task непроходим в любом
порядке. Обход: rebase ветки на main + ff-merge + чекбокс админ-коммитом ПМ (контент
одобрен review-001/002, вердикты в code-reviews/). Предложение фабрике: считать
sidecar валидным, если reviewed_commit_sha — предок HEAD без изменений контента
(diff по файлам задачи), либо проводить слияние в сессии dev_lead с записью sidecar
после мерджа (порядок стадий).

## 2026-10-26 — pm_bounds J2 против общего реестра (мультипроект)

pm_bounds_check --sessions флагует ЛЮБОЙ повтор пары (project, owner_pm) в
active_sessions.json, включая завершенные сессии за всю историю. Общий реестр
(127 сессий wiki/ai-factory/child_bot) делает ворота непроходимыми. Решение:
проектный скоуп-реестр active_sessions.child_bot.json (+ .archive.json для
завершенных; флаг --registry у flowctl для этого и есть). Общий реестр не тронут.
Предложение фабрике: J2 должен учитывать только НЕ-терминальные сессии или
фильтровать по project.
