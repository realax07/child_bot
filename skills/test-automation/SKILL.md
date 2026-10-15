---
name: test-automation
description: Implement approved test cases as pytest+requests and pytest+playwright suites with tracing.
---

# Автоматизация тест-кейсов

Как превратить одобренные тест-кейсы в изолированные детерминированные автотесты. Стек конвейера: Python, pytest, requests (API), playwright (web).

## Когда применять

- Кейсы появились в `test-model/approved/<change-id>/`
- Расширение существующего набора тестов новыми одобренными кейсами

Не применять для: кейсов в `test-model/new/` (не прошли ревью), правки продукта (находка дефекта → баг-репорт).

**UI-дельта ⇒ web-тесты обязательны (J50, 2026-10-14).** Если change-пакет
содержит макеты (`design/`) или UI-дельту по спеке — web-e2e не опциональны:
каждый сценарий с UI-поверхностью получает e2e-проверку; видимые элементы
мокапа (кнопки, состояния, раскладка-инварианты) сверяются с эталоном, токены
— из `design/tokens.html` (рассинхрон = дефект, ловит design_validator).
Исключение — явный `[no-ui-tests]`-маркер в PR с основанием (проверяет
pr_validate); молчаливый скип UI-задачи без web-тестов = провал CI.

## Структура тестового проекта

```
tests/
├── api/                        # pytest + requests
│   ├── conftest.py             # фикстуры: base_url, auth, http-сессия
│   └── test_<domain>.py
├── web/                        # pytest-playwright
│   ├── conftest.py             # фикстуры: браузер, контекст, base_url
│   └── test_<domain>_ui.py
└── README.md                   # запуск, env-переменные, маркеры
```

## Процедура

1. **Проверь вход:** кейсы лежат в `approved/`, review с вердиктом «одобрить» существует. Нет — работа не начинается, это контракт 6.
2. **Каркас фикстур до тестов.** `base_url` из переменной окружения, HTTP-сессия с auth, браузерный контекст. Тесты без фикстур размножают настройку и гниют при смене окружения.
3. **1 кейс = 1 тест минимум.** В docstring — ID кейса (`TC-pages-001`) — это трассировка, по ней матрица в `tests/README.md`. Тест без ID — дефект.
4. **Маркеры:** `@pytest.mark.api` / `@pytest.mark.web` для выбора набора; `@pytest.mark.must/should/could` для приоритета MoSCoW из кейса.
5. **Ассерты = ожидаемый результат кейса.** Дословно: кейс требует «200 OK, тело содержит token (JWT)» → два ассерта: статус и валидность JWT. Ослабленный ассерт «код 2xx» — скрытая потеря проверки.
6. **Изоляция:** каждый тест создает свои данные через фикстуры (setup) и убирает (teardown). Тест, зависящий от порядка запуска или данных другого теста, — флак в CI.
7. **Детерминизм:** никаких `time.sleep`. Web — автожидания Playwright (`expect(...).to_be_visible()`), API — poll-цикл с таймаутом. Sleep «пока заработает» — отсроченный флак.
8. **Прогон и фиксация результата.** Падение? Сначала проверь код теста против кейса. Тест верен → баг-репорт в `test-model/bugs/` (кейс, ожидание, факт, окружение). Править ассерт «чтобы был зеленый» — запрещено: это маскировка дефекта продукта.

## Структура теста (AAA / Given-When-Then)

Тело теста — три блока с пустой строкой между ними; комментарии `# Arrange / Act / Assert` (API) или GWT-шаги (web) обязательны для тестов длиннее 5 строк:

```python
def test_create_page_returns_201(base_url, http_session):
    """TC-wiki-020 (FR-107): создание страницы от владельца"""
    payload = {"title": "Собрание 12.10", "content": "повестка"}          # Arrange
    resp = http_session.post(f"{base_url}/api/wiki/pages", json=payload)  # Act
    assert resp.status_code == 201                       # Assert
    assert resp.json()["title"] == payload["title"]
```

## Test data builders

Данные создает фабрика-фикстура с teardown-удалением (см. `wiki_pages_created` в tests/web/test_p15_wiki_tree.py — эталон): функция `_create(**overrides)` с валидными значениями по умолчанию, тест переопределяет только релевантное поле. Builder без teardown — мусор в стенде и флаки соседей.

## Контрактная проверка (openapi)

Проект держит схему `contracts/openapi.json` (экспорт export_openapi.py). API-тесты проверяют СТРУКТУРУ ответа по схеме (обязательные поля, типы) — не только бизнес-ассерты:

```python
def test_wiki_page_schema(resp, openapi_schema):
    """TC-wiki-021: ответ POST /api/wiki/pages соответствует контракту"""
    schema = openapi_schema["components"]["schemas"]["WikiPage"]
    for field in schema["required"]:
        assert field in resp.json(), f"контракт: нет {field}"
```

## Property-based (опция, чистые функции)

Для функций-инвариантов (`_strip_tags`, сериализация, валидаторы) — `hypothesis` вместо ручного перебора классов:

```python
from hypothesis import given, strategies as st

@given(st.text())
def test_strip_tags_never_keeps_angle_brackets(text):
    assert "<" not in _strip_tags(text)
```

Не применять к stateful-эндпоинтам (там кейсы из approved-набора).

## Page Objects (web-тесты)

Локаторы живут в Page Object, тесты — о поведении. Без PO дублирование
`get_by_role`/`locator` расползается по файлам (26 локаторов в одном
тест-файле — реальный случай из tests/web до внедрения).

```python
# tests/web/pages/__init__.py — пустой (package)
# tests/web/pages/wiki_page.py
from playwright.sync_api import expect


class WikiPage:
    """Page Object /wiki: локаторы в одном месте, тесты — о поведении."""

    def __init__(self, page):
        self.page = page
        self.tree = page.locator("ul.wiki-tree")
        self.search_input = page.get_by_placeholder("Поиск")
        self.create_first = page.get_by_role(
            "button", name="Создать первую страницу")

    def open(self, base_url):
        self.page.goto(f"{base_url}/wiki")
        expect(self.tree).to_be_visible()

    def search(self, query):
        self.search_input.fill(query)

    def results(self):
        return self.page.locator(".wiki-search-results .tree-link")

    def toggle_node(self, title):
        self.page.get_by_role("button", name=title).first.click()
```

Тест с PO (сравни: три строки поведения вместо десяти локаторов):

```python
def test_search_finds_page(logged_in_page, web_base_url, wiki_pages_created):
    """TC-wiki-002 (FR-114): поиск по заголовку"""
    wiki = WikiPage(logged_in_page)
    wiki_pages_created("Сборка шкафа", "инструкция")     # Arrange
    wiki.open(web_base_url)                              # Act
    wiki.search("шкаф")
    expect(wiki.results().first).to_contain_text("Сборка шкафа")  # Assert
```

Правила:
- **Ассерты — в тестах**, не в PO (метод PO возвращает locator/состояние; единственное исключение — wait-обертки вроде `open()`).
- PO принимает `page`, не знает о фикстурах/БД; данные — через builders.
- Крупный экран дели на компонентные PO (`WikiTree`, `WikiToolbar`), вложенные в Page.
- Локаторы в PO — по приоритету playwright-pro (get_by_role → … → CSS крайний случай).
- PO — не догма: одноэкранный smoke без переиспользования можно писать напрямую.
- Переиспользуемый PO для существующих страниц вынеси в `tests/web/pages/` и используй в новых тестах; массовая конвертация старых — отдельная задача, не попутная правка.

## Шаблон API-теста

```python
import pytest, requests

pytestmark = [pytest.mark.api, pytest.mark.must]

def test_login_returns_token(base_url, http_session):
    """TC-auth-001: валидный логин возвращает JWT (FR-1)"""
    resp = http_session.post(f"{base_url}/api/auth/login",
                             json={"login": "test_user_1", "password": "Corr3ct_Pass!"})
    assert resp.status_code == 200
    assert "token" in resp.json()
```

## Ошибки

- Хардкод `http://localhost:8000` в тестах — окружение только через переменные.
- Тест без docstring-ID — трассировка разорвана.
- Свежесозданный тест мимо approved-кейса — «улучшение» мимо процесса; идея → новый кейс через ПМ.
