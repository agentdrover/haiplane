# Перепись нестабильных тестов CI за 60 дней (#1685)

Окно: ci.yml в `agentdrover/haiplane`, прогоны, созданные с **2026-08-16** по **2026-10-14** включительно (UTC; 60 суток, заканчивающихся накануне даты начала 15.10.2026: день начала не закрыт, и его новые прогоны меняли бы числа при повторе). История для поиска правок тестов зафиксирована на `develop` = `315f45feaac35bbc4374ffecacd46bfbffc86e7a`.
Данные: `flaky-tests-census-2026-10.json` (строка на пару attempt и nodeid) и `flaky-tests-census-2026-10.manual.json` (ручные входы: локальные повторы с командой и нагрузкой, класс причины, строки вне ci.yml). Сбор: `scripts/flaky_census.py`.

## Как повторить

```
uv run python scripts/flaky_census.py --since 2026-08-16 --until 2026-10-14 \
    --history-ref 315f45feaac35bbc4374ffecacd46bfbffc86e7a \
    --manual docs/agent-context/flaky-tests-census-2026-10.manual.json \
    --out docs/agent-context/flaky-tests-census-2026-10.json
uv run python scripts/flaky_census.py <те же аргументы> --summary    # числа этого документа
uv run python scripts/flaky_census.py <те же аргументы> --markdown   # таблица приложения
```

Нужны `gh` с правом чтения и локальный клон с `origin/develop` (`git fetch origin`). Только `gh api` GET и git; секреты не читаются. Ответы API кэшируются в `--cache-dir` (по умолчанию `.flaky-cache`, в коммит не входит). Повторный запуск при тех же границах и том же `--history-ref` даёт байт-в-байт тот же JSON: проверено `cmp` между файлом в репозитории и холодным прогоном с пустым кэшем (279 вызовов gh, около 7 минут). Окно должно кончаться закрытым днём: при `--until` равном сегодняшнему дню новые прогоны меняют счётчики. Базу релизных PR скрипт выводит из ветки головы (develop → main, main → develop), потому что GitHub со временем очищает `pull_requests` у старых прогонов.

## Охват

| Что | Число |
|---|---|
| Прогоны ci.yml в окне | 3195 (push 1064, pull_request 1607, workflow_dispatch 524) |
| Упавшие attempt (conclusion failure) | 86; из них первым упавшим шагом был Test: 48 |
| Остальные 38 | Secret scan 14, «упавшего шага в pytest-job нет» 12 (упали другие job), Check formatting 3, Static security scan 3, Complexity budget 2, Set up job 2, Lint 1, Type check 1 |
| Строк переписи | 79: 73 из журналов шага Test (48 attempt) и 6 ручных строк вне ci.yml или без прогона CI |
| Недоступные журналы | 0 (в окне 60 дней все журналы читаются; статус `log_unavailable` в этой выборке не понадобился) |
| Вызовов gh при пустом кэше | около 280 (постраничные списки прогонов, jobs, журналы, коммиты) |

Правило статусов (скрипт, `classify`): `confirmed` — тот же nodeid прошёл на том же `tested_tree` (другой attempt того же прогона на том же коммите, другой push/dispatch-прогон с тем же деревом, либо локальный повтор из `--manual`); `fixed` — прохода на том же дереве нет, но файл теста менялся до следующего зелёного прогона ветки; `unconfirmed` — иначе, причина в `evidence`. Прогон, где шаг Test пропущен по дедупу дерева, подтверждением не считается (проверяется `conclusion == success` шага Test). Для pull_request `tested_sha` — merge-коммит из строки `git log -1 --format=%H` журнала checkout; дерево берётся из git, а при отсутствии коммита локально — запросом `git fetch origin <sha>` либо `gh api git/commits`. Повтор run_attempt принимается только после сверки проверенного коммита по журналу второго attempt.

## Итог переписи

```
runs=3195; failed attempts=86; first failed step: Test=48, Secret scan=14, no failed step in the pytest job=12, Check formatting=3, Static security scan=3, Complexity budget=2, Set up job=2, Lint=1, Type check=1
rows=79; by status: confirmed=27, fixed=35, unconfirmed=17
by event: local=5, pull_request=33, push=12, workflow_dispatch=29
by class: гонки git=3, детерминированная регрессия=47, дочерние процессы=9, иное=3, общее состояние вне теста=4, планировщик asyncio=1, сон и стена=12
distinct failing nodeids: 33
flaky candidates (class is not deterministic): nodeids=17 rows=32
  by status: confirmed=27, unconfirmed=5
  by class: гонки git=3, дочерние процессы=9, иное=3, общее состояние вне теста=4, планировщик asyncio=1, сон и стена=12
  rows on develop/main: 11

| nodeid | class | statuses | rows on develop/main | source task | pre-merge CI runs (upper bound) | CI failures/executions in window | P(at least one failure in the pre-merge runs) |
|---|---|---|---|---|---|---|---|
| tests/test_api_context.py::test_context_and_views_match_the_pre_refactor_baseline | общее состояние вне теста | confirmed=1 | 0/1 | 1691 | 0 | 1/45 | n/a |
| tests/test_brand.py::test_legacy_only_tokens_do_not_authenticate | дочерние процессы | confirmed=1 | 0/1 | None | None | 1/2081 | n/a |
| tests/test_ci_trust.py::test_a_retry_round_reserves_its_own_version_and_a_late_answer_loses | иное | unconfirmed=1 | 0/1 | None | None | None/None | n/a |
| tests/test_delivery_gate.py::test_git_ops_update_by_push_never_overwrites_a_racing_push | гонки git | confirmed=3 | 2/3 | 1419 | 8 | 3/932 | 0.025 |
| tests/test_delivery_gate.py::test_the_limit_does_not_cut_off_the_oldest_live_row | сон и стена | confirmed=2, unconfirmed=1 | 1/3 | 1204 | 14 | 2/1439 | 0.019 |
| tests/test_egress_watch.py::test_probe_is_bounded_and_never_blocks_the_loop | планировщик asyncio | confirmed=1 | 0/1 | 1645 | 3 | 1/295 | 0.010 |
| tests/test_git_ops.py::test_run_bytes_cancel_kills_the_pipeline | дочерние процессы | unconfirmed=1 | 0/1 | None | None | None/None | n/a |
| tests/test_lifecycle.py::test_a_probe_never_leaks_its_secret | иное | confirmed=1 | 0/1 | 1236 | 1 | 1/1530 | 0.001 |
| tests/test_locator_worker.py::test_a_crashing_or_garbling_worker_makes_every_locator_unknown | дочерние процессы | confirmed=1 | 0/1 | None | None | 1/296 | n/a |
| tests/test_mcp_transport.py::test_mcp_endpoint_requires_bearer_at_public_path | общее состояние вне теста | confirmed=2 | 0/2 | 1 | 0 | 2/2082 | n/a |
| tests/test_red_test_gate.py::test_baseline_step_runs_branch_tests_on_merge_base | общее состояние вне теста | confirmed=1 | 0/1 | None | None | None/None | n/a |
| tests/test_review_dispatch.py::test_the_queued_local_run_starts_with_a_live_access_code | сон и стена | confirmed=2 | 1/2 | 1208 | 9 | 2/1405 | 0.013 |
| tests/test_review_drain.py::test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker | дочерние процессы | confirmed=5 | 2/5 | 1588 | 5 | 5/505 | 0.049 |
| tests/test_review_drain.py::test_a_signal_during_the_wait_removes_only_our_marker | дочерние процессы | confirmed=1 | 1/1 | 1588 | 5 | 1/501 | 0.010 |
| tests/test_review_drain.py::test_no_active_jobs_or_no_spool_is_immediate | сон и стена | unconfirmed=1 | 0/1 | None | None | None/None | n/a |
| tests/test_review_drain.py::test_the_marker_is_renewed_beyond_its_ttl_independently | сон и стена | confirmed=6 | 4/6 | 1588 | 5 | 6/506 | 0.058 |
| tests/test_steward_dispatch.py::test_a_local_advisor_run_is_withdrawn_and_closed_on_every_exit | иное | unconfirmed=1 | 0/1 | None | None | None/None | n/a |
```

Что показывает перепись:

- Из 33 разных упавших nodeid **16 — детерминированные поломки** (47 строк): тест падает на дереве каждый раз (локально 3 из 3 или 10 из 10 красных), автор ветки правит код. Это не нестабильность; их ловит CI ветки, как задумано. Сюда же `test_no_legacy_name` (7 строк, 6 деревьев).
- **17 nodeid — кандидаты в нестабильные**, 32 строки: 12 nodeid из журналов шага Test (26 строк); 1 nodeid из шага отчёта PR-прогона (`test_red_test_gate`, validation=fail); 4 nodeid только по отчётам исполнителей (локально, без прогона ci.yml); ещё одна локальная строка (#1682) относится к CI-nodeid `test_the_limit…`.
- Подтверждены повтором **на том же дереве по CI**: 8 nodeid, 21 строка (9 из них — повтором того же прогона run_attempt, 12 — другими прогонами с тем же деревом) (в том числе все 7 строк из таблицы фактов карточки: прогоны 38241673074, 38083616982, 37539128383, 37495761804, 37434457494, 37417835760, 37417217015). Только локальным повтором на том же дереве: 5 строк (`test_api_context`, `test_locator_worker`, `test_egress_watch`, `test_mcp_transport` ×2) и 1 строка шага отчёта (`test_red_test_gate`, воспроизведено переменной окружения). Эти шесть слабее: локальная машина не раннер.
- До `develop`/`main` дошли **11 из 26** CI-строк кандидатов (42%); остальные 15 упали на ветках задач. Каждая из 11 остановила релиз или полный прогон.
- Дифф прогона менял файл теста только в 2 из 26 CI-строк кандидатов (`test_api_context`, `test_locator_worker`): остальные 24 — «упал тест, которого дифф не менял».
- Классы (строки кандидатов): сон и стена 12, дочерние процессы 9, общее состояние вне теста 4, гонки git 3, иное 3, планировщик asyncio 1. **21 из 32 строк — время и процессы**.
- Топ nodeid по числу падений: `test_the_marker_is_renewed_beyond_its_ttl_independently` 6 (исправлен #1627), `test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker` 5 (не исправлен: последнее падение 11.10), `test_git_ops_update_by_push_never_overwrites_a_racing_push` 3 (исправлен #1684, в develop 315f45fe), `test_the_limit_does_not_cut_off_the_oldest_live_row` 3 с локальным (не исправлен).

## Подтверждённые случаи: источник, класс, проверки до мержа

«Проверки до мержа» — число прогонов ci.yml на ветках задачи-источника (по префиксу `task-N/`) до даты коммита, добавившего тест; верхняя оценка числа выполнений теста в CI (каждый прогон гоняет pytest `-n auto` один раз; успешные PR и dispatch-прогоны Test всегда выполняют). Параллельность CI: `-n auto` на ubuntu-latest (число ядер в журнале не печатается; допущение: 4 vCPU для публичного репозитория). Число локальных запусков автора в отчётах сдачи не записано: прочитаны карточки #1588 и #1419, в обеих только «CI зелёный» и список заявленных мутаций; остальные задачи-источники не открывались. Мутации и baseline: baseline гоняет изменённые тесты один раз на коде merge-base (ожидаемо красный, зелёного повтора не даёт); сколько раз мутации выполнили тест, восстановить нельзя (артефакты `hub-ci-results` и отчёты мутаций истекли, в выборке мутации давали `no_tests`).

| nodeid | класс | задача-источник | проверки до мержа | падений в окне / выполнений (оценка) | где всплыл | исправление |
|---|---|---|---|---|---|---|
| test_review_drain::test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker | дочерние процессы | #1588 | 5 прогонов (3 PR, 2 dispatch), все зелёные | 5 / 505 (1,0%) | 2 из 5 на develop/main | нет |
| test_review_drain::test_the_marker_is_renewed_beyond_its_ttl_independently | сон и стена | #1588 (последнее изменение #1595) | те же 5 | 6 / 506 (1,2%) | 4 из 6 на develop/main | #1627 |
| test_review_drain::test_a_signal_during_the_wait_removes_only_our_marker | дочерние процессы | #1588 (последнее #1595) | те же 5 | 1 / 501 (0,2%) | develop | нет |
| test_delivery_gate::test_git_ops_update_by_push_never_overwrites_a_racing_push | гонки git | #1419 | 8 (4 PR, 4 dispatch), 4 сдачи | 3 / 932 (0,3%) | 2 из 3 на develop/main | #1684 |
| test_delivery_gate::test_the_limit_does_not_cut_off_the_oldest_live_row | сон и стена | #1204 | 14 PR-прогонов | 2 / 1439 (0,14%) | 1 из 2 на develop | нет |
| test_review_dispatch::test_the_queued_local_run_starts_with_a_live_access_code | сон и стена | #1208 (изменён #1670 после падений) | 9 (8 PR, 1 dispatch) | 2 / 1405 (0,14%) | 1 из 2 на develop | в docstring названа гонка #1325; тест менялся #1670 (11.10) |
| test_lifecycle::test_a_probe_never_leaks_its_secret | иное (случайный адрес объекта в журнале) | #1236 | 1 PR-прогон | 1 / 1530 (0,07%) | PR | #1251, #1362 (изменения теста в тот же день и позже) |
| test_egress_watch::test_probe_is_bounded_and_never_blocks_the_loop | планировщик asyncio | #1645 | 3 (2 PR, 1 dispatch) | 1 / 295 (0,34%) | dispatch ветки #1680 | нет |
| test_brand::test_legacy_only_tokens_do_not_authenticate | дочерние процессы | до хаба (коммиты 08.2026) | не восстановлены | 1 / 2081 (0,05%) | dispatch ветки #1666 | #1666, #1667 |
| test_locator_worker::test_a_crashing_or_garbling_worker_makes_every_locator_unknown | дочерние процессы | без номера задачи в теме коммита | не восстановлены | 1 / 296 | dispatch ветки #1650, тест в диффе этой ветки: поймано до мержа | правка в ветке #1650 |
| test_mcp_transport::test_mcp_endpoint_requires_bearer_at_public_path | общее состояние вне теста | #1 (до окна) | вне окна | 2 / 2082 | PR и dispatch ветки #1667 (тест в диффе не менялся) | не установлено |
| test_api_context::test_context_and_views_match_the_pre_refactor_baseline | общее состояние вне теста (имя хоста в эталоне) | #1691 | тест новый в этой ветке | 1 / 45 | dispatch ветки #1691: поймано до мержа | правка автора |
| test_red_test_gate::test_baseline_step_runs_branch_tests_on_merge_base | общее состояние вне теста (переменная окружения шага отчёта) | не установлена (git log -L не искался: падение вне шага Test) | не восстановлены | вне Test-шага | PR-прогон 38269508400 (validation=fail), тот же head_sha в двух dispatch-прогонах зелёный | нет |

Пять nodeid без прогона ci.yml (`test_run_bytes_cancel_kills_the_pipeline`, `test_no_active_jobs_or_no_spool_is_immediate` — #1681; `test_the_limit…` — #1682; `test_a_local_advisor_run_is_withdrawn_and_closed_on_every_exit`, `test_a_retry_round_reserves_its_own_version_and_a_late_answer_loses` — #1684) вынесены как строки `event=local`, статус `unconfirmed`: дерево неизвестно. Для `test_no_active_jobs_or_no_spool_is_immediate` в заметке сказано «также в CI 11.10»; в ci.yml за окно строки с этим nodeid нет (проверялась каждая строка `FAILED` шага Test), так что либо это был другой workflow, либо прогон не завершился падением шага Test. Данных недостаточно.

## Гипотеза стюарда: вердикт

Формулировка: «новый или изменённый тест до мержа выполняется мало раз и без нагрузки; реестра нестабильных тестов нет».

Вердикт: **подтверждена по числу выполнений, но недостаточна как объяснение; по мутациям и локальным запускам автора — данных недостаточно.**

Числа:

1. Восемь CI-подтверждённых nodeid; у семи из них известна задача-источник и прогоны до мержа (`test_brand` добавлен до хаба): до мержа каждый прошёл в CI ветки 1–14 раз (медиана 5, сумма 47 выполнений), без повтора внутри прогона и без искусственной нагрузки. Падений в этих 47 выполнениях: **0**.
2. Частота падений на выполнение в окне у этих тестов: от 0,07% до 1,2% (таблица выше). Ожидаемое число падений в 47 выполнениях по этим частотам: 0,18; вероятность хотя бы одного падения до мержа по всем семи вместе 16%, по каждому отдельно от 0,1% до 5,8%. То есть ноль падений до мержа — ожидаемый исход (84%), а не сбой проверок.
3. Чтобы найти частоту 1% с вероятностью 95%, нужно около 300 выполнений, частоту 0,14% — около 2100. Рост числа обычных CI-прогонов этого не даёт: за 60 дней тест `test_review_drain` выполнился около 500 раз во всём CI, а упал 5–6 раз.
4. Опровергающая часть: «CI уже повторяет AC-тесты в baseline и мутациях» — зелёного повтора на том же коде они не дают (baseline идёт на коде merge-base и по замыслу красный; мутации гоняют тесты по файлам изменённых функций, но число выполнений нигде не сохраняется). Поэтому утверждение «повторов нет» подтверждено для baseline и **не проверяемо** для мутаций.
5. Реестр действительно отсутствовал: до этой переписи 17 кандидатов нигде не были собраны; из 11 попаданий на develop/main каждое разбиралось отдельно.
6. Что гипотеза не объясняет: 16 из 33 упавших nodeid — детерминированные поломки, нестабильности в них нет, а 6 слабых подтверждений (локальных) с классом «общее состояние вне теста» (имя хоста, переменные окружения шага, одноразовый объект модуля) — это не «мало повторов», а негерметичность. Повторы под нагрузкой поймали бы часть классов, но не все (раздел «Опыты»).

## Этап, на котором можно было поймать

Этапы цепочки: (E0) локальный запуск автора перед сдачей; (E1) CI ветки задачи (Test, один раз за прогон); (E2) baseline и мутации (dispatch и первый PR-прогон); (E3) CI после мержа на develop/main (то, что остановило релиз). Все 11 попаданий в базу прошли E1 без падения.

| Случай | Поймать мог | Основание |
|---|---|---|
| dead_parent (#1588) | E0/E1 с повтором под нагрузкой | воспроизведено локально 4 из 80 (2/40 без нагрузки, 2/40 с нагрузкой) |
| limit_not_cut (#1204) | E1 с повтором | воспроизведено 4 из 480 под нагрузкой |
| queued_local_run (#1208) | E1 с повтором под нагрузкой | воспроизведено 1 из 80 под нагрузкой; один повтор 5,9 с, бюджет повторов мал |
| probe_leaks (#1236) | E1 с повтором | воспроизведено 3 из 480 под нагрузкой |
| mcp_transport (shared state) | E0/E1 с повтором в одном процессе | второй запуск в процессе падает 9 раз из 10 (отчёт rA, `RuntimeError: ... only be called once per instance`) |
| egress_watch (#1645), run_bytes_cancel, no_active_jobs, ci_trust-тест | статическое правило на стену/pgrep/фиксированный сон | AST-скан дерева c8fe209 помечает все четыре; повторы под нагрузкой их не воспроизвели (0 из 200, 100, 100) |
| red_test_gate (#1681) | герметичное окружение | воспроизведено переменной окружения (раздел «Опыты») |
| api_context (#1691) | E1 (уже поймано до мержа) | имя хоста в эталоне автора |
| marker_renewed (#1588), signal (#1588), git race (#1419), brand (#1666), locator | данных недостаточно | локально 0 из 80, 230, 180, 80, 490 за бюджет; частоты 0,05–1,2% требуют сотен повторов с ценой минут |

## Опыты (воспроизведения)

Хост: macOS 26.6, arm64, 8 ядер, Python 3.12, uv; на машине параллельно работали другие сессии (load average 4–17). Дерево — то, на котором упал CI (для merge-коммитов PR создавался коммит `git commit-tree <tree>` и `git worktree add --detach`); повтор внутри одного процесса — плагин pytest `repeatplug` (20 строк), которого нет в репозитории:

```python
# repeatplug.py: REPS=N повторяет тест N раз в одном процессе
import os, pytest
def pytest_generate_tests(metafunc):
    n = int(os.environ.get("REPS", "1"))
    if n > 1:
        metafunc.fixturenames.append("_rep_step")
        metafunc.parametrize("_rep_step", range(n), indirect=True)
@pytest.fixture
def _rep_step(request):
    return request.param
```

Команда: `REPS=<N> PYTHONPATH=<каталог плагина> uv run --directory <worktree дерева> pytest <nodeid> -q -p repeatplug -p no:cacheprovider`. Нагрузка — 16 процессов `python -c "while True: pass"` на 8 ядрах. Полные команды и счётчики каждой серии лежат в `local_runs` файла `.manual.json` и в `evidence.local_runs` каждой строки.

| Серия | Результат |
|---|---|
| 10 nodeid (8 CI-подтверждённых, `test_locator_worker`, `test_egress_watch`), по 40 повторов без нагрузки и с нагрузкой; `test_mcp_transport` отдельными процессами 3 раза | воспроизведено: dead_parent 2/40 без нагрузки и 2/40 с нагрузкой, queued_local_run 1/40 (с нагрузкой); остальное 0; mcp_transport 3 из 3 зелёных по отдельности |
| те же, у быстрых тестов 400 повторов под нагрузкой | limit_not_cut 4/400, probe_leaks 3/400; locator_worker 0/400; signal 0/150; egress 0/120 |
| 3 теста #1681/#1684 на дереве до #1684, 100 повторов под нагрузкой | run_bytes_cancel 0/100, no_active_jobs 0/100, git race 0/100 |
| 2 теста из отчёта #1684 на текущем develop, 100 под нагрузкой | 0/100 и 0/100 |
| 25 пар (nodeid, дерево) «fixed» по 3 повтора (параметризованные по 1) и 12 пар «unconfirmed» по 10 повторов | красные на каждом повторе: детерминированные поломки |
| `test_mcp_transport` 10 повторов в одном процессе | 9 красных, 1 зелёный (`RuntimeError … only be called once per instance`): тест повтор-небезопасен |
| `test_red_test_gate` на дереве merge-коммита 5e6ebf09 | без переменных 1 passed; с `HAIPLANE_HUB_CI_BASELINE_OUTCOME=skipped` 1 failed |
| AST-скан дерева c8fe209 (правила R1 assert по `monotonic`/`perf_counter`, R2 `pgrep`/`ps`, R3 фиксированный `sleep` ≥ 0,05 с) | помечено 84 из 5602 тестов (R1 20, R2 2, R3 74); из 17 кандидатов помечены 4 (run_bytes R2+R3, no_active_jobs R1, egress R1+R3, ci_trust R3) |

Ограничения опытов: macOS, а не Linux-раннер (заглушка flock вместо системного); нагрузка нестрогая. Повторами под нагрузкой воспроизведено 4 из 14 протестированных CI-nodeid плюс mcp_transport (повтор в процессе), статическим правилом помечено 4 из 17, остальное остаётся «не воспроизведено за бюджет».

## Меры: черновики задач

Цена в минутах CI: медианная длительность прогона в окне — PR 7,3 мин (p90 17,7), dispatch 8,1 мин (p90 21,2), push 3,5 мин. Деньги: репозиторий публичный, стандартные раннеры GitHub для него бесплатны (допущение; платным остаётся время ожидания и работа людей). Прогонов task-веток в окне: 427 веток с PR, 523 dispatch-прогона.

### M1. Повтор изменённых и добавленных тестов под нагрузкой в CI ветки

- Класс: сон и стена, дочерние процессы, общее состояние вне теста (одноразовые объекты модулей), иное (случайные данные).
- Этап: шаг после Test в прогонах `workflow_dispatch` и первом PR-прогоне task-ветки (то же условие, что у мутаций и baseline, ci.yml шаг «Decide whether mutations…»).
- Что делает: берёт тест-функции, добавленные или изменённые диффом (механика `scripts/red_test_baseline.py` и `scripts/mutation_changed.py`), и гоняет каждую K раз в одном процессе (плагин повтора) при фоновой нагрузке на все ядра; K = min(400, 120 с / длительность повтора под нагрузкой); итого не больше 8 минут. Падение даёт красный шаг с nodeid и числом падений.
- (а) Проверено опытом: dead_parent (4/80), limit_not_cut (4/480), queued_local_run (1/80), probe_leaks (3/480), mcp_transport (повтор в процессе падает 9 из 10). Всего 5 из 17 кандидатов (воспроизведение — раздел «Опыты»).
- (б) Ожидаемое покрытие остальных при K по формуле выше, допущение — частота падения на выполнение равна оценке за окно: marker_renewed 0,31 (K=31 при 3,8 с на повтор), signal 0,37, git race 0,14, egress 0,25, queued_local 0,03 (K=20), brand 0,01 (K=23), locator 0,74 (0 из 490 локально, оценка завышена), api_context n/a. Среднее по десяти случаям с оценкой частоты (включая воспроизведённые) около 0,32. Оговорки: оценки частоты берутся из CI, а нагрузка опыта слабее CI; вероятность обнаружения на тесте без воспроизведения может быть ниже в разы.
- Ложные срабатывания: тесты, непригодные к повтору в процессе (`test_mcp_transport` падает при втором запуске); нужен маркер `no_repeat` с реестром исключений. В опыте 1 из 17 протестированных nodeid (6%) повтор-небезопасен. Красный по дрейфу нагрузки в слабом раннере ожидается редко: тест красный в повторе означает реальную недетерминированность или нехватку устойчивости к нагрузке.
- Цена: +2 мин в среднем на прогон с изменёнными тестами (потолок 8 мин) на 427 + 523 прогонах за 60 дней, то есть до 1 900–7 600 минут за период; деньги 0 (публичный репозиторий), задержка сдачи.
- Черновик AC: (1) тест, добавленный в дифф и падающий в 1 из 50 повторов, роняет шаг и называет nodeid; (2) тест без изменений в диффе не повторяется; (3) повторяемость внутри процесса не ломает набор: маркер `no_repeat` для `test_mcp_transport`; (4) шаг укладывается в 8 минут на 5 изменённых тестах; (5) красный шаг входит в отчёт CI хабу отдельным полем и блокирует сдачу при `ci_before_submit=require`.

### M2. Автоповтор упавшего теста на том же дереве и реестр

- Класс: все; этап: CI после падения шага Test (E1 и E3).
- Что делает: при падении Test перезапускает только упавшие nodeid один раз в том же job (`pytest --lf`) и пишет в отчёт `flaky_pass` с nodeid и `tested_tree`; job остаётся красным (поведение гейта не меняется), но отчёт создаёт запись реестра и черновик находки. Отдельным решением владельца: для develop/main считать прошедший повтор достаточным, чтобы релиз не вставал (в окне это 11 остановок).
- (а) Проверено опытом: сама перепись — 21 CI-строка из 26 подтверждены повтором на том же дереве, из них 9 повтором того же прогона (`rerun of the same run`): такой автоповтор дал бы подтверждение в момент падения без ручной переписи. Нагрузочный опыт не нужен.
- (б) Ожидаемое покрытие: все стабильные падения с частотой ниже 50% при втором прогоне; для частоты p вероятность, что повтор тоже упадёт, равна p (0,05–1,2%), то есть 99% кандидатов перейдут в `flaky_pass`. Детерминированные поломки падают снова и остаются красными (47 строк переписи).
- Ложные срабатывания: повтор скрывает нестабильность, если реестр не читают (риск процесса); mitigation: черновик находки создаётся автоматически.
- Цена: повтор только на упавших прогонах: 48 attempt за 60 дней, +1–5 минут каждый; деньги 0.
- AC: (1) падение теста в CI порождает вторую попытку этого nodeid; (2) в отчёте есть поля `rerun_outcome` и `tested_tree`; (3) детерминированная поломка остаётся красной; (4) создаётся запись реестра с классом «не назначен».

### M3. Статические правила для тестов (стена, процессы по строке, фиксированный сон)

- Класс: сон и стена, дочерние процессы, планировщик asyncio. Этап: lint в CI и в `make check`, секунды.
- Правила: R1 — `assert` со сравнением по `monotonic()`/`perf_counter()`; R2 — `pgrep`/`ps ` в теле теста; R3 — `sleep(<число ≥ 0,05>)` перед проверкой состояния. Ledger исключений по образцу `docs/agent-context/complexity-budget.json`; новое нарушение красит шаг.
- (а) Проверено опытом: AST-скан дерева c8fe209 пометил 4 из 17 кандидатов (run_bytes_cancel, no_active_jobs, egress_watch, ci_trust).
- (б) Остальные 13 кандидатов правило не видит (гонки в самой логике продукта: git race, ротация по секундам, адрес объекта в журнале); ожидаемое покрытие — только классы, где стена и pgrep записаны в тексте теста.
- Ложные срабатывания: 84 из 5602 тестов помечены сегодня (1,5%); известных нестабильных среди них 4, то есть в ledger при внедрении придётся занести около 80 записей, из которых большинство не нестабильны. Для R1+R2 помечено 22 теста, 3 известных нестабильных (14%).
- Цена: секунды в CI, 0 денег, одноразовая работа над ledger.
- AC: (1) новый тест с `assert time.monotonic() - t0 < X` красит шаг; (2) тест из ledger не красит; (3) удаление записи из ledger при исчезновении нарушения требует снять запись; (4) правило читает только `tests/`, ложное срабатывание на `time.monotonic()` вне assert не даёт нарушения.

### M4. Герметичное окружение тестов

- Класс: общее состояние вне теста (переменные окружения, имя хоста). Этап: каждый запуск тестов (E0–E3).
- Что делает: autouse-фикстура убирает из окружения теста `HAIPLANE_HUB_*` и переменные CI (`GITHUB_*`, `CI`), кроме явно объявленных тестом (маркер `uses_env`), и тест-сторож запрещает имя хоста машины автора в закоммиченных эталонах.
- (а) Проверено опытом: `test_red_test_gate` — без переменной зелёный, с `HAIPLANE_HUB_CI_BASELINE_OUTCOME=skipped` красный на том же дереве merge-коммита (1 passed, 1 failed); с фикстурой падение невозможно по построению. 1 случай.
- (б) Остальное: api_context (имя хоста в эталоне) ловит сторож по имени хоста; ожидаемое покрытие 2 из 3 nodeid класса «общее состояние», вероятность обнаружения высокая (детерминированно), mcp_transport не покрывается (нужен M1).
- Ложные срабатывания: тесты, которым нужна переменная; маркер `uses_env` и список в ledger.
- Цена: секунды; 0 денег.
- AC: (1) переменная `HAIPLANE_HUB_CI_BASELINE_OUTCOME` в окружении pytest не меняет исход `test_red_test_gate`; (2) тест, читающий переменную без маркера, падает с понятным сообщением; (3) строка `MacBook` или имя хоста в `tests/` красит сторожа.

Рекомендуемый порядок: M1 и M2 (закрывают то, что видно по числам: мало повторов и нет данных), затем M4 (дёшево, детерминированно), M3 последним (дорогой ledger при малой точности).

## Что не сделано и ограничения

- Окно ровно 60 дней; тесты, упавшие до 16.08, не охвачены. Упавшие attempt без шага Test (lint, secret scan и т.д.) в перепись не входят.
- `tested_tree` для PR-прогонов берётся из merge-коммита; во всех 73 строках журнала Test-шага дерево восстановлено (`unconfirmed` по причине «дерево не восстановлено» нет).
- Классы причин назначены вручную по тексту теста, сообщению и опытам; у `test_a_local_advisor_run_is_withdrawn…` и `test_a_retry_round_reserves…` причина не установлена (класс «иное»).
- Подтверждение по локальному повтору (5 строк плюс `test_red_test_gate`) слабее подтверждения по CI: машина другая.
- Число локальных запусков автора проверено по двум задачам из семи; число выполнений теста мутациями не восстановлено.
- Строки `event=local` (5 штук) не имеют `tested_tree` и не подтверждаются; они приведены ради полноты списка кандидатов карточки.
- Кэш `gh` и временные worktree в коммит не входят.

## Приложение: строки переписи

| run | att | branch | event | tested_sha | status | class | nodeid |
|---|---|---|---|---|---|---|---|
| - | - | task-1684/* | local | - | unconfirmed | иное | tests/test_ci_trust.py::test_a_retry_round_reserves_its_own_version_and_a_late_answer_loses |
| - | - | task-1682/* | local | - | unconfirmed | сон и стена | tests/test_delivery_gate.py::test_the_limit_does_not_cut_off_the_oldest_live_row |
| - | - | task-1681/flaky-local-prompt | local | - | unconfirmed | дочерние процессы | tests/test_git_ops.py::test_run_bytes_cancel_kills_the_pipeline |
| - | - | task-1681/flaky-local-prompt | local | - | unconfirmed | сон и стена | tests/test_review_drain.py::test_no_active_jobs_or_no_spool_is_immediate |
| - | - | task-1684/* | local | - | unconfirmed | иное | tests/test_steward_dispatch.py::test_a_local_advisor_run_is_withdrawn_and_closed_on_every_exit |
| 33156749350 | 1 | develop | push | 1019c7dc | fixed | детерминированная регрессия | tests/test_workflow_reference.py::test_lifecycle_transitions_unchanged |
| 33156752766 | 1 | develop | pull_request | 6cdb53f4 | fixed | детерминированная регрессия | tests/test_workflow_reference.py::test_lifecycle_transitions_unchanged |
| 33171935998 | 1 | task-1007/finding-uid-locator | pull_request | 5fbe6274 | fixed | детерминированная регрессия | tests/test_lifecycle.py::test_disposed_findings_produce_no_note |
| 33171935998 | 1 | task-1007/finding-uid-locator | pull_request | 5fbe6274 | fixed | детерминированная регрессия | tests/test_lifecycle.py::test_second_report_names_the_dispatched_reviewer |
| 34022980998 | 1 | task-1180/local-reviewer | pull_request | bc76799e | unconfirmed | детерминированная регрессия | tests/test_no_legacy_name.py::test_head_contains_no_legacy_name |
| 34350251898 | 1 | task-1168/escalation-report-quality | pull_request | 4a8265a8 | unconfirmed | детерминированная регрессия | tests/test_no_legacy_name.py::test_head_contains_no_legacy_name |
| 34451276933 | 1 | task-1235/circle-detector | pull_request | dd9a7dba | confirmed | иное | tests/test_lifecycle.py::test_a_probe_never_leaks_its_secret |
| 35772345819 | 1 | task-1269/steward-first-judgement | workflow_dispatch | bed4a3f3 | unconfirmed | детерминированная регрессия | tests/test_no_legacy_name.py::test_head_contains_no_legacy_name |
| 35780035532 | 1 | develop | pull_request | fd5d49ef | confirmed | сон и стена | tests/test_delivery_gate.py::test_the_limit_does_not_cut_off_the_oldest_live_row |
| 35785728735 | 1 | develop | push | ac7bdc57 | confirmed | сон и стена | tests/test_review_dispatch.py::test_the_queued_local_run_starts_with_a_live_access_code |
| 35806396435 | 1 | task-1275/stack-walk-undecided-statuses | workflow_dispatch | fb2c93d9 | confirmed | сон и стена | tests/test_review_dispatch.py::test_the_queued_local_run_starts_with_a_live_access_code |
| 35966739991 | 1 | task-1363/mcp-session-restart | workflow_dispatch | 60df0e74 | unconfirmed | детерминированная регрессия | tests/test_no_legacy_name.py::test_head_contains_no_legacy_name |
| 36056014437 | 1 | task-1274/orchestrator-queue-shadow | pull_request | 96abdc18 | confirmed | сон и стена | tests/test_delivery_gate.py::test_the_limit_does_not_cut_off_the_oldest_live_row |
| 36114877269 | 1 | task-1401/review-context-package | workflow_dispatch | 203d2f36 | unconfirmed | детерминированная регрессия | tests/test_no_legacy_name.py::test_head_contains_no_legacy_name |
| 36125011361 | 1 | develop | push | 7d57cfa8 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36125011361 | 1 | develop | push | 7d57cfa8 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36125011361 | 1 | develop | push | 7d57cfa8 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36125015796 | 1 | develop | pull_request | 74f15f8d | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36125015796 | 1 | develop | pull_request | 74f15f8d | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36125015796 | 1 | develop | pull_request | 74f15f8d | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36125033313 | 1 | develop | push | d162a40a | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36125033313 | 1 | develop | push | d162a40a | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36125033313 | 1 | develop | push | d162a40a | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36125038431 | 1 | develop | pull_request | 9418002b | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36125038431 | 1 | develop | pull_request | 9418002b | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36125038431 | 1 | develop | pull_request | 9418002b | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36125058990 | 1 | develop | push | 1e1f2910 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36125058990 | 1 | develop | push | 1e1f2910 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36125058990 | 1 | develop | push | 1e1f2910 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36125065600 | 1 | develop | pull_request | d6bd89cf | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36125065600 | 1 | develop | pull_request | d6bd89cf | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36125065600 | 1 | develop | pull_request | d6bd89cf | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36126237189 | 1 | task-1410/executor-run-row | workflow_dispatch | ae7279bf | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36126237189 | 1 | task-1410/executor-run-row | workflow_dispatch | ae7279bf | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36126237189 | 1 | task-1410/executor-run-row | workflow_dispatch | ae7279bf | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36126937499 | 1 | task-1407/gate-merge-wait-and-unread | workflow_dispatch | b1a1efe3 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36126937499 | 1 | task-1407/gate-merge-wait-and-unread | workflow_dispatch | b1a1efe3 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36126937499 | 1 | task-1407/gate-merge-wait-and-unread | workflow_dispatch | b1a1efe3 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36133349256 | 1 | task-1414/deep-daily-cap | workflow_dispatch | 549e45c2 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_an_uncounted_report_is_not_a_delta_base[self_reviewed] |
| 36133349256 | 1 | task-1414/deep-daily-cap | workflow_dispatch | 549e45c2 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_is_previous_generation_when_its_review_is_recorded |
| 36133349256 | 1 | task-1414/deep-daily-cap | workflow_dispatch | 549e45c2 | fixed | детерминированная регрессия | tests/test_review_dispatch.py::test_delta_base_skips_generation_without_recorded_review |
| 36215317762 | 1 | task-1405/review-on-green-ci | workflow_dispatch | 0a59096c | fixed | детерминированная регрессия | tests/test_review_evidence.py::test_the_authors_claim_survives_the_dispatch_note |
| 36479851980 | 1 | task-913/red-test-gate | workflow_dispatch | 8212d40e | fixed | детерминированная регрессия | tests/test_local_checks_match_ci.py::test_a_new_uncovered_step_breaks_the_build |
| 36479851980 | 1 | task-913/red-test-gate | workflow_dispatch | 8212d40e | fixed | детерминированная регрессия | tests/test_local_checks_match_ci.py::test_every_ci_static_check_has_a_make_target |
| 36479851980 | 1 | task-913/red-test-gate | workflow_dispatch | 8212d40e | unconfirmed | детерминированная регрессия | tests/test_mcp_catalog_budget.py::test_description_chars_stay_under_freeze |
| 36479851980 | 1 | task-913/red-test-gate | workflow_dispatch | 8212d40e | unconfirmed | детерминированная регрессия | tests/test_mcp_catalog_budget.py::test_the_report_leads_with_the_limit_that_binds |
| 36479851980 | 1 | task-913/red-test-gate | workflow_dispatch | 8212d40e | fixed | детерминированная регрессия | tests/test_web.py::test_saving_the_project_form_keeps_unknown_policy_keys |
| 37362288795 | 1 | task-1590/predeploy-backup | workflow_dispatch | 5222714d | confirmed | сон и стена | tests/test_review_drain.py::test_the_marker_is_renewed_beyond_its_ttl_independently |
| 37375907895 | 1 | task-1591/release-artifacts-check | pull_request | 58a492df | confirmed | дочерние процессы | tests/test_review_drain.py::test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker |
| 37417217015 | 1 | develop | pull_request | 9149fa2d | confirmed | дочерние процессы | tests/test_review_drain.py::test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker |
| 37417835760 | 1 | develop | pull_request | b3b703f5 | confirmed | дочерние процессы | tests/test_review_drain.py::test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker |
| 37434457494 | 1 | develop | pull_request | 3fa23e89 | confirmed | сон и стена | tests/test_review_drain.py::test_the_marker_is_renewed_beyond_its_ttl_independently |
| 37438529641 | 1 | task-1598/promt-lokalnogo-revyuera-nazyvaet-ego-re | workflow_dispatch | ba6ab11c | confirmed | гонки git | tests/test_delivery_gate.py::test_git_ops_update_by_push_never_overwrites_a_racing_push |
| 37495761804 | 1 | main | push | dab592ae | confirmed | сон и стена | tests/test_review_drain.py::test_the_marker_is_renewed_beyond_its_ttl_independently |
| 37539128383 | 1 | develop | pull_request | 8419a629 | confirmed | дочерние процессы | tests/test_review_drain.py::test_a_signal_during_the_wait_removes_only_our_marker |
| 37582340752 | 1 | main | pull_request | ce9acda7 | confirmed | сон и стена | tests/test_review_drain.py::test_the_marker_is_renewed_beyond_its_ttl_independently |
| 37582475944 | 1 | develop | pull_request | ee044402 | confirmed | сон и стена | tests/test_review_drain.py::test_the_marker_is_renewed_beyond_its_ttl_independently |
| 37668027818 | 1 | task-1610/ruchnoe-odobrenie-ne-opiraetsya-na-ustar | workflow_dispatch | 1679a87f | confirmed | сон и стена | tests/test_review_drain.py::test_the_marker_is_renewed_beyond_its_ttl_independently |
| 37959789361 | 1 | task-1650/hab-ispolnyaet-kod-iz-vetki-zadachi-pri | workflow_dispatch | 0a31a2a2 | confirmed | дочерние процессы | tests/test_locator_worker.py::test_a_crashing_or_garbling_worker_makes_every_locator_unknown |
| 38038268501 | 1 | task-1651/neudachnoe-perechislenie-dereva-vetki-ke | pull_request | 2ae5a915 | confirmed | дочерние процессы | tests/test_review_drain.py::test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker |
| 38049486764 | 1 | task-1221/versionirovat-nabor-kanareek-i-kontrakt | pull_request | 339262fa | unconfirmed | детерминированная регрессия | tests/test_agent_eval_contract.py::test_critical_approve_with_error_is_quality_failed |
| 38049486764 | 1 | task-1221/versionirovat-nabor-kanareek-i-kontrakt | pull_request | 339262fa | unconfirmed | детерминированная регрессия | tests/test_agent_eval_contract.py::test_duplicate_with_critical_approve_is_quality_failed |
| 38049486764 | 1 | task-1221/versionirovat-nabor-kanareek-i-kontrakt | pull_request | 339262fa | unconfirmed | детерминированная регрессия | tests/test_agent_eval_contract.py::test_validate_rejects_outcome_downgrade_to_infrastructure |
| 38057528563 | 1 | task-1666/zavisshii-test-derzhit-ci-do-6-chasov-i | workflow_dispatch | ede7c9ef | confirmed | дочерние процессы | tests/test_brand.py::test_legacy_only_tokens_do_not_authenticate |
| 38062226962 | 1 | task-1667/dochernii-protsess-haba-posle-testclient | workflow_dispatch | f14e5a4f | unconfirmed | детерминированная регрессия | tests/test_no_legacy_name.py::test_head_contains_no_legacy_name |
| 38083616982 | 1 | develop | pull_request | 30b1d563 | confirmed | гонки git | tests/test_delivery_gate.py::test_git_ops_update_by_push_never_overwrites_a_racing_push |
| 38084751439 | 1 | task-1667/dochernii-protsess-haba-posle-testclient | pull_request | f88b54a0 | confirmed | общее состояние вне теста | tests/test_mcp_transport.py::test_mcp_endpoint_requires_bearer_at_public_path |
| 38084762788 | 1 | task-1667/dochernii-protsess-haba-posle-testclient | workflow_dispatch | e3db1d85 | confirmed | общее состояние вне теста | tests/test_mcp_transport.py::test_mcp_endpoint_requires_bearer_at_public_path |
| 38084829102 | 1 | task-1223/podnimat-odnorazovyi-hub-dlya-eval-i-sob | workflow_dispatch | f052fc34 | unconfirmed | детерминированная регрессия | tests/test_no_legacy_name.py::test_head_contains_no_legacy_name |
| 38181418208 | 1 | task-1673/archive-needs-reason | pull_request | 271e5339 | confirmed | дочерние процессы | tests/test_review_drain.py::test_a_dead_parent_stops_a_waiting_acquire_and_frees_its_marker |
| 38241673074 | 1 | main | pull_request | 573a671a | confirmed | гонки git | tests/test_delivery_gate.py::test_git_ops_update_by_push_never_overwrites_a_racing_push |
| 38269508400 | 1 | task-1681/flaky-local-prompt | pull_request | 5e6ebf09 | confirmed | общее состояние вне теста | tests/test_red_test_gate.py::test_baseline_step_runs_branch_tests_on_merge_base |
| 38514094063 | 1 | task-1691/snizit-do-porogov-slozhnosti-funktsii-ch | workflow_dispatch | 3465fd1e | confirmed | общее состояние вне теста | tests/test_api_context.py::test_context_and_views_match_the_pre_refactor_baseline |
| 38514112670 | 1 | task-1680/huk-dezhurit-po-yaschiku-rabochei-roli-s | workflow_dispatch | c60f1552 | confirmed | планировщик asyncio | tests/test_egress_watch.py::test_probe_is_bounded_and_never_blocks_the_loop |
