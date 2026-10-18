# Снимок служб прода

Конфигурация служб хаба на проде без секретов (задача #1849, E0 спайка #1792).
Нужна, чтобы стенд (k8) и новый хост собирались таким же окружением, как прод, а не по примеру
`haiplane-hub.service` из корня репозитория.

## Источник и снятие

- Дата снятия: 18.10.2026. Снимал владелец хаба, версия скрипта v4 (`.claude/e0-capture.py`, вне репозитория).
- Снятие записано в `CAPTURE.txt` без правок; sha256 файла закреплён в `tests/test_prod_unit_snapshot.py`
  (и в карточке задачи #1849). В README хеш не повторён: сплошная последовательность от 24 символов запрещена.
- Команда снятия (rc 3 при неполноте, замена файла только при rc 0):

```
(umask 077; ssh agenthai 'sudo -n python3 -I -' < .claude/e0-capture.py > .claude/e0-capture.v4.tmp && mv .claude/e0-capture.v4.tmp .claude/e0-capture.v4.txt)
```

- Скрипт печатает только значения, прошедшие проверку своего типа (список, число, путь, модель, URL без
  учётных данных, команда из закрытой схемы, JSON-параметры из закрытого списка ключей). Всё прочее заменено
  на `<скрыто>`. Комментарии юнитов не печатаются. Значения сверх снятия сюда не добавлены.

## Неполнота снятия (rc 3, раздел НЕПОЛНО)

Снятие закончилось с rc 3. Последний раздел `CAPTURE.txt` говорит: разбор `/etc/haiplane-review-runner.env`
остановлен на значении `HAIPLANE_REVIEW_RUNNER_ARGV`. Что известно о файле: две переменные
(`HAIPLANE_REVIEW_RUNNER_SPOOL_DIR`, `HAIPLANE_REVIEW_RUNNER_SCRATCH_DIR`) и имя `HAIPLANE_REVIEW_RUNNER_ARGV`.
Значение этой переменной и все переменные файла после неё: **неизвестно**. Файл `etc/haiplane-review-runner.env`
в снимке повторяет раздел как есть: строка `HAIPLANE_REVIEW_RUNNER_ARGV` без знака равенства.

## Правило границ раздела

Файл снимка это файл хоста в том виде, как его напечатало снятие (после замены значений на `<скрыто>`
и без комментариев). Раздел `CAPTURE.txt` начинается строкой `--- файл <путь>` или
`--- EnvironmentFile <путь> (ignore_errors=...)` и кончается на первой строке, которая начинается с
`--- `, `##########` или `TimersCalendar=`, либо совпадает с шаблоном строки проверки каталога
`/путь: mode=0o... uid=... gid=...`. Эту строку тоже не включают. Строка заголовка в файл не входит.
Строки делятся только по LF.

Строки `TimersCalendar=...` (после файла таймера), `/путь: mode=... uid=... gid=...` (после файла tmpfiles)
и `--- PATH службы: ...` печатает сам скрипт снятия; в файлах хоста их нет, они остаются только в `CAPTURE.txt`.

Хвост файла: юниты, таймеры и drop-in-ы скрипт печатает одним `print`, текст файла уже кончается LF,
поэтому в разделе после него идёт лишняя пустая строка. Файл хоста равен телу раздела без последнего LF.
tmpfiles и EnvironmentFile скрипт печатает построчно: файл равен телу раздела целиком.
Правило проверяет `tests/test_prod_unit_snapshot.py` (`parse_sections`, `host_file_text`).

Разделы `haiplane-hub.service` и `openclaw-hub.service` в `CAPTURE.txt` описывают один и тот же юнит
и совпадают побайтно; файл один (см. ниже).

## Файлы снимка

Порядок drop-in-ов совпадает с `DropInPaths` (systemd читает их по имени; последний выигрывает). Состояние
юнита и таймера: `UnitFileState/ActiveState` из `CAPTURE.txt`.

| Путь в снимке | Путь на хосте | Состояние |
|---|---|---|
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service` | `/etc/systemd/system/openclaw-hub.service` | enabled/active |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/30-auto-approve.conf` | `/etc/systemd/system/openclaw-hub.service.d/30-auto-approve.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/50-local-review.conf` | `/etc/systemd/system/openclaw-hub.service.d/50-local-review.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/50-steward.conf` | `/etc/systemd/system/openclaw-hub.service.d/50-steward.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/52-steward-glm-advisor.conf` | `/etc/systemd/system/openclaw-hub.service.d/52-steward-glm-advisor.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/53-review-random-deep.conf` | `/etc/systemd/system/openclaw-hub.service.d/53-review-random-deep.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/executor.conf` | `/etc/systemd/system/openclaw-hub.service.d/executor.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/review-snapshot.conf` | `/etc/systemd/system/openclaw-hub.service.d/review-snapshot.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/sdd.conf` | `/etc/systemd/system/openclaw-hub.service.d/sdd.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/worktree.conf` | `/etc/systemd/system/openclaw-hub.service.d/worktree.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/zz-csrf.conf` | `/etc/systemd/system/openclaw-hub.service.d/zz-csrf.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/openclaw-hub.service.d/zz-models.conf` | `/etc/systemd/system/openclaw-hub.service.d/zz-models.conf` | drop-in openclaw-hub.service |
| `deploy/systemd/prod/etc/systemd/system/haiplane-review-runner.service` | `/etc/systemd/system/haiplane-review-runner.service` | enabled/active |
| `deploy/systemd/prod/etc/systemd/system/haiplane-hub-backup.service` | `/etc/systemd/system/haiplane-hub-backup.service` | static/inactive |
| `deploy/systemd/prod/etc/systemd/system/haiplane-hub-backup.timer` | `/etc/systemd/system/haiplane-hub-backup.timer` | enabled/active |
| `deploy/systemd/prod/etc/systemd/system/haiplane-snapshot-daily.service` | `/etc/systemd/system/haiplane-snapshot-daily.service` | static/inactive |
| `deploy/systemd/prod/etc/systemd/system/haiplane-snapshot-daily.timer` | `/etc/systemd/system/haiplane-snapshot-daily.timer` | enabled/active |
| `deploy/systemd/prod/etc/systemd/system/haiplane-snapshot-hourly.service` | `/etc/systemd/system/haiplane-snapshot-hourly.service` | static/inactive |
| `deploy/systemd/prod/etc/systemd/system/haiplane-snapshot-hourly.timer` | `/etc/systemd/system/haiplane-snapshot-hourly.timer` | enabled/active |
| `deploy/systemd/prod/etc/openclaw-hub/openclaw-hub.env` | `/etc/openclaw-hub/openclaw-hub.env` | EnvironmentFile |
| `deploy/systemd/prod/etc/haiplane-review-runner.env` | `/etc/haiplane-review-runner.env` | EnvironmentFile |
| `deploy/systemd/prod/etc/tmpfiles.d/haiplane-review-spool.conf` | `/etc/tmpfiles.d/haiplane-review-spool.conf` | tmpfiles.d |

### Псевдоним haiplane-hub.service

Отдельного файла `haiplane-hub.service` на проде нет: `/etc/systemd/system/haiplane-hub.service` это ссылка на
`/etc/systemd/system/openclaw-hub.service`; оба имени дают `Id=openclaw-hub.service`. Копия не создана дважды.
Ссылка `/opt/haiplane-hub` ведёт на `/opt/openclaw-hub`. Каталогов `/etc/haiplane-hub`, `/var/lib/haiplane-hub`,
`/var/log/haiplane-hub` на проде нет; есть `/etc/openclaw-hub`, `/var/lib/openclaw-hub`, `/var/log/openclaw-hub`.

### Только в CAPTURE.txt

Правила `/etc/sudoers.d/haiplane-review` и `/etc/sudoers.d/haiplane-advisor` лежат только в `CAPTURE.txt`
(разделы с заголовком `--- файл`), отдельных файлов нет. Первое правило видно, второе скрыто снятием
(строка `<правило скрыто: не прошло проверку>`): его текст неизвестен.

### secrets.env: перечень имён (файла нет)

Источник `/etc/openclaw-hub/secrets.env` подключён к `openclaw-hub.service` (`ignore_errors=yes`). Файла-копии
в снимке нет, значения скрыты снятием, ниже только имена в порядке файла (тест сверяет их с `CAPTURE.txt`):

- `HAIPLANE_GITVERSE_TOKEN`
- `CURSOR_API_KEY`
- `CURSOR_REVIEWER_HUB_TOKEN`
- `HAIPLANE_STEWARD_HUB_TOKEN`
- `HAIPLANE_LOCAL_REVIEWER_HUB_TOKEN`

## Переменные

Колонка «Источник» это файл из таблицы выше. Значения как в снимке; `<скрыто>` не раскрыто. Если имя задано
в нескольких drop-in-ах, действует последний по порядку `DropInPaths`. Переменные с неизвестным значением
(`HAIPLANE_REVIEW_RUNNER_ARGV` и всё после неё в `haiplane-review-runner.env`) в таблицу не входят.
`PATH` не задан ни в одном проверенном источнике (раздел «Расхождения»).

| Имя | Источник | Значение | Какой источник действует |
|---|---|---|---|
| `HAIPLANE_AUTO_APPROVE_MAX_CLASS` | `30-auto-approve.conf` | `r1` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_CMD` | `50-local-review.conf` | `<скрыто>` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_SANDBOX` | `50-local-review.conf` | `<скрыто>` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_SCRATCH_DIR` | `50-local-review.conf` | `/var/lib/haiplane-review/scratch` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_TIMEOUT_SEC` | `50-local-review.conf` | `1800` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_TOKEN_CEILING` | `50-local-review.conf` | `2000000` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_TRANSPORT` | `50-local-review.conf` | `runner` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_SPOOL_DIR` | `50-local-review.conf` | `/var/lib/openclaw-hub/review-spool` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_MODEL` | `50-local-review.conf` | `deepseek-v4-pro` | действует (единственный источник) |
| `HAIPLANE_STEWARD_MODE` | `50-steward.conf` | `shadow` | действует (единственный источник) |
| `HAIPLANE_STEWARD_ADVISOR_LOCAL_MODEL` | `52-steward-glm-advisor.conf` | `glm-5.3` | действует (единственный источник) |
| `HAIPLANE_STEWARD_ADVISOR_MODELS` | `52-steward-glm-advisor.conf` | `glm-5.3,grok-4.6,composer-2.5` | действует (единственный источник) |
| `HAIPLANE_REVIEW_RANDOM_DEEP_SHARE` | `53-review-random-deep.conf` | `0.2` | действует (единственный источник) |
| `HAIPLANE_EXECUTOR_MODEL` | `executor.conf` | `gemini-3.8-flash` | перекрыт: zz-models.conf |
| `HAIPLANE_EXECUTOR_TOKEN_CEILING` | `executor.conf` | `40000000` | действует (единственный источник) |
| `HAIPLANE_EXECUTOR_CENTS_CEILING` | `executor.conf` | `500` | действует (единственный источник) |
| `HAIPLANE_LOCAL_REVIEW_SNAPSHOT` | `review-snapshot.conf` | `1` | действует (единственный источник) |
| `HAIPLANE_SDD_AC_LOCATOR` | `sdd.conf` | `require` | действует (единственный источник) |
| `HAIPLANE_WORKTREE_PER_TASK` | `worktree.conf` | `1` | действует (единственный источник) |
| `HAIPLANE_CSRF_MODE` | `zz-csrf.conf` | `require` | действует (единственный источник) |
| `HAIPLANE_EXECUTOR_MODEL` | `zz-models.conf` | `grok-4.7` | действует (позже по DropInPaths) |
| `HAIPLANE_EXECUTOR_MODEL_PARAMS` | `zz-models.conf` | `<скрыто>` | действует (единственный источник) |
| `HAIPLANE_HUB_HOST` | `openclaw-hub.env` | `127.0.0.1` | действует (единственный источник) |
| `HAIPLANE_HUB_PORT` | `openclaw-hub.env` | `8080` | действует (единственный источник) |
| `HAIPLANE_HUB_HOME` | `openclaw-hub.env` | `/var/lib/openclaw-hub` | действует (единственный источник) |
| `HAIPLANE_HUB_DB` | `openclaw-hub.env` | `/var/lib/openclaw-hub/hub.db` | действует (единственный источник) |
| `HAIPLANE_TRANSCRIPTS_DIR` | `openclaw-hub.env` | `/var/lib/openclaw-hub/transcripts` | действует (единственный источник) |
| `HAIPLANE_HUB_COOKIE_SECURE` | `openclaw-hub.env` | `1` | действует (единственный источник) |
| `HAIPLANE_HUB_TOKENS` | `openclaw-hub.env` | `<скрыто>` | действует (единственный источник) |
| `HAIPLANE_WORKSPACE_REPO` | `openclaw-hub.env` | `/var/lib/openclaw-hub/workspaces/_default` | действует (единственный источник) |
| `HAIPLANE_HUB_URL` | `openclaw-hub.env` | `https://agenthai.ru` | действует (единственный источник) |
| `HAIPLANE_HUB_REPO` | `openclaw-hub.env` | `agentdrover/haiplane` | действует (единственный источник) |
| `HAIPLANE_HUB_ALLOWED_HOSTS` | `openclaw-hub.env` | `agenthai.ru,www.agenthai.ru,hub.agenthai.ru,haiplane.com,www.haiplane.com,localhost,127.0.0.1` | действует (единственный источник) |
| `HAIPLANE_REVIEW_RUNNER_SPOOL_DIR` | `haiplane-review-runner.env` | `/var/lib/openclaw-hub/review-spool` | действует (единственный источник) |
| `HAIPLANE_REVIEW_RUNNER_SCRATCH_DIR` | `haiplane-review-runner.env` | `/var/lib/haiplane-review/scratch` | действует (единственный источник) |

## Расхождения

У каждого пункта названы действие и исполнитель. Изменения прода, `deploy/remote-deploy.sh`, примера юнита
и руководств в этой задаче не делались: это предложения владельцу. Задач под них пока нет; завести их или
отклонить решает владелец.

| № | Расхождение | Действие | Исполнитель |
|---|---|---|---|
| 1 | Имена. Прод: `openclaw-hub.service`, `/opt/openclaw-hub`, `/etc/openclaw-hub`. Репозиторий и `deploy/remote-deploy.sh` (строки 11-14, 52, 355, 373) зовут `haiplane-hub` и `/opt/haiplane-hub`. Работает через псевдоним юнита и ссылку `/opt/haiplane-hub`. | Не менять прод. При сборке стенда и нового хоста воспроизвести псевдоним и ссылку либо переименовать пути единым решением. Решить, когда переименовывать прод. | Владелец (решение); задачи нет |
| 2 | `PATH` не задан в `Environment=` ни одной службы и в проверенных EnvironmentFile; в `haiplane-review-runner.env` файл прочитан неполностью (раздел «Неполнота»). Итог: в проверенных источниках не задан, вероятно умолчание systemd, точно неизвестно. | Не полагаться на умолчание при сборке стенда. Снять фактический `PATH` процесса и вписать его в drop-in после решения владельца. | Владелец (решение); снятие процесса и правка drop-in-а: задачи нет |
| 3 | Дубль `HAIPLANE_EXECUTOR_MODEL`: `executor.conf` даёт `gemini-3.8-flash`, `zz-models.conf` даёт `grok-4.7`. Действует `zz-models.conf` (идёт позже в `DropInPaths`), значение в `executor.conf` мертво. | Убрать строку из `executor.conf` или объединить файлы. Прод не менять без решения. | Владелец (решение); задачи нет |
| 4 | Пример юнита `haiplane-hub.service` в корне репозитория против прода: пользователь `haiplane` против `openclaw`; запуск `python -m uvicorn hub.app:app` против `/opt/openclaw-hub/venv/bin/haiplane-hub`; нет песочницы против `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `MemoryDenyWriteExecute` и др.; журнал в `hub.log` против `/var/log/openclaw-hub/service.log` и `service.err`; нет `ReadWritePaths`. | Привести пример к снимку либо пометить его как устаревший. | Владелец (решение); задачи нет |
| 5 | `docs/admin-agent-deployment-guide.md`: `ReadWritePaths=/var/lib/haiplane-hub /var/log/haiplane-hub` (строка 215), на проде добавлен `/opt/openclaw-hub`. Резервная копия в руководстве через `cron.daily` (строки 355-369), на проде `haiplane-hub-backup.timer` в 00:30 UTC (команда `/usr/local/sbin/haiplane-hub-backup`), а также `haiplane-snapshot-hourly.timer` (минута 17 каждого часа) и `haiplane-snapshot-daily.timer` (03:47 UTC). | Поправить руководство по снимку. | Владелец (решение); задачи нет |
| 6 | `deploy/TAILSCALE.md` (строки 86-103): юнит под пользователем `haiplane` из домашнего каталога, `EnvironmentFile=/home/haiplane/.haiplane-hub.env`, без песочницы. `deploy/LOCAL-REVIEW.md` (строки 285-292): `HAIPLANE_LOCAL_REVIEWER_HUB_TOKEN` предписано класть в drop-in; на проде имя в `secrets.env`, а в drop-in его нет. | Пометить `TAILSCALE.md` как руководство для другого типа хоста; поправить рецепт токена в `LOCAL-REVIEW.md`. | Владелец (решение); задачи нет |
| 7 | Шаблоны в `deploy/review-runner/` и `deploy/backup/install.sh` против снимка: в шаблоне службы `User=<пользователь хаба>`, на проде `User=openclaw`; в шаблоне есть директива запрета перезапуска при коде выхода 2 и комментарии, а в снимке эта строка записана как `<скрыто>=2` (имя скрыто последним фильтром снятия); шаблон tmpfiles и таймеры снимков совпадают по смыслу с продом. | Сверять шаблоны со снимком при следующем изменении службы ревью; строку со скрытым именем не домысливать. | Владелец (решение); задачи нет |
| 8 | Правило sudoers `haiplane-advisor` скрыто снятием. `deploy/LOCAL-REVIEW.md` (строка 1384) описывает его для пользователя `haiplane`, хаб на проде работает под `openclaw`; фактический текст правила неизвестен. | Повторить снятие правила с расширенной проверкой или прочитать правило владельцем и записать результат сюда. | Владелец; задачи нет |
| 9 | Неполнота снятия: `haiplane-review-runner.env` разобран только до `HAIPLANE_REVIEW_RUNNER_ARGV` (rc 3). | Повторить снятие после правки скрипта снятия (значение ARGV должно пройти проверку или быть скрыто целиком, а разбор файла идти дальше). | Владелец; задачи нет |
| 10 | Кандидат на уборку: `HAIPLANE_LOCAL_REVIEW_*` в `50-local-review.conf` при `deep_reviewer=cloud` (локальный ревьюер выключен 07.10), хотя `HAIPLANE_LOCAL_REVIEW_SNAPSHOT=1` и транспорт `runner` заданы. | Оставить как есть либо убрать drop-in-ы и остановить `haiplane-review-runner.service`. Решение за владельцем. | Владелец (решение); задачи нет |
