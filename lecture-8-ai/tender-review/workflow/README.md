# Workflow проверки тендера

[workflow.json](../workflow.json) содержит проверенный workflow `Lab 8: Tender review` для n8n 2.45.6: 14 узлов от ручного запуска до JSON-файла.

Полный запуск № 21 завершился успешно за 84,454 секунды. Пять вызовов MCP получили политику, профиль, список пакета и оба документа; сбор занял 65,867 секунды. Оценка прошла проверку: `support_schedule=mismatch`, `response_time=needs_clarification`.

Узел `Assessment JSON` создал `demo-tender-v1-evaluation-21.json` (`application/json`, 1307 байт). Его точная копия сохранена в [reports/evaluation-demo-tender-v1.json](../reports/evaluation-demo-tender-v1.json).

## Цепочка

Ручной запуск → регистрация начала → AI Agent с двумя MCP → проверка полноты → регистрация исхода сбора → проверка `complete` → подготовка запроса → оценка → проверка JSON → JSON-файл.

- `Collection context` фиксирует идентификатор исполнения и время до повторяемого HTTP-запроса.
- `Register collection` требует подтверждение exporter до обращения к модели.
- `AI Agent` получает источники, оба его выхода ведут в `Validate collection`.
- `Validate collection` сверяет успешные ответы инструментов с манифестом. Пересказ модели не используется.
- `Finish collection` сохраняет исход `complete / incomplete / error`. Доставка события повторяется до трёх раз с прежними данными.
- `Require complete context` останавливает неполный пакет после регистрации исхода.
- `Prepare assessment` передаёт только проверенные источники, skill оценки и JSON Schema. Идентификаторы, критерии и допустимые ссылки берёт из текущего контекста.
- `Evaluate tender` вызывает native Ollama `/api/chat`: `think:false`, температура 0, контекст 16384. Автоматического повтора генерации нет.
- `Validate assessment` проверяет JSON, точные наборы документов и критериев, версии, статусы, вопросы и существование ссылок. Смысловую правильность вывода этот код не доказывает.
- `Assessment JSON` создаёт один скачиваемый JSON-объект. Режим `each` нужен, чтобы не получить массив из одного элемента.

Исходники Code nodes находятся рядом: [подготовка сбора](prepare-collection.js), [проверка сбора](validate-collection.js), [проверка полноты](require-complete.js), [подготовка оценки](prepare-assessment.js), [проверка оценки](validate-assessment.js).

## Повторный запуск

1. Запустить MCP, Ollama и exporter по [README сервисов](../mcp-services/README.md).
2. Импортировать `workflow.json` как `Lab 8: Tender review`. В узле `Ollama/Qwen3:8b` выбрать существующие credentials с адресом `http://host.docker.internal:11434`.
3. Нажать **Execute workflow**. В `Validate collection` должны быть `complete`, пустые `missing_documents` и `errors`.
4. В `Validate assessment` проверить два вывода тестового пакета: несовпадение графика поддержки и уточнение срока реакции.
5. В `Assessment JSON → Binary → data` скачать файл.

Файл создаётся во внутреннем binary storage n8n, который находится в томе `n8n_data`. Он переживает перезапуск контейнера, но подчиняется очистке истории исполнений. В папку репозитория этот узел файл не записывает.

Метрики `tender_collection_*` относятся к сбору источников. Его `complete` ещё не означает, что оценка прошла проверку и файл был создан. Полнота ограничена манифестом, доступным MCP.

## Проверка кода

Из `lecture-8-ai/tender-review`:

```sh
node --test workflow/tests/*.test.mjs
```

Проверяются фактические тела Code nodes из экспорта. [Фикстура исполнения 15](tests/fixtures/execution-15-tools.json) подтверждает обнаружение непрочитанного договора. Искусственные входы покрывают чужие версии, неполные пакеты, выдуманные ссылки, дубликаты критериев, обрезанный и некорректный ответ модели.

[Протокол проверок](../reports/verification.json) содержит результаты полного запуска № 21, предыдущих сборов и тестов кода.
