# Политика безопасности

## Как сообщить об уязвимости

Не открывайте публичный issue. Воспользуйтесь приватным каналом GitHub:
[**Report a vulnerability**](https://github.com/agentdrover/haiplane/security/advisories/new)
на вкладке Security. Черновик виден только вам и мейнтейнеру, пока не будет
опубликован.

Проект поддерживается одним человеком. Ответ обычно в течение недели; если
через две недели ответа нет — эскалируйте, открыв публичный issue *со ссылкой
на факт молчания, но без деталей уязвимости*.

Что помогает разобрать репорт быстрее:

- версия или коммит, на котором воспроизводится;
- конфигурация: `HAIPLANE_HUB_HOST`, заданы ли токены, за каким прокси;
- минимальные шаги воспроизведения — запрос, ожидаемый и фактический ответ.

## Что считается уязвимостью, а что настройкой

Хаб по умолчанию слушает `127.0.0.1`, а старт с не-loopback-адресом при пустой
авторизации отвергается на старте (`validate_network_auth()` в
[hub/config.py](hub/config.py)). Поэтому:

- **уязвимость** — обход авторизации, повышение роли, чтение или запись чужих
  задач, XSS/инъекция, утечка токена в логи или ответы API;
- **не уязвимость** — инстанс, намеренно выставленный в сеть с
  `HAIPLANE_HUB_ALLOW_UNAUTHENTICATED_NETWORK=1`, и последствия того, что вы
  раздали токен с ролью `admin`. Это задокументированные решения оператора.

Граница ролей описана в [docs/security-remediation-recommendations.md](docs/security-remediation-recommendations.md).

## Поддерживаемые версии

Исправления выходят только для `main`. Отдельных веток поддержки нет: релиз —
это то, что доехало в `main`, и обновление сводится к выкату свежего `main`.

---

## Reporting in English

Please do not open a public issue. Use GitHub's private channel —
[**Report a vulnerability**](https://github.com/agentdrover/haiplane/security/advisories/new)
under the Security tab. Include the commit you reproduced on, your
`HAIPLANE_HUB_HOST` and token configuration, and minimal reproduction steps.

This is a solo-maintained project; expect a reply within a week. Fixes land on
`main` only — there are no maintenance branches.

Auth bypass, role escalation, cross-task read/write, injection, and token
leakage are vulnerabilities. An instance you deliberately exposed to the
network with `HAIPLANE_HUB_ALLOW_UNAUTHENTICATED_NETWORK=1` is not.
