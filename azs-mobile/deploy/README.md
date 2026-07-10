# Деплой на azs-classifier.ru

## Шаг 1 — Купи VPS

Рекомендуем **reg.ru** (домен уже там, удобно) или **Timeweb** (дешевле).

| Хостинг | Тариф | Цена | Ссылка |
|---|---|---|---|
| **reg.ru** | VPS-1 (1 vCPU, 1GB RAM, 20GB SSD) | ~280 руб/мес | https://www.reg.ru/vps/ |
| **Timeweb** | Cloud-1 (1 vCPU, 1GB RAM, 15GB SSD) | ~189 руб/мес | https://timeweb.cloud/ |

> Операционная система: **Ubuntu 22.04 LTS**

---

## Шаг 2 — Настрой DNS на reg.ru

1. Зайди в **reg.ru → Домены → azs-classifier.ru → DNS-записи**
2. Добавь A-запись:

```
Тип:  A
Имя:  @
IP:   <IP твоего VPS>
TTL:  3600
```

И для www:
```
Тип:  A
Имя:  www
IP:   <IP твоего VPS>
TTL:  3600
```

> DNS применяется за 1-24 часа. Проверить: `dig azs-classifier.ru +short`

---

## Шаг 3 — Настрой сервер (один раз)

```bash
# Скопируй скрипт на сервер
scp deploy/setup-server.sh root@<IP>:/root/

# Зайди по SSH
ssh root@<IP>

# Запусти настройку (занимает ~5 минут)
bash setup-server.sh
```

Что делает скрипт:
- Устанавливает nginx, Python 3.11, certbot
- Создаёт системного пользователя `azs`
- Настраивает nginx с SSL (Let's Encrypt)
- Создаёт systemd-сервис `azs-api` для FastAPI
- Включает UFW firewall + fail2ban

---

## Шаг 4 — Загрузи env на сервер

```bash
# Скопируй безопасный шаблон
scp deploy/env.production.example root@<IP>:/opt/azs/.env

# Зайди на сервер и отредактируй
ssh root@<IP>
nano /opt/azs/.env   # вставь реальный SMTP_PASSWORD
chown azs:azs /opt/azs/.env
chmod 640 /opt/azs/.env
```

---

## Шаг 5 — Деплой приложения

```bash
# С локального Mac:
bash deploy/deploy.sh <IP>
```

Скрипт автоматически:
- Собирает фронтенд (`npm run build`)
- Синхронизирует файлы через rsync
- Устанавливает Python-зависимости
- Перезапускает FastAPI
- Проверяет что сайт отвечает

**Повторный деплой** (IP уже сохранён):
```bash
bash deploy/deploy.sh
```

---

## Управление на сервере

```bash
# Статус
systemctl status azs-api

# Логи API
tail -f /opt/azs/logs/api.log

# Перезапуск
systemctl restart azs-api

# Логи nginx
tail -f /var/log/nginx/azs.access.log
```

---

## Обновление данных (stations.json)

```bash
# Подготовь данные локально
npm run prepare-data

# Загрузи на сервер
scp azs-mobile/data/stations.json root@<IP>:/opt/azs/data/stations.json
ssh root@<IP> "chown azs:azs /opt/azs/data/stations.json && systemctl restart azs-api"
```

---

## Обновление KPI из DWH без доступа сервера к VPN

Сайт читает KPI из локальной витрины `/opt/azs/data/kpi_metrics.sqlite3`. Сервер не подключается к корпоративному VPN и не хранит DWH-пароли.

Один раз на сервере:

```bash
# /opt/azs/.env
APP_DATA_MODE=local
KPI_DATA_MODE=local
KPI_IMPORT_TOKEN=<длинный случайный токен>

systemctl restart azs-api
```

На корпоративной машине:

```bash
# .env.local
APP_PUBLIC_URL=https://azs-classifier.ru
KPI_IMPORT_TOKEN=<тот же токен>

DWH_DB_HOST=<host>
DWH_DB_PORT=5432
DWH_DB_NAME=<database>
DWH_DB_USER=<readonly_user>
DWH_DB_PASSWORD=<password>
DWH_MIN_DATE=2024-01-01
```

Далее:

1. Вручную подключи CheckPoint VPN через MobilePass+.
2. Заполни реальный SQL в `backend/sql/dwh_kpi_daily_export.sql`.
3. Проверь выгрузку без отправки:

```bash
npm run sync-kpi -- --period 2026-07 --dry-run
```

4. Отправь агрегаты на сайт:

```bash
npm run sync-kpi -- --period 2026-07
```

Для первичной загрузки всей истории с 1 января 2024 года:

```bash
npm run sync-kpi -- --from-period 2024-01
```

Для текущего месяца параметр `--period` можно не указывать.

---

## Проверка

После деплоя открой:
- https://azs-classifier.ru — приложение
- https://azs-classifier.ru/api/health — статус API
