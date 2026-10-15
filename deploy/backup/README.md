# Копии прода вне сервера (#1707)

Серверная часть: на хабе раз в час появляется зашифрованный снимок базы, раз в сутки —
зашифрованный архив настроек. Забирает их домашняя сеть (k8, затем NAS) по ssh одной
командой. Закрытый ключ age хранится только дома; на сервере лежит открытый, поэтому
сервер сам копии прочитать не может.

| Файл | Что делает |
|---|---|
| `hub-snapshot.py` | `hourly`: sqlite backup API, проверка, шифрование age. `daily`: tar по allowlist, шифрование age потоком |
| `snapshot-serve.sh` | ForceCommand пользователя `haiplane-backup`: отдаёт самую новую копию, больше ничего |
| `install.sh` | установка владельцем одной командой, идемпотентна |

## Установка (владелец, sudo)

Ключи передаются аргументами или файлами. В репозитории ключей нет.

```sh
sudo sh deploy/backup/install.sh \
  --ssh-pubkey-file /path/to/k8-backup.pub \
  --age-recipient-file /path/to/recipient.txt
# или значениями:
sudo sh deploy/backup/install.sh \
  --ssh-pubkey "ssh-ed25519 AAAA...ПУБЛИЧНЫЙ_КЛЮЧ... haiplane-backup@k8" \
  --age-recipient age1...ОТКРЫТЫЙ_КЛЮЧ_ПОЛУЧАТЕЛЯ...
```

Нужны `age` (`apt install age`) и `openssh-server`. Повторный запуск без изменений печатает
`no changes`. Без ключей повторный запуск оставляет установленные. Флаг `--no-services` не
трогает systemd и не перезагружает sshd (для узла без systemd).

Что появляется:

- отдельный системный пользователь `haiplane-backup` (системный `backup`, uid 34, не трогается): shell `/bin/sh`, без пароля (`usermod -p '*'`), домашний каталог
  `/var/lib/haiplane-backup-home` и `.ssh` принадлежат root, `authorized_keys` — root:root 0644,
  ключ с опциями `restrict,command="…/snapshot-serve.sh …"`: принудительная команда стоит в самом ключе и
  действует, даже если sshd ещё не перезагружен;
- `/etc/ssh/sshd_config.d/60-haiplane-backup.conf`: `Match User haiplane-backup`, ForceCommand,
  без пароля, TTY, пробросов и туннелей; `sshd -t` до перезагрузки, затем проверка `sshd -T`, затем reload (на каждом запуске, не только при изменении),
  и только после этого появляется ключ:
  если основной `sshd_config` не включает `sshd_config.d`, установка падает;
- `/var/lib/haiplane-backup` (0750 root:haiplane-backup): копии 0640 root:haiplane-backup, `work/` — точка
  монтирования tmpfs 512 МБ только для открытого текста часового снимка;
- `/etc/haiplane-backup/recipient.txt` (открытый ключ age) и `paths.conf` (allowlist daily,
  файл создаётся один раз, дальше его правит владелец);
- скрипты в `/usr/local/lib/haiplane-backup/` (после обновления репозитория запустить
  `install.sh` ещё раз);
- `haiplane-snapshot-hourly.timer` (каждый час в :17, сдвиг до 5 мин) и
  `haiplane-snapshot-daily.timer` (03:47 UTC), оба `Persistent`.

Ночной `haiplane-hub-backup`, predeploy-снимки и `remote-deploy.sh` не затрагиваются.
Копии хранятся локально: 3 часовых и 2 суточных.

## Контракт забора

```
ssh haiplane-backup@HOST snapshot latest       # hub-<UTC>.db.age
ssh haiplane-backup@HOST snapshot daily        # conf-<UTC>.tar.age
ssh haiplane-backup@HOST snapshot latest-meta  # JSON часовой копии
ssh haiplane-backup@HOST snapshot daily-meta   # JSON суточной копии
```

Код возврата: 0 отдано; 3 копии нет; 4 копия старше порога (часовая 90 мин, суточная 26 ч);
2 всё остальное (другая команда, shell, scp, sftp), без вывода.

Meta — одна строка JSON: `name`, `utc`, `bytes`, `sha256` зашифрованного файла, `kind`,
`hub_commit`, `schema_migrations` (у часовой `{count, sha256}` списка имён из таблицы
`_migrations`; у суточной `null`), `manifest_sha256` (только у суточной).

Клиент забирает копию так: **meta, файл, meta**. Если `name` в двух ответах разный, копия
сменилась во время чтения — повторить с начала. Затем сверить `bytes` и `sha256` файла с meta.
Открытый до ротации файл сервер дочитывает целиком.

## Восстановление базы

1. Расшифровать: `age -d -i key.txt hub-<UTC>.db.age > hub.db` (на домашней стороне).
2. Выбрать код хаба: взять коммит из `hub_commit` как подсказку, с какого начать поиск; код
   подходит, когда список миграций `hub/db.py` совпадает с `schema_migrations` (число и sha256
   отсортированного списка имён). `hub_commit` — метка развёрнутого дерева, а не доказательство,
   что процесс работал на этом коде. Зависимости — по `uv.lock` этого коммита.
3. На сервере: остановить хаб и поллер; остановить таймеры
   `haiplane-snapshot-*.timer` и исключить деплой (иначе снимок или выкат пойдёт посреди
   восстановления).
4. Отложить в сторону старые `hub.db`, `hub.db-wal`, `hub.db-shm`, `hub.db-journal`.
5. Положить расшифрованную базу как `hub.db` без файлов `-wal`/`-shm`/`-journal`, вернуть
   владельца и права прежней базы.
6. `sqlite3 hub.db 'PRAGMA integrity_check'` должен ответить `ok`.
7. Запустить хаб и поллер, проверить `/health`; включить таймеры.

## Восстановление настроек

`conf-<UTC>.tar.age` расшифровать и распаковать в отдельный каталог. Внутри пути allowlist
без начального `/` и `MANIFEST.json` (путь, sha256, размер, права каждого файла). Сверить
sha256 с manifest и положить файлы на место вручную. Симлинки наружу allowlist в архив не
попадают (список пропущенных — в manifest).

Пересоздаётся вручную: TLS-сертификаты (перевыпуск certbot, ключей TLS в архиве нет),
ключи агентов, секреты вне allowlist, содержимое необязательных путей, которых в `paths.conf` нет.

## Allowlist (`/etc/haiplane-backup/paths.conf`)

Строки `unit NAME`, `optional-unit NAME`, `required PATH`, `optional PATH`. Для `unit`
берутся файл юнита, его drop-in и `EnvironmentFiles` (по `systemctl show`). Отсутствие
обязательного пути — отказ снимка, в журнал уходит код `required_missing`.

## Журнал

Каждый запуск пишет в journald одну строку: `ok kind=... name=... bytes=...`,
`skipped kind=... reason=lock_busy` или `failed kind=... reason=<код>`. Коды: `enospc`,
`mid_deploy`, `age`, `age_missing`, `no_recipient`, `no_deploy_sha`, `no_migrations`,
`integrity`, `work_not_tmpfs`, `required_missing`, `io_error`. Вывода инструментов и путей
секретов в журнале нет. `journalctl -u haiplane-snapshot-hourly`.

## Проверка владельцем после установки

```sh
ssh haiplane-backup@HOST snapshot latest-meta   # JSON с hub_commit и schema_migrations
ssh haiplane-backup@HOST id                     # отказ, rc 2
sftp haiplane-backup@HOST                       # отказ
```

## Откат установки

```sh
sudo systemctl disable --now haiplane-snapshot-hourly.timer haiplane-snapshot-daily.timer
sudo rm /etc/systemd/system/haiplane-snapshot-{hourly,daily}.{service,timer}
sudo rm /etc/ssh/sshd_config.d/60-haiplane-backup.conf
sudo sshd -t && sudo systemctl reload ssh
sudo systemctl daemon-reload
sudo rm -r /usr/local/lib/haiplane-backup /var/lib/haiplane-backup-home
# копии: /var/lib/haiplane-backup; ключ получателя: /etc/haiplane-backup
sudo userdel haiplane-backup
```
