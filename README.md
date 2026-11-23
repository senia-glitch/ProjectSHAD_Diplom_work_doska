# doska

Локальный пример на личном backend-стеке. Python 3.14, PostgreSQL 17. Пример заметок не содержит авторизации.

1. Создайте отдельное Python-окружение и установите `requirements-tested.txt`.
2. `python -m scripts.configure` создаёт локальный `.env`; существующий файл сохраняется.
3. `docker compose up -d db --wait` поднимает PostgreSQL с отдельной тестовой БД.
4. `python -m scripts.migrate --database doska` применяет просмотренные ревизии.
5. `python -m scripts.check` и `python -m scripts.check --integration --database doska_test` запускают проверки.
6. `python -m app.main` запускает HTTP на 127.0.0.1:8000; `/docs` содержит OpenAPI.

Полностью в Docker: `docker compose build app`, `docker compose run --rm migrate`, `docker compose up -d app --wait`. Внешний порт приложения — 18090. Миграции при HTTP-старте не создаются и не применяются. Для нескольких копий используйте разные имена и свободные порты.

`docker compose down` сохраняет данные. Удаление тома стирает БД и не входит в обычный запуск. Тестовую БД SQL-скрипт создаёт только для нового тома. На существующей БД сначала проверяются принадлежность данных и ревизии. Публичное развёртывание, роли, сессии и резервное копирование требуют отдельной подготовки по требованиям продукта.
