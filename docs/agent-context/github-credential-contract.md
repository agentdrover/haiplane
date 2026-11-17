# GitHub credential boundary (#2355)

Это внутренняя основа для browser flows #2356/#2357 и Git/PR/CI wiring #2358.
Публичных маршрутов, смены режима доступа и рабочего подключения GitHub пока
нет. Существующие `proc.git_env` и forge readers сохраняют прежний контракт
до #2358. Частная изоляция исполнения относится к #2354.

## Входы и полномочия

`GithubConnections.credential(identity, context, delegation)` принимает
неизменяемый `CredentialContext`: project, binding, repository, actor, task,
purpose, auth_epoch, permissions. Для write добавляются refs, expected SHA
и protected path policy. Типы всех полей проверяются для каждого purpose;
изменяемые элементы не допускаются. Это контекст решения, а не enforcement Git-команд:
диспетчер #2358 обязан проверить конкретную команду, refs, SHA и защищённые
пути перед запуском. Токен GitHub сам не ограничивает refs.

Actor берётся из аутентифицированного `TokenIdentity`; решение проектного
доступа делает `project_access.decide`. Для агента требуется его проектный
доступ или проверенная task delegation. Владелец GitHub-link остаётся человеком.
Приглашённый участник использует проектное полномочие, а не получает секрет
владельца. Binding #2350 имеет primary key project_id.

Три плоскости независимы: App ceiling, installation permissions, project
capability. Purpose ограничивает запрошенные permissions; каждый из трёх
grant должен их разрешать. Отказ расширения не меняет прежний grant/epoch.
Успешная замена grant увеличивает auth_epoch. Версии grant, App metadata,
App material и user material входят в cache key вместе со всем контекстом.

До выдачи, включая cache hit, и после сетевого ожидания проверяются активные
Hub records и свежие GitHub owner/admin/repository/installation proof.
Внешний отказ и невозможность получить доказательство не дают credential.
Изменение доступа в ходе запроса блокирует публикацию lease.
Срок токена повторно проверяется после завершающего network proof и перед
публикацией ciphertext под DB lock; недостаточный остаток даёт `credential_expired`.
Отзыв уже выданного внешнего токена и остановка процесса требуют #2359/#2354.

`configure_app`, `set_user_credentials`, `set_grant` — внутренние trusted
writers. Они не являются CRUD API. Будущие browser callbacks обязаны до их
вызова проверить actor, state/expiry, GitHub identity и admin authorization.
Секреты не передаются через параметры CLI, task feed, origin или URL.

## Хранение и переходы

Новая add-only migration создаёт `github_credential_slots`,
`github_app_credential_configs`, `github_user_credential_apps`,
`github_project_credential_grants`. В slots сохраняются только AES-256-GCM
ciphertext, key id, nonce и служебные поля. AAD связывает ciphertext с
slot/kind; перестановка между записями не проходит аутентификацию.
Отдельный JSON keyring имеет каталог 0700 и файл 0600 текущего service user;
symlink последнего каталога/файла, hardlink, неверные owner/mode отвергаются.
При создании каждого каталога выполняется fsync его родителя. Перед каждой
записью keyring повторно синхронизируется вся цепочка каталогов, включая
существующие: наличие каталога после неудачного fsync не доказывает durability.
Перед готовностью ключа fsync выполняется для файла и конечного каталога.
Keyring не входит в SQLite backup. Доступ к обоим — полномочие service user.

Каждое изменение slots идёт под `BEGIN IMMEDIATE`; сеть выполняется после
коммита reservation. `active → refreshing` назначает lease с дедлайном.
CAS при публикации проверяет lease и срок. Два экземпляра broker используют
общую SQLite reservation. Ошибка, отмена, неизвестный исход или просроченный
lease переводят OAuth slot в `blocked`: повтор одноразового refresh token
запрещён, нужна проверенная повторная авторизация. Writer восстановления
заменяет material и сбрасывает blocked.

Installation mint не потребляет одноразовый refresh token. После неизвестного
исхода slot также блокируется; новый явный запрос допускает mint после
пятисекундного cooldown. Ожидающие вызовы не запускают OAuth replay.

Ротация сначала атомарно пишет keyring с новым и старыми ключами, затем
перешифровывает все slots одной DB transaction. После commit повторно
берётся DB lock и проверяется актуальный key id всех ciphertext; только
затем старые ключи удаляются из keyring. Сбой до commit оставляет старые
ключи; параллельный rotator не затирает более новый keyring. После 16
неоконченных ротаций операция требует восстановления, а не потери ключей.

## Установка и восстановление

Docker entrypoint запускает `scripts/github_credentials_bootstrap.py` после
demo seed и до запуска Hub. Compose хранит `/keys` в отдельном persistent
volume `github_keys`; SQLite остаётся в `/data`. Обычный restart не меняет
ключ. Для native установки оператор один раз запускает от service user:

```bash
uv run python scripts/github_credentials_bootstrap.py --database /path/to/hub.db
```

Путь native по умолчанию: `~/.config/haiplane-hub/github/credentials.keys`;
настройка `HAIPLANE_GITHUB_CREDENTIAL_KEY_FILE` задаёт другой отдельный путь.
Автоматическое создание допустимо только для vault без credential rows.
Восстановленная непустая БД без ключа новый ключ не получает.

Оператор хранит защищённую резервную копию keyring отдельно от DB backup.
Перед backup/restore или ротацией нужно исключить конкурирующие записи
(остановить writers) и сохранить согласованные DB/keyring. Обычный DB-only
backup не гарантирует восстановление credentials. После DB-only restore Hub
может стартовать, bootstrap сообщает `recovery_required`, broker закрыт.
Оператор восстанавливает соответствующий keyring с owner/mode service user
и повторяет bootstrap. Утрата ключа требует новой App/user авторизации
проверенными flows; этот модуль не удаляет slots и не делает reset автоматически.

Ротация существующего keyring:

```bash
uv run python scripts/github_credentials_bootstrap.py --database /path/to/hub.db --rotate
```

Вывод содержит только статус/фиксированную причину; значения ключей отсутствуют.
`--rotate` возвращает 2 при ошибке. Обычный bootstrap возвращает 0 при
`recovery_required`, чтобы не блокировать историю/UI после восстановления.

## Внешняя граница и наблюдение

`GithubTokenProvider` работает только с GitHub.com по HTTPS, без env proxy и
redirects. App JWT использует RS256; mint явно передаёт ровно один repository
id и минимальные permissions. OAuth refresh сохраняет новую пару access и
refresh как один ciphertext. Неполная pagination — неизвестный результат,
а не доказательство отсутствия репозитория.

`Secret` скрыт в repr/str; `credential()` обрывает raw exception context.
Проекции `status()` содержат allowlist полей без путей и credentials.
`configured` означает наличие metadata, `github_authorization=unchecked`
прямо отделяет это от успешной GitHub проверки. Для ключа возвращается
`recovery_required`; причины provider failures фиксированы закрытым словарём.

`command_environment` предназначен только для доверенного gh subprocess:
частный HOME/GH_CONFIG_DIR, один scoped GH_TOKEN, allowlist locale/PATH,
отключены prompts и глобальный Git config. Он не меняет `os.environ`.
Нельзя отдавать это окружение проектному коду или агентской shell-сессии.
Безопасный Git credential transport и process isolation подключают #2358/#2354.

## Проверки и граница свидетельств

Точные AC refs — три теста в `tests/test_github_credentials.py`:
`test_parallel_scoped_tokens_refresh_without_context_leak`,
`test_secret_values_absent_from_projections_logs_and_argv`,
`test_missing_key_and_rotation_fail_closed`. Дополнительные security tests
проверяют delegation, cache partition, revoke-during-mint, AAD swap, неверные
ключи/mode/symlink, crash/concurrent rotation, refresh cancellation/expired
lease, failed cleanup и идемпотентность installer.

`tests/test_github_token_provider.py` проверяет настоящий HTTP adapter через
MockTransport с синтетическими токенами и RSA key. Эти проверки не являются
доказательством live GitHub подключения, Docker запуска или production rollout.
REST/CLI/MCP не меняются: публичные browser flows — отдельные задачи.

Первичные контракты:

- [AES-GCM и associated data](https://cryptography.io/en/stable/hazmat/primitives/aead/).
- [Installation access token и сужение permissions/repositories](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).
- [Одноразовое обновление user access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens).
