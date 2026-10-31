---
name: ui-ux-matrices
description: Searchable UI/UX matrices — styles, palettes, font pairs, UX rules
license: MIT — vendored from https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
metadata:
  hermes:
    tags: [design, ui, ux, typography, color, style, search]
    related_skills: [frontend-design, design-review, claude-design, design-md]
---

# UI/UX Matrices

> **Vendor / license:** `scripts/`, `data/`, `references/` скопированы целиком из
> [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill)
> (MIT License, Copyright (c) 2024 Next Level Builder). Это данные + поисковик, не код
> нашего конвейера: не рефакторить, обновлять заменой каталога.

Локально-поисковая база дизайн-интеллидженса: 79 стилей, 192 продуктовые палитры с reasoning-профилями, 74 шрифтовые пары, 119 UX-гайдлайнов, 105 иконок, 17 GSAP-пресетов, 25 типов чартов, 22 стека. Всё оффлайн: Python 3 stdlib, без внешних зависимостей, без сети.

## Когда применять

Задача меняет то, как интерфейс **выглядит, ощущается, двигается или взаимодействует**: новая страница/экран, выбор стиля/палитры/шрифтовых пар, ревью UX/доступности, навигация, анимация, responsive. Не применять для чистого бэкенда, API/БД, инфраструктуры.

## Матрица приоритетов 1→10

Какую категорию проверять первой; полные тексты правил — `references/quick-reference.md`.

| Приоритет | Категория | Impact | Домен | Must have | Anti-patterns |
|---|---|---|---|---|---|
| 1 | Accessibility | CRITICAL | `ux` | Контраст 4.5:1, alt, клавиатура, aria-label | Снятые focus rings, icon-only без label |
| 2 | Touch & Interaction | CRITICAL | `ux` | Мин. 44×44px, отступ 8px+, loading feedback | Только hover, мгновенная смена состояний |
| 3 | Performance | HIGH | `ux` | WebP/AVIF, lazy, резерв места (CLS<0.1) | Layout thrashing, прыжки вёрстки |
| 4 | Style Selection | HIGH | `style`, `product` | Стиль под тип продукта, SVG-иконки | Смешение flat/скевоморфизма, emoji как иконки |
| 5 | Layout & Responsive | HIGH | `ux` | Mobile-first, viewport meta, без гор. скролла | Фикс. px-контейнеры, отключённый zoom |
| 6 | Typography & Color | MEDIUM | `typography`, `color` | Body 16px, line-height 1.5, семантические токены | Text <12px, gray-on-gray, сырой hex в компонентах |
| 7 | Animation | MEDIUM | `ux`, `gsap` | Тайминг по контексту, reduced-motion | Одна duration на всё, анимация width/height |
| 8 | Forms & Feedback | MEDIUM | `ux` | Видимые label, ошибка у поля, helper text | Label только placeholder, ошибки только сверху |
| 9 | Navigation | HIGH | `ux` | Предсказуемый back, bottom nav ≤5, deep links | Перегруженная навигация, сломанный back |
| 10 | Charts & Data | LOW | `chart` | Легенды, tooltips, доступные цвета | Смысл передаётся только цветом |

## Как звать search.py

Скрипт лежит внутри каталога скилла — зови полным путем относительно скилла, не полагаясь на cwd:

```bash
python3 <skill-dir>/scripts/search.py "<query>" --domain <domain> [-n <max>]
# skill-dir = каталог этого SKILL.md; варианты: ~/.hermes/skills/creative/ui-ux-matrices/scripts/search.py
```

Новые страницы/проекты — генерация дизайн-системы целиком:

```bash
python3 <skill-dir>/scripts/search.py "<product> <industry> <keywords>" --design-system [-p "Name"] \
  [--variance 1-10] [--motion 1-10] [--density 1-10]
```

Дальше `--persist -p "Name" --output-dir <project-root>` сохраняет `design-system/<slug>/MASTER.md` (+ страницы в `pages/`); `MASTER.md` без `--force` не перезаписывается.

## Query-контракт

Один доминирующий интент на запрос, 2–5 осмысленных слов + одно ограничение (продукт/платформа/интеракция). Выбери минимальный режим:

1. Новый проект/страница или общая визуальная директория → `--design-system`.
2. Точечная проблема/компонент → один явный `--domain`.
3. Известный стек → `--stack <stack>` (отдельным запросом, не вместо domain).

Домены: `style`, `color`, `chart`, `landing`, `product`, `ux`, `typography`, `google-fonts`, `icons`, `gsap`, `react`, `web`. Автодетект домена бывает промахивается — при нерелевантных результатах задавай `--domain` явно. Доступность: ищи по наблюдаемому исходу (`"error summary validation" --domain ux`), не по имени компонента; семантический исход → потом компонент → потом стек.

**0 результатов:** не выдумывай. Один ретрай более узкой формулировкой / явным доменом; снова пусто — скажи, что верифицированного совпадения нет, и пометь общий совет как fallback. Не персисти непроверенный вывод.

## У нас в конвейере

- Выбор стиля/палитры/шрифтовой пары для мокапов: `--design-system` → перенеси выбранное в токены `design/tokens.html` (J47), а не «сырые hex в мокапе».
- Для HTML-мокапов без CDN (ОГР-8) данные `--design-system` — источник намерения (характер палитры, шкала, стиль), а не готовых веб-шрифтов: подставляй системные стеки с тем же характером.
- Ревью-сторонa — в `design-review`; эта база — справочник, не чек-лист сдачи.

## Прежде чем сдавать app-UI (native)

Прогони `references/pro-rules.md` — канонический Pre-Delivery Checklist (иконки, touch feedback, контраст light/dark, safe areas, a11y).
