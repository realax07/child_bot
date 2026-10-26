# PLAN: релиз пакета add-core-diary (child_bot)

- **Дата:** 2026-10-26
- Флоу: 1 (полный: requirements → create_change → arch-review → dev → review → merge → QA-кейсы → ревью кейсов → impact → автотесты → archive → release)
- **Автор плана:** ПМ (Мария-сессия)
- **Решение продакта:** decisions/2026-10-26-approve-requirements-core-diary.md (requirements УТВЕРЖДЕН, 5e7b50a)

## Цель пакета

Ядро дневника сна: кнопки «😴 Заснул»/«☀️ Проснулся», ввод задним числом (3 дня),
склейка снов (20 мин), разметка день/ночь (19:00–07:00, ночь за день начала),
кнопка «↩️ Отменить последнее», роль «наблюдатель», 13 дефолтных настроек,
приватность (всё локально на нашем сервере). Без окон сна и напоминаний (Пакет 2).

## Зоны записи (по ролям)

- sa: `openspec/changes/add-core-diary/**`
- dev: `src/**`, `tests/**`, `openspec/changes/add-core-diary/tasks.md`
- code_reviewer: `code-reviews/add-core-diary/review-*.md`
- qa_case_author / qa_case_reviewer / qa_impact / qa_automation: `test-model/**`
- pm: `PLAN-add-core-diary.md`, `decisions/**`

## Критерий приемки пакета

1. Все задачи tasks.md закрыты, code-review APPROVE по каждой dev-задаче.
2. Юнит-тесты на расчеты (разметка, склейка, конфликты, «вчера», отмена) — green.
3. Smoke: бот стартует, /start, две кнопки, ввод задним числом, отмена.
4. Ручная приемка продакта в реальном Telegram-боте (Пакет 1 целиком).

## Вехи

1. create_change (СА): proposal, specs-дельты, tasks.md, sdd — до dev.
2. architecture_review (ПМ) — после create_change.
3. Dev-волны по tasks.md (порядок: каркас+БД → события/меню → разметка/склейка → задним числом/отмена → наблюдатель/настройки).
4. QA-цепочка, archive, release.

## Риски

- Часовой пояс из TG-профиля может отсутствовать → fallback: уточнение при /start (в requirements).
- Дней-граница 06:00 и «ночь за день начала» — приоритет юнит-тестам на границы.
