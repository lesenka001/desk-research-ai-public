# SPEC 003 / T3: приватный снимок и откат релиза

Реализация: `scripts/release.py`, Python 3.12+, только стандартная библиотека.
Команда возвращает прежние образы и конфигурацию worker, bot и web, а также
указатели версий материалов. Таблица findings не удаляется и не перезаписывается.
Сервис postgres, его контейнер, Compose-проект и существующий том сохраняются.

## Два разных снимка

Точный локальный снимок исходников до изменений уже находится здесь:

```text
C:\Users\Alesya Benderina\private-releases\spec003-prechange-20261113
```

Его `manifest.json` имеет `kind: local-source-only`, `deployment_snapshot: false`.
Он включает фактические исходники с незакоммиченными изменениями и приватную
конфигурацию; снимок сохранён без изменений. Это **не снимок развёрнутого релиза**:
в нём нет образов Docker и указателей рабочей БД. `rollback` его не принимает.
Нельзя менять его manifest или использовать его каталог для `capture`.

**Настоящий снимок образов возможен только на Docker-хосте развёртывания,
пока прежний релиз ещё работает, до нового release/redeploy.** На локальной
машине Docker отсутствует. Этот документ не утверждает, что боевой снимок уже
получен или откат уже проверен. Git commit, копия source и сборка старого commit
после внедрения не заменяют снимок действительно работающих контейнеров.

## Подготовка на хосте развёртывания

Нужны Docker Engine, Docker Compose v2 с `config --format json`, `config --hash`,
`--pull never`, доступ к исходному проекту и `psql` внутри работающего postgres.
Скрипт запускается на том же хосте и Docker daemon, где работает проект.
`--compose-file` должен указывать на реальный Compose-файл развёртывания,
включая конфигурацию Coolify, а `--project-name` — на его фактическое имя.
Переменные окружения/`.env` должны соответствовать прежнему развёртыванию.
Если проект использует несколько Compose-файлов, сначала предоставьте единый
эквивалентный файл; несовпадение config hash с контейнерами блокирует снимок.

Скопируйте актуальный `scripts/release.py` на хост без redeploy приложения.
Приостановите внешние scheduled jobs, collector, автоматический redeploy и
ручную повторную обработку на время capture/release/rollback. Дождитесь
завершения текущей обработки worker: capture не останавливает контейнеры,
а согласованность снимка с границей релиза обеспечивает оператор. Worker,
bot, web и postgres должны существовать в одном экземпляре и работать.

Создайте приватный родительский каталог вне любых Git-репозиториев; конечный
каталог снимка должен ещё не существовать. Пример для Linux-хоста (замените
пути и имя проекта фактическими значениями):

```sh
install -d -m 700 /srv/private-releases
python3 /srv/ir-navigator/scripts/release.py capture \
  /srv/private-releases/spec003-production-before \
  --compose-file /srv/ir-navigator/docker-compose.yml \
  --project-name ir-navigator
```

Не используйте имя проекта из примера вслепую: другой project name может
указывать на другую БД. Capture проверяет Compose-метки контейнеров, hash
конфигурации каждого сервиса и совпадение разрешённых env с фактическими
переменными контейнеров. Несовпадение, в том числе из-за другой версии
Compose, требует исправить входную конфигурацию, а не отключить проверку.
Запоминается Docker daemon ID, имя проекта, абсолютный рабочий каталог,
имя/пользователь БД и все mount identities postgres, включая имя data volume.

На POSIX каталог получает права `0700`, файлы — `0600`; проверяется владелец
и отсутствие доступа группы/прочих. На Windows оператор должен заранее
обеспечить эквивалентные приватные ACL: Python chmod не обеспечивает их.
Не помещайте снимок в Git, общую папку, публичные CI artifacts или логи.

## Что сохраняется

Завершённый снимок содержит ровно шесть файлов:

| Файл | Содержимое |
|---|---|
| `manifest.json` | `kind: deployed-docker-release`, `format_version: 1`, `deployment_snapshot: true`, UTC-время, уникальный sentinel, SHA-256 пяти артефактов |
| `compose.resolved.json` | Полный `docker compose config --format json`: разрешённые env, конфигурация всех четырёх сервисов, сетей и тома; содержит секреты |
| `compose.rollback.json` | Та же конфигурация; worker/bot/web закреплены на `sha256:<image-config-digest>`, без build, с `pull_policy: never`; литералы `$` экранированы для повторного чтения Compose |
| `deployment.json` | Image ID каждого app-контейнера, доступные registry RepoDigests и image labels, container IDs, identity хоста/проекта/БД и mount identities postgres |
| `images.tar` | Один `docker image save` со всеми уникальными текущими образами worker/bot/web, даже если registry недоступен |
| `database.json` | Все material IDs/URL, исходные и разрешённые указатели, статусы, processing_error; метаданные колонок materials/findings и версия PostgreSQL |

Registry digest может отсутствовать у локально собранного образа. В этом случае
неизменяемым идентификатором служит image ID `sha256:...`; архив сохраняет именно
этот образ, а не изменяемый tag. Image labels сохраняют revision, если он был
встроен при сборке; при отсутствии метки скрипт не выдаёт локальный Git HEAD
за revision реально работающего контейнера. Промпт/код зафиксированы образом,
а runtime env, в том числе PROMPT_VERSION и параметры reprocess, — конфигурацией.

Capture выполняет SELECT через `docker compose exec -T postgres psql` в одной
read-only repeatable-read транзакции. Для null `processed_prompt_version`
фиксируется прежний fallback интерфейса: `findings.prompt_version` по
`created_at DESC, id DESC LIMIT 1`. Таким образом, позднейшие версии не меняют
смысл legacy baseline. Снимок не является dump БД; метаданные схемы предназначены
для аудита, обратная миграция схемы не выполняется. SPEC 003 допускает только
совместимые добавления к схеме.

App bind mounts/volumes, неразрешённые env_file, внешние secrets/configs,
label_file и develop не поддерживаются: capture завершается ошибкой, поскольку
эти изменяемые файлы не фиксируются архивом образов. Поддерживается текущая
топология из ровно worker/bot/web/postgres с именованным томом PostgreSQL.

Manifest записывается последним, после проверки архива и повторной проверки
container IDs. При ошибке остаётся неполный приватный каталог без пригодного
manifest; повторная capture в него запрещена. Сохраните его для диагностики
и выберите новое имя каталога. Существующий снимок никогда не перезаписывается.
Хеши обнаруживают повреждения; это не цифровая подпись от злоумышленника,
который может заменить одновременно manifest и файлы. Доверенным остаётся
приватный каталог оператора; не редактируйте и не меняйте его во время отката.

## Возврат одним действием

После внедрения SPEC 003 команда на исходном хосте:

```sh
python3 /srv/ir-navigator/scripts/release.py rollback \
  /srv/private-releases/spec003-production-before
```

Повторно указывать Compose-файл, project name, env или параметры модели
не нужно: используется сохранённая конфигурация. Откат не читает актуальный
compose приложения; текущий `.env` исключён явным `--env-file` с пустым
системным устройством, переменные `COMPOSE_*` процесса отфильтрованы.
Экранирование `$` сохраняется ровно таким, каким его выводит
[Docker Compose config](https://github.com/docker/compose/blob/v2.39.4/cmd/compose/config.go#L159-L175):
дополнительное удвоение испортило бы значения секретов. Откат не пересобирает
старый commit. Не удаляйте рабочий каталог,
сохранённый в deployment metadata, и не меняйте Docker context.

Последовательность внутри одной команды:

1. Проверить тип/версию снимка, его расположение вне репозиториев, приватность,
   точный набор файлов, отсутствие symlink/junction/hardlink, SHA-256 каждого
   артефакта, структуру material baseline и соответствие двух Compose-конфигов.
   Проверить безопасные пути внутри tar и хеши image configs из Docker save
   manifest: набор образов должен совпадать с сохранёнными image IDs.
2. До первой постоянной мутации проверить Docker daemon ID, проект, работающий
   postgres, БД и mount identities. Разобрать сохранённый Compose, проверить
   закреплённые image IDs и project/service labels существующих app-контейнеров.
   Отсутствующий app-контейнер допускается для повтора после частичной ошибки.
   Через psql проверить наличие baseline materials с теми же URL и отсутствие
   конфликта sentinel. Временная baseline-таблица этого preflight откатывается;
   рабочие таблицы не изменяются.
3. Загрузить `images.tar`, проверить image IDs после загрузки. Только затем
   вызвать `compose stop worker bot web` и проверить, что они остановлены.
4. Одной транзакцией с блокировкой materials/findings восстановить указатели
   и статусы. Вновь проверить baseline identity и конфликт sentinel под lock.
   При ошибке psql с `ON_ERROR_STOP=1` транзакция откатывается.
5. Выполнить `compose up --detach --no-deps --no-build --pull never
   --force-recreate worker bot web` с сохранённым конфигом. Проверить запуск
   этих image IDs и неизменность контейнера postgres/его mounts.

Ни один шаг не вызывает `compose down`, volume remove, Git reset/stash или
миграцию назад. Postgres не включается в stop/up, его зависимости не запускаются.
После возврата проверьте web, Mini App и Telegram; признак Running сам по себе
не доказывает корректность продуктовой выдачи. Скрипт не отправляет сообщения.

## Правила восстановления указателей

| Материал | Указатель после отката | Статус |
|---|---|---|
| Был explicit baseline pointer | Точное прежнее значение, в том числе успешная версия с нулём находок | Прежний статус |
| Был null pointer, существовала legacy latest версия | Зафиксированная при capture версия latest, а не null | Прежний статус |
| Был в снимке, но не было ни pointer, ни legacy findings | Уникальный `__release_no_baseline_<32 hex>__` | `new` |
| Появился после capture | Тот же sentinel, даже если новая версия уже обработана | `new` |

`processing_error` baseline-материалов восстанавливается из снимка; у материалов,
появившихся после capture, сбрасывается в null. Findings всех версий остаются
на месте, включая их audit-поля и sent_at. Не откатываются подписки, runs,
содержимое материалов, источники или доставки Telegram. Уже отправленное
сообщение не может быть отозвано откатом контейнеров.

Sentinel не совпадает ни с одной findings.prompt_version и **не равен null**.
Поэтому прежний `coalesce(materials.processed_prompt_version, latest)` не
раскроет находки, существующие только в новой версии. Возвращённый старый
worker может обработать материалы со статусом `new` заново и заменить sentinel
на свою успешную версию; это ожидаемое поведение. Сохранённые reprocess-параметры
также возвращаются точно: перед capture оператор должен понимать, какой режим
в действительности работает в прежнем релизе.

Если baseline material удалён или ID теперь соответствует другому URL,
команда отказывается от восстановления. Указатели не заменяются по best effort.
Снимок не предназначен для переноса на другой Docker daemon или другую БД.

## Ошибки и повтор

Exit code: `0` — capture/rollback завершён, `1` — ошибка операции,
`2` — ошибка аргументов argparse. Секретные stdout/stderr Docker и psql не
выводятся; CLI показывает только фиксированные безопасные сообщения.

При ошибке до загрузки образов постоянных изменений не происходит. При ошибке
image load контейнеры ещё не остановлены. После начала остановки любая ошибка
вызывает best-effort остановку только worker/bot/web: новый релиз автоматически
не запускается. При недоступности Docker нельзя гарантировать даже остановку;
оператор должен проверить фактическое состояние. Postgres и том сохраняются.
Устраните причину и повторите ту же единственную команду rollback. Повтор
восстановит тот же baseline, включая новые материалы, появившиеся позже;
это повторяемая операция, но не автоматическая транзакция для всего Docker.
Внешние jobs и redeploy нельзя возобновлять до завершения проверки.

## Контракт для независимого тестировщика и runner

Импорт `scripts/release.py` не выполняет I/O или Docker-команд. Публичные точки:

```python
capture(snapshot, compose_file=None, project_name=None)  # -> pathlib.Path
validate_snapshot(snapshot)  # -> dict; только чтение локальных файлов
build_restore_sql(materials, sentinel)  # -> str; чистая функция
rollback(snapshot)  # -> pathlib.Path
run_command(args, input_text=None)  # -> str stdout; граница для mock
main(argv=None)  # -> int exit code
ReleaseError  # безопасная ошибка контракта
```

`validate_snapshot()` возвращает `path`, `manifest`, `deployment`, `database`,
`compose.resolved`, `compose.rollback`. Schema материалов для SQL-builder:

```json
[
  {
    "id": 1,
    "url": "https://example.org/research",
    "status": "done",
    "processing_error": null,
    "processed_prompt_version": null,
    "resolved_prompt_version": "audience-human-shares-v5"
  }
]
```

Все шесть ключей обязательны; ID — уникальный положительный int; version/error —
строка или null. При explicit pointer resolved version должна ему равняться.
Sentinel имеет строгий формат выше. Capture возвращает также metadata колонок
схемы, поэтому тестовый deployed fixture должен содержать `schema_columns: []`
или реальные метаданные. Архив fixture — валидный uncompressed Docker-save tar
с `manifest.json`, image config файлами и image IDs, равными SHA-256 их байтов.

Отдельный автор тестов проверяет как минимум:

- отказ source-only, существующему target capture, пути внутри Git,
  ссылкам/лишним/отсутствующим файлам, неверным SHA-256 и небезопасному tar;
- отказ неправильному host/project/volume/config hash до image load/stop/UPDATE;
- разные и общие app image IDs, пустые RepoDigests, приватные env и литералы `$`;
- explicit pointer, null с legacy latest, полностью пустой baseline, материалы
  без baseline и появившиеся после capture, сохранение статусов/errors;
- SQL quoting, sentinel collision, baseline URL mismatch, отсутствие DELETE,
  блокировку и единую транзакцию, ошибки Docker/psql без утечки секретов;
- порядок load → stop только apps → SQL → up только apps с no-deps/no-build/
  pull never; отсутствие команд postgres stop/up, down, volume remove и Git;
- повтор после частичного старта, в том числе при отсутствующем app-контейнере.

Runner независимо запускает тесты и требуемые проверки репозитория. На хосте
перед внедрением отдельно проверяется реальный capture и откат на контрольном
проекте с непустой PostgreSQL БД, legacy null pointer и new-only findings.
Эта проверка должна подтвердить сохранность всех findings и тома, возврат
реальных images/config, отсутствие утечки новых версий и работу трёх каналов.
Локальные unit-тесты используют реальные файлы, SHA-256 и структуру Docker-save
tar, а команды Docker/psql подменяют. Код T3, его тесты и независимый прогон
выполнены разными агентами. Реальные Docker-операции здесь не запускались.
