# АЗС Mobile

Мобильный PWA-прототип справочника объектов АЗС: поиск, фильтры, карта-точки, карточка объекта, аналитика и режим установки на экран телефона.

## Что внутри

- `azs-mobile/` — React/Vite приложение.
- `azs-mobile/scripts/prepare_data.py` — локальная подготовка приватного `data/stations.json` из Excel-листа `cls_AZS`.
- `azs-mobile/public/stations.sample.json` — обезличенный пример структуры данных.
- `TZ_mobile_AZS.md` — исходное ТЗ.

Реальные файлы данных (`data.csv`, `cls_2026_05_AZS.xlsx`, `azs-mobile/data/stations.json`) не коммитятся, потому что могут содержать адреса, ФИО, телефоны и внутренние признаки объектов.

## Запуск

Одной командой из корня проекта:

```bash
./start_app.command
```

Или из папки приложения:

```bash
cd azs-mobile
npm install
npm start
```

После установки зависимостей одна команда `npm start` соберёт PWA, поднимет backend API и frontend preview. Остановить оба сервера можно через `Ctrl+C`.

Раздельный запуск, если нужен:

```bash
cd azs-mobile
npm run backend
npm run dev
```

Открыть локально:

```text
http://localhost:5174/
```

Для просмотра на телефоне открой `Network`-адрес из вывода Vite, например:

```text
http://192.168.1.112:5174/
```

## Корпоративный доступ

Приложение закрыто авторизацией. Регистрация и вход доступны только для корпоративных email:

- домены из `AUTH_ALLOWED_EMAIL_DOMAINS` (по умолчанию `lukoil.com,lukoil.ru,licard.com,spb.lukoil.com,ynp.lukoil.com`);
- точные адреса из `AUTH_ALLOWED_EMAILS`;
- домены или адреса из локального файла `azs-mobile/data/auth_allowlist.json`.

Пример локального файла:

```bash
cp azs-mobile/data/auth_allowlist.example.json azs-mobile/data/auth_allowlist.json
```

При первой авторизации приложение отправляет одноразовый код на корпоративную почту. Без подтверждения email сессия не создаётся. Внешние email получают `403`, приватные маршруты и API без сессии получают `401`. Сессия хранится в httpOnly cookie, локальная база пользователей и сессий находится в `azs-mobile/data/auth.sqlite3` и не коммитится.

Чтобы включить реальную отправку писем, добавь в `azs-mobile/.env.local` SMTP-настройки:

```text
AUTH_EMAIL_DEV_MODE=false
APP_PUBLIC_URL=https://адрес-приложения
SMTP_HOST=smtp.example.ru
SMTP_PORT=587
SMTP_USERNAME=почтовый_логин
SMTP_PASSWORD=пароль_или_app_password
SMTP_FROM_EMAIL=no-reply@example.ru
SMTP_FROM_NAME=Классификатор АЗС
SMTP_USE_TLS=true
SMTP_USE_SSL=false
```

Для локальной разработки можно оставить `AUTH_EMAIL_DEV_MODE=true`: код подтверждения будет возвращаться в dev-ответе API и печататься в лог backend.

## Подключение Яндекс Карты

Приложение использует Yandex Maps JavaScript API 2.1. Ключ не хранится в коде: создай локальный файл:

```bash
cd azs-mobile
cp .env.example .env.local
```

Впиши ключ:

```text
VITE_YANDEX_MAPS_API_KEY=твой_ключ
```

После изменения `.env.local` перезапусти сервер:

```bash
npm start
```

В кабинете Яндекса для ключа включи ограничение по HTTP Referer. Для локальной разработки добавь адреса вроде `http://localhost:5174/*` и сетевой адрес Vite, если открываешь приложение с телефона.

## Подготовка реальных данных

Положи файл `cls_2026_05_AZS.xlsx` в корень проекта рядом с `TZ_mobile_AZS.md`, затем:

```bash
cd azs-mobile
npm run prepare-data
```

Скрипт создаст локальный файл:

```text
azs-mobile/data/stations.json
```

Этот файл используется backend API `/api/stations`, но не публикуется в GitHub и не раздаётся как статический файл.

На macOS можно запустить двойным кликом:

```text
azs-mobile/scripts/update_data.command
```

Он выполнит ту же подготовку данных и обновит `data/stations.json`.

## Подготовка рекомендаций по персоналу

Положи месячные Excel-файлы с листом `По дням` в папку:

```text
azs-mobile/data/staff/
```

Скрипт берёт месяц и год из имени файла, например `04.2026.xlsx`, и считает рекомендации:

- дневная смена: `Совокупная сумма часов для дневной смены / 12`;
- ночная смена: `Совокупная сумма часов для ночной смены / 12`.

```bash
cd azs-mobile
npm run prepare-staff
```

Скрипт создаст локальный файл:

```text
azs-mobile/data/staff_recommendations.json
```

Backend использует этот приватный файл для `/api/stations/{ksss}/staff?period=YYYY-MM`; если данных за период нет, в mock-режиме останутся демо-рекомендации.

## Offline/PWA

Приложение кэширует оболочку PWA и обезличенный `stations.sample.json`. Приватные `/api/*` и рабочий реестр не кэшируются service worker после logout, чтобы данные не оставались доступными внешним пользователям.

## PWA на телефоне

На iPhone:

1. Открыть приложение в Safari.
2. Нажать `Поделиться`.
3. Выбрать `На экран "Домой"`.
4. Запускать приложение с иконки `АЗС`.

Так приложение откроется в standalone-режиме без адресной строки браузера.
