# 📖 Документация по работе с хранилищами в MediaFarm

Подсистема хранилищ (Storage) является фундаментом MediaFarm, обеспечивая унифицированный асинхронный доступ к медиафайлам независимо от их физического расположения. Документация основана на анализе исходного кода проекта.

---

## 1. Архитектура подсистемы хранилищ

### 1.1. Ключевые компоненты

**Storage (Хранилище)**
- Логическая сущность в базе данных PostgreSQL
- Описывает точку доступа к файловой системе или объектному хранилищу
- Имеет уникальное имя (`name`), тип (`kind`), базовый URI (`base_uri`) и настройки безопасности

**Backend (Бэкенд)**
- Асинхронный драйвер, реализующий интерфейс `Backend` из `mf_core.storage.base`
- Знает, как выполнять файловые операции для конкретного типа хранилища
- Реализует 6 обязательных примитивов: `stat`, `list`, `exists`, `_reader`, `_writer`, `_publish`

**StorageRegistry (Реестр хранилищ)**
- Глобальный кэш в памяти сервиса (UI, Worker, Watcher)
- Загружает активные хранилища из БД и кеширует их на 30 секунд (TTL)
- Разрешает секреты и переиспользует соединения (connection pooling)
- Автоматически обновляется при изменении хранилищ через API

**StorageRow**
- Легковесная копия ORM-модели `Storage` (dataclass)
- Используется бэкендами для доступа к настройкам без зависимости от SQLAlchemy-сессии

### 1.2. Жизненный цикл операции с файлом

```
1. Пользователь/система запрашивает операцию с URI: s3://bucket/path/file.mp4
2. StorageRegistry.resolve(uri) находит подходящее хранилище по префиксу
3. Registry извлекает StorageRow и разрешает секреты через SecretResolver
4. Создается/переиспользуется экземпляр Backend (например, S3Backend)
5. Backend выполняет операцию через нативный протокол (S3 API, NFS, POSIX и т.д.)
6. Результат возвращается вызывающему коду
```

---

## 2. Модель данных хранилища (Storage)

Таблица `storage` в PostgreSQL содержит следующие поля:

| Поле | Тип | Обязательное | Описание |
|------|-----|:------------:|----------|
| `id` | `UUID` | ✅ | Первичный ключ, генерируется автоматически |
| `name` | `VARCHAR(128)` | ✅ | **Уникальное** имя хранилища (например, `primary_s3`, `nas_archive`). Используется в пресетах и шаблонах путей |
| `kind` | `VARCHAR(16)` | ✅ | Тип бэкенда. Допустимые значения: `local`, `nfs`, `smb`, `s3`, `ftp` |
| `base_uri` | `TEXT` | ✅ | Корневой URI хранилища. Определяет "точку входа" для всех операций |
| `mount_path` | `TEXT` | ❌ | Абсолютный путь в файловой системе хоста. Критически важен для `local` и `nfs` для защиты от Directory Traversal |
| `options` | `JSONB` | ❌ | Словарь дополнительных параметров, специфичных для бэкенда (например, `endpoint_url` для S3, `port` для NFS) |
| `secret_ref` | `VARCHAR(256)` | ❌ | Ссылка на менеджер секретов (например, `vault:secret/data/mediafarm/s3` или `k8s:my-secret`). Используется для получения учетных данных |
| `read_only` | `BOOLEAN` | ❌ | Если `true`, система блокирует все операции записи, удаления и перемещения. По умолчанию: `false` |
| `enabled` | `BOOLEAN` | ❌ | Если `false`, хранилище игнорируется реестром и не может быть использовано. По умолчанию: `true` |
| `created_at` | `TIMESTAMP` | ✅ | Время создания записи |
| `updated_at` | `TIMESTAMP` | ✅ | Время последнего обновления |

**Ограничения:**
- `name` должен быть уникальным (UNIQUE constraint)
- `kind` должен быть одним из: `local`, `nfs`, `smb`, `s3`, `ftp` (CHECK constraint)

---

## 3. Поддерживаемые типы хранилищ

### 3.1. Local (Локальная файловая система)

**Назначение:** Диски, непосредственно подключенные к воркеру или UI (SSD, HDD, RAID-массивы).

**Реализация:** `LocalBackend` в `packages/mf-core/mf_core/storage/local.py`

**Конфигурация:**
```json
{
  "name": "worker_scratch",
  "kind": "local",
  "base_uri": "file:///data/scratch",
  "mount_path": "/data/scratch",
  "options": {},
  "secret_ref": null,
  "read_only": false,
  "enabled": true
}
```

**Ключевые особенности:**
- **`base_uri`**: Может быть в формате `file:///path/to/dir` или просто `/path/to/dir`
- **`mount_path`**: **Обязательно** для валидации безопасности. Должен совпадать с реальным путем в файловой системе
- **Атомарные операции**: Использует `os.replace()` для атомарного переименования временных файлов в финальные
- **Защита от Directory Traversal**:
    - Вычисляет канонический путь через `Path.resolve()`
    - Проверяет, что целевой путь начинается с `mount_path`
    - Дополнительно проверяет `allowlist` из настроек (`storage_local_allow_paths`)
- **Асинхронность**: Все блокирующие операции (`stat`, `list`, `read`, `write`) обернуты в `asyncio.to_thread()`
- **Поддержка докачки**: При скачивании проверяет размер существующего `.part` файла и продолжает с нужного offset

**Операции:**
- `stat`: `Path.stat()` → возвращает размер, mtime, inode
- `list`: `os.scandir()` с рекурсивным обходом через `stack`
- `_reader`: `aiofiles.open(path, "rb")` + `read(chunk_size)`
- `_writer`: `aiofiles.open(path, "wb")` + `write()` + `fsync()`
- `_publish`: `os.replace(tmp, final)` — атомарная операция
- `delete`: `Path.unlink()` для файлов, `shutil.rmtree()` для директорий
- `mkdirs`: `Path.mkdir(parents=True, exist_ok=True)`
- `move`: `shutil.move()` с созданием родительских директорий

---

### 3.2. NFS (Network File System через libnfs)

**Назначение:** Сетевые файловые системы без необходимости монтирования на уровне ОС.

**Реализация:** `NfsBackend` в `packages/mf-core/mf_core/storage/nfs.py`

**Требования:**
- Python-пакет: `pip install libnfs`
- Системная библиотека: `libnfs-dev` (Debian/Ubuntu) или эквивалент

**Конфигурация:**
```json
{
  "name": "nas_nfs_archive",
  "kind": "nfs",
  "base_uri": "nfs://192.168.1.50/volume1/media_archive",
  "mount_path": null,
  "options": {
    "port": 2049
  },
  "secret_ref": null,
  "read_only": false,
  "enabled": true
}
```

**Ключевые особенности:**
- **Нативный протокол**: Использует C-библиотеку `libnfs` для прямого общения с NFS-сервером по порту 2049 (по умолчанию)
- **Не требует монтирования**: В отличие от OS-level NFS, MediaFarm подключается напрямую через сеть
- **URI формат**: `nfs://<host>/<export>/<path>`
    - `host`: IP или hostname NFS-сервера
    - `export`: Имя экспорта (например, `volume1`, `share`)
    - `path`: Опциональный путь внутри экспорта
- **Кэширование соединений**: Глобальный потокобезопасный кэш `_CLIENTS: dict[str, NFS]` переиспользует клиенты
- **Асинхронность**: Все вызовы `libnfs` обернуты в `asyncio.to_thread()`

**Операции через libnfs:**
- `stat`: `client.stat(path)` → возвращает `st_size`, `st_mtime`, `st_mode`, `st_ino`
- `list`: `client.opendir()` + `client.readdir()` + `client.stat()` для каждого элемента
- `_reader`: `client.open(path, os.O_RDONLY)` + `client.lseek()` (для offset) + `client.read()`
- `_writer`: `client.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)` + `client.write()` + `client.fsync()`
- `_publish`: `client.rename(tmp, final)` — атомарная операция на стороне NFS-сервера
- `delete`: `client.unlink()` для файлов, `client.rmdir()` для директорий
- `mkdirs`: Рекурсивное создание через `client.mkdir()` с проверкой существования через `client.stat()`

**Fallback в реестре:**
Если `libnfs` не установлен (`ImportError`), `StorageRegistry` автоматически заменяет `NfsBackend` на `LocalBackend`. Это приведет к ошибкам, если путь не смонтирован локально.

---

### 3.3. S3 (AWS S3, MinIO, Ceph RGW, Yandex Object Storage)

**Назначение:** Объектные хранилища с высокой надежностью и параллелизмом.

**Реализация:** `S3Backend` в `packages/mf-core/mf_core/storage/s3.py`

**Конфигурация:**
```json
{
  "name": "release_minio",
  "kind": "s3",
  "base_uri": "s3://media-releases/2026",
  "mount_path": null,
  "options": {
    "endpoint_url": "http://minio.internal:9000",
    "region": "us-east-1",
    "addressing_style": "path"
  },
  "secret_ref": "env:MINIO_CREDS",
  "read_only": false,
  "enabled": true
}
```

**Ключевые особенности:**
- **Библиотека**: Использует `s3fs` в асинхронном режиме (`asynchronous=True`)
- **URI формат**: `s3://<bucket>/<prefix>`
    - `bucket`: Имя бакета (обязательно)
    - `prefix`: Опциональный префикс внутри бакета
- **Избегает файловых дескрипторов**: Не использует `fs.open()` из-за проблем с async/sync mismatch в `s3fs`
- **Атомарные операции**:
    - Чтение: `fs.cat_file()` → возвращает bytes целиком, затем yield чанками
    - Запись: Аккумуляция чанков в память (до 64 МБ) или временный файл → `fs.pipe_file()` (для маленьких) или `fs.put_file()` (для больших)
    - Публикация: `fs._cp_file()` + `fs._rm_file()` (так как в S3 нет нативного rename)
- **Connection Pool**: Настраивается через `storage_s3_concurrency` в настройках
- **Retry логика**: `retries: {"max_attempts": 5, "mode": "standard"}`

**Операции через s3fs:**
- `stat`: `fs._info(key)` → возвращает `size`, `LastModified`, `ETag`, `StorageClass`
- `list`: `fs._ls(key, detail=True)` с рекурсивным обходом через `stack`
- `_reader`: `fs._cat_file(key, start=offset)` → chunked yield
- `_writer`:
    - Маленькие файлы (≤64 МБ): `fs._pipe_file(key, bytes)`
    - Большие файлы: спулинг в `tempfile.NamedTemporaryFile` → `fs._put_file(tmp_path, key)`
- `_publish`: `fs._cp_file(tmp_key, final_key)` + `fs._rm_file(tmp_key)` + `fs.invalidate_cache()`
- `delete`: `fs._rm_file(key)` + `fs.invalidate_cache()`

**Нормализация путей:**
- Метод `_key(path)` преобразует относительный путь в полный ключ S3: `<bucket>/<prefix>/<path>`
- Метод `_rel(key)` извлекает относительный путь из полного ключа

---

### 3.4. SMB / CIFS (Windows Shares, Samba)

**Назначение:** Сетевые папки Windows/Samba.

**Реализация:** `SmbBackend` в `packages/mf-core/mf_core/storage/smb.py`

**Конфигурация:**
```json
{
  "name": "camera_ingest",
  "kind": "smb",
  "base_uri": "smb://nas.local/camera_drops",
  "mount_path": null,
  "options": {
    "port": 445
  },
  "secret_ref": "vault:secret/data/infra/nas_camera",
  "read_only": true,
  "enabled": true
}
```

**Формат секрета:**
```json
{
  "username": "media_user",
  "password": "SuperSecretPassword",
  "domain": "CORP"
}
```

**Ключевые особенности:**
- **Библиотека**: Использует `smbprotocol`
- **URI формат**: `smb://<server>/<share>/<path>`
- **Аутентификация**: Поддерживает NTLM и Kerberos (при правильной настройке окружения)
- **Порт**: По умолчанию 445, можно переопределить в `options`

---

### 3.5. FTP / FTPS

**Назначение:** Интеграция с устаревшими системами вещания или камерами.

**Реализация:** `FtpBackend` в `packages/mf-core/mf_core/storage/ftp.py`

**Конфигурация:**
```json
{
  "name": "legacy_ftp",
  "kind": "ftp",
  "base_uri": "ftp://ftp.example.com/incoming",
  "mount_path": null,
  "options": {
    "tls": true,
    "port": 21
  },
  "secret_ref": "env:FTP_CREDS",
  "read_only": false,
  "enabled": true
}
```

**Формат секрета:**
```json
{
  "username": "ftp_user",
  "password": "ftp_password"
}
```

**Ключевые особенности:**
- **Протокол**: FTP или FTPS (с TLS)
- **Использование**: В основном для ingest-а с камер или интеграции с legacy-системами

---

## 4. Механизм тестирования хранилищ

Функция `test_storage` в `services/ui/ui/services.py` проводит комплексную диагностику при нажатии кнопки "Проверить" в UI.

### 4.1. Алгоритм тестирования

```python
async def test_storage(row: Storage, writable_probe: bool = True) -> dict[str, Any]:
    # 1. Разрешение секретов
    creds = SecretResolver().resolve(row.secret_ref)
    
    # 2. Инициализация бэкенда
    be = BUILDERS[row.kind](row, creds)
    
    # 3. Тест на чтение (List)
    items = []
    async for e in be.list("", recursive=False):
        items.append({"path": e.path, "size": e.size, "is_dir": e.is_dir})
        if len(items) >= 5:
            break
    
    # 4. Тест на запись (Probe) — если read_only == false
    if writable_probe and not row.read_only:
        probe = f".mf-probe-{uuid.uuid4().hex[:8]}"
        await be.write_bytes(probe, b"mediafarm", overwrite=True)
        await be.delete(probe)
        result["writable"] = True
    
    # 5. Замер метрик
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    result["ok"] = True
```

### 4.2. Возвращаемые данные

```json
{
  "ok": true,
  "kind": "s3",
  "entries": [
    {"path": "file1.mp4", "size": 1048576, "is_dir": false},
    {"path": "subdir", "size": 0, "is_dir": true}
  ],
  "writable": true,
  "latency_ms": 245.3
}
```

При ошибке:
```json
{
  "ok": false,
  "error": "ClientError: Access denied",
  "kind": "s3",
  "entries": [],
  "writable": null,
  "latency_ms": 120.5
}
```

### 4.3. Анализ ошибок и подсказки

Функция `_storage_error_hint` в `pages.py` классифицирует ошибки и возвращает понятные подсказки:

| Ошибка | Подсказка |
|--------|-----------|
| `"нет хранилища для uri"` | "проверьте base_uri: запись хранилища должна покрывать этот URI" |
| `"некорректный s3 uri"` | "в base_uri нужен бакет: s3://имя-бакета" |
| `"nosuchbucket"` | "бакет не существует — проверьте имя в base_uri" |
| `"invalidaccesskeyid"`, `"signaturedoesnotmatch"`, `"access denied"` | "проверьте access_key/secret_key в secret_ref" |
| `"connection refused"`, `"connect call failed"` | "endpoint недоступен — проверьте endpoint_url и порт в options" |
| `"getaddrinfo"`, `"name or service not known"` | "DNS не резолвит хост — проверьте endpoint_url" |
| `"timeout"`, `"timed out"` | "таймаут — проверьте сеть и фаервол до хранилища" |
| `"no such file"`, `"not a directory"`, `"permission denied"` | "проверьте mount_path и права доступа" |

---

## 5. Безопасность и управление секретами

### 5.1. SecretResolver

MediaFarm **никогда** не хранит пароли или ключи доступа в открытом виде в таблице `storage`. Вместо этого используется поле `secret_ref`.

**Формат `secret_ref`:**
- `env:VAR_NAME` — переменная окружения
- `vault:secret/data/path` — HashiCorp Vault
- `k8s:namespace/secret-name` — Kubernetes Secrets
- `file:/path/to/secret.json` — локальный файл

**Алгоритм работы:**
1. При инициализации бэкенда вызывается `SecretResolver().resolve(secret_ref)`
2. Резолвер извлекает учетные данные из внешнего хранилища
3. Возвращает словарь вида `{"access_key": "...", "secret_key": "..."}`
4. Словарь используется только в оперативной памяти для аутентификации

### 5.2. Форматы секретов для разных типов хранилищ

**S3:**
```json
{
  "access_key": "AKIAIOSFODNN7EXAMPLE",
  "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
  "session_token": "optional_session_token"
}
```

**SMB:**
```json
{
  "username": "media_user",
  "password": "SuperSecretPassword",
  "domain": "CORP"
}
```

**FTP:**
```json
{
  "username": "ftp_user",
  "password": "ftp_password"
}
```

### 5.3. Защита от Directory Traversal (Local/NFS)

Для `local` и `nfs` бэкендов реализована строгая валидация:

```python
def _abs(self, path: str) -> Path:
    root = self.root.resolve()
    target = (root / path.lstrip("/")).resolve()
    
    # Проверка 1: путь не выходит за пределы root
    if target != root and not str(target).startswith(str(root) + os.sep):
        raise StorageAuthError(f"выход за пределы хранилища: {path!r}")
    
    # Проверка 2: путь в allowlist (если настроен)
    allow = get_settings().storage_local_allow_paths
    if allow and not any(
        str(target).startswith(str(Path(a).resolve())) for a in allow
    ):
        raise StorageAuthError(f"путь не в allowlist: {target}")
    
    return target
```

---

## 6. Шаблоны путей (Path Templating)

При настройке шага `publish` в пресете или архивации в `WatchFolder`, путь формируется динамически через функцию `render` из `mf_core.storage.template`.

### 6.1. Поддерживаемые переменные

| Переменная | Описание | Пример |
|------------|----------|--------|
| `{yyyy}` | Год из даты создания задачи/актива | `2026` |
| `{mm}` | Месяц (с ведущим нулем) | `11` |
| `{dd}` | День (с ведущим нулем) | `18` |
| `{asset.name}` | Полное имя исходного файла | `interview_raw.mov` |
| `{asset.stem}` | Имя файла без расширения | `interview_raw` |
| `{asset.ext}` | Расширение файла (с точкой) | `.mov` |
| `{ts}` | Unix-таймстамп (для избежания коллизий) | `1700312345` |
| `{preset.name}` | Имя используемого пресета | `proxy_1080p_nvenc` |

### 6.2. Примеры шаблонов

**Шаблон:**
```
{yyyy}/{mm}/{asset.stem}_{preset.name}{asset.ext}
```

**Результат:**
```
2026/11/interview_raw_proxy_1080p_nvenc.mp4
```

**Шаблон с таймстампом:**
```
{yyyy}/{mm}/{dd}/{asset.stem}_{ts}{asset.ext}
```

**Результат:**
```
2026/11/18/interview_raw_1700312345.mp4
```

### 6.3. Контекст шаблона

Функция `build_context` в `mf_core.storage.template` формирует словарь переменных:

```python
def build_context(asset_uri: str, asset_meta: dict, preset_name: str) -> dict:
    now = utcnow()
    stem = Path(asset_uri).stem
    ext = Path(asset_uri).suffix
    
    return {
        "yyyy": now.strftime("%Y"),
        "mm": now.strftime("%m"),
        "dd": now.strftime("%d"),
        "ts": str(int(now.timestamp())),
        "asset": {
            "name": Path(asset_uri).name,
            "stem": stem,
            "ext": ext,
        },
        "preset": {"name": preset_name},
        **asset_meta,  # Дополнительные поля из meta
    }
```

---

## 7. Режимы работы и ограничения

### 7.1. Read-Only режим

Если для хранилища установлен флаг `read_only: true`:

**Разрешенные операции:**
- `stat` — получение метаданных файла
- `list` — листинг директорий
- `exists` — проверка существования
- `download` — скачивание файлов

**Заблокированные операции:**
- `upload` — загрузка файлов
- `delete` — удаление файлов/директорий
- `move` — перемещение файлов
- `mkdirs` — создание директорий
- `write_bytes` — запись байтов

**Реализация:**
```python
def _guard_write(self) -> None:
    if self.row.read_only:
        raise StorageError(f"хранилище {self.row.name} помечено read_only")
```

Метод `_guard_write()` вызывается в начале всех операций записи.

### 7.2. Атомарность записи

Все бэкенды реализуют паттерн **"Write to Temporary, then Publish"**:

1. Файл всегда сначала пишется во временный путь с суффиксом:
   ```python
   def _tmp_name(self, final: str) -> str:
       return f"{final}{self.tmp_suffix}-{uuid.uuid4().hex[:12]}"
   ```
   Пример: `file.mp4.mf-tmp-a1b2c3d4e5f6`

2. После успешной записи и проверки контрольной суммы вызывается `_publish`:
    - **Local/NFS**: `os.replace(tmp, final)` — атомарная операция POSIX
    - **S3**: `fs._cp_file(tmp_key, final_key)` + `fs._rm_file(tmp_key)` — copy + delete

3. Это гарантирует, что другие процессы (или сканер `watcher`) никогда не увидят "наполовину записанный" файл под финальным именем.

### 7.3. Докачка (Resume)

Метод `download` в `base.py` поддерживает докачку:

```python
async def download(self, path: str, dst: str | Path, *, resume: bool = True, ...):
    entry = await self.stat(path)
    part = Path(str(dst) + self.tmp_suffix)
    
    offset = 0
    if resume and part.exists():
        offset = part.stat().st_size
        if offset > entry.size:  # источник изменился — начинаем заново
            offset = 0
    
    if offset and offset == entry.size:  # уже скачано целиком
        os.replace(part, dst)
        return TransferResult(str(dst), entry.size, offset, ...)
    
    # Продолжаем запись с offset
    async with aiofiles.open(part, "ab" if offset else "wb") as f:
        if offset:
            await f.seek(offset)
        async for chunk in self._reader(path, offset):
            await f.write(chunk)
```

### 7.4. Отмена передачи

Все операции передачи поддерживают отмену через `asyncio.Event`:

```python
def _check_cancel(cancel: asyncio.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledByUser("передача отменена")
```

Проверка вызывается в циклах чтения/записи чанков.

---

## 8. Практические примеры конфигурации

### 8.1. Локальный SSD для временной обработки (Scratch)

```json
{
  "name": "worker_scratch",
  "kind": "local",
  "base_uri": "file:///data/scratch",
  "mount_path": "/data/scratch",
  "options": {},
  "secret_ref": null,
  "read_only": false,
  "enabled": true
}
```

**Использование:** Временное хранилище для промежуточных файлов во время транскодирования.

### 8.2. NFS-архив через libnfs

```json
{
  "name": "nas_nfs_archive",
  "kind": "nfs",
  "base_uri": "nfs://10.0.0.20/export/media/final",
  "mount_path": null,
  "options": {
    "port": 2049
  },
  "secret_ref": null,
  "read_only": false,
  "enabled": true
}
```

**Использование:** Долгосрочное хранение готовых релизов на сетевом хранилище.

### 8.3. MinIO (S3-совместимое) для готовых релизов

```json
{
  "name": "release_minio",
  "kind": "s3",
  "base_uri": "s3://media-releases/2026",
  "mount_path": null,
  "options": {
    "endpoint_url": "http://minio.internal:9000",
    "region": "us-east-1",
    "addressing_style": "path"
  },
  "secret_ref": "env:MINIO_CREDS",
  "read_only": false,
  "enabled": true
}
```

**Использование:** Основное хранилище для финальных результатов обработки.

### 8.4. SMB-шара для ingest-а с камер

```json
{
  "name": "camera_ingest",
  "kind": "smb",
  "base_uri": "smb://nas.local/camera_drops",
  "mount_path": null,
  "options": {
    "port": 445
  },
  "secret_ref": "vault:secret/data/infra/nas_camera",
  "read_only": true,
  "enabled": true
}
```

**Использование:** Только для чтения исходных файлов с камер.

### 8.5. FTP для legacy-систем

```json
{
  "name": "legacy_ftp",
  "kind": "ftp",
  "base_uri": "ftp://ftp.example.com/incoming",
  "mount_path": null,
  "options": {
    "tls": true,
    "port": 21
  },
  "secret_ref": "env:FTP_CREDS",
  "read_only": false,
  "enabled": true
}
```

**Использование:** Интеграция с устаревшими системами вещания.

---

## 9. Диагностика и устранение неполадок

### 9.1. Частые ошибки и решения

| Симптом | Вероятная причина | Решение |
|---------|-------------------|---------|
| **"нет хранилища для URI"** | URI актива не начинается с `base_uri` настроенного хранилища | Проверьте, что `base_uri` является общим префиксом для всех файлов в этом хранилище |
| **"некорректный s3 uri"** | Отсутствует имя бакета в URI | Используйте формат `s3://имя-бакета`, а не просто `s3://` |
| **"бакет не существует"** | Опечатка в имени бакета или неправильный `endpoint_url` | Проверьте имя бакета в панели управления облачным провайдером |
| **"проверьте access_key/secret_key"** | Ошибка 403 Forbidden или InvalidSignature | Убедитесь, что `secret_ref` указывает на актуальные учетные данные |
| **"endpoint недоступен"** | Таймаут соединения или ошибка DNS | Проверьте сетевую связность (ping, telnet) от подов/контейнеров MediaFarm до хранилища |
| **"путь не в allowlist"** | Сработала защита от Directory Traversal | Добавьте целевой путь в `storage_local_allow_paths` в конфигурации приложения |
| **"libnfs не установлен"** | Отсутствует Python-пакет `libnfs` | Выполните `pip install libnfs` и установите системную библиотеку `libnfs-dev` |

### 9.2. Проверка сетевой связности

**Для S3:**
```bash
# Проверка DNS
nslookup minio.internal

# Проверка порта
telnet minio.internal 9000

# Проверка через curl
curl -I http://minio.internal:9000
```

**Для NFS:**
```bash
# Проверка порта NFS
nmap -p 2049 192.168.1.50

# Проверка экспортов
showmount -e 192.168.1.50
```

**Для SMB:**
```bash
# Проверка порта SMB
nmap -p 445 nas.local

# Проверка шар
smbclient -L //nas.local -U media_user
```

### 9.3. Логирование

Все операции с хранилищами логируются через `get_logger("mf.storage")`. Примеры логов:

```
INFO  mf.storage: S3Backend initialized for bucket=media-releases
DEBUG mf.storage: Listing s3://media-releases/2026/
ERROR mf.storage: S3 stat failed: ClientError: Access denied
INFO  mf.storage: Upload completed: file.mp4 (1048576 bytes) in 2.3s
```

---

## 10. Связь с другими компонентами

### 10.1. WatchFolder (Папка наблюдения)

При создании папки наблюдения вы выбираете `storage_id` и указываете относительный `path`:

```json
{
  "name": "camera_drops",
  "storage_id": "uuid-of-camera_ingest",
  "path": "2026/11",
  "recursive": true,
  "enabled": true,
  "poll_interval_s": 30,
  "stability_s": 60,
  "include": ["*.mp4", "*.mov"],
  "exclude": ["*.part", ".*"]
}
```

Система будет сканировать `base_uri` + `path` этой папки через соответствующий бэкенд.

### 10.2. Preset (Пресет)

В шаге `publish` указывается **имя хранилища** (не ID):

```json
{
  "id": "publish",
  "storage": "release_minio",
  "path": "{yyyy}/{mm}/{asset.stem}_{preset.name}{asset.ext}",
  "on_conflict": "skip"
}
```

`StorageRegistry` найдет хранилище по имени и подставит правильный `base_uri`.

### 10.3. Worker

При выполнении задачи воркер запрашивает у Registry бэкенд по URI файла:

```python
backend, rel_path = registry.resolve("s3://media-releases/2026/11/file.mp4")
# backend = S3Backend instance
# rel_path = "2026/11/file.mp4"
```

Registry автоматически выбирает нужный драйвер (S3, Local и т.д.) и инжектит секреты.

### 10.4. Asset (Актив)

Каждый актив привязан к хранилищу через `storage_id` и хранит полный `uri`:

```json
{
  "id": "uuid-of-asset",
  "storage_id": "uuid-of-release_minio",
  "uri": "s3://media-releases/2026/11/file.mp4",
  "size": 1048576,
  "state": "done"
}
```

---

## 11. Производительность и оптимизация

### 11.1. Размер чанка

Размер чанка для чтения/записи настраивается через `storage_chunk_mb` в конфигурации:

```python
self.chunk = max(1, s.storage_chunk_mb) * 1024 * 1024
```

**Рекомендации:**
- Для S3: 8–16 МБ (оптимально для multipart upload)
- Для Local/NFS: 1–4 МБ (меньше накладные расходы на системные вызовы)

### 11.2. Connection Pool для S3

Количество одновременных соединений к S3 настраивается через `storage_s3_concurrency`:

```python
config_kwargs={
    "max_pool_connections": get_settings().storage_s3_concurrency,
    ...
}
```

**Рекомендации:**
- Для одного воркера: 20–50 соединений
- Для высоконагруженных систем: 100+ соединений

### 11.3. Кэширование в S3

S3Backend инвалидирует кэш `s3fs` после операций записи:

```python
fs.invalidate_cache(key)
```

Это гарантирует, что последующие операции `stat` и `list` вернут актуальные данные.

### 11.4. Спулинг для больших файлов в S3

Файлы >64 МБ автоматически спулятся во временный файл перед загрузкой:

```python
_SPILL_LIMIT = 64 * 1024 * 1024  # 64 МБ

if len(buf) > _SPILL_LIMIT:
    # Переполнение: спулим в temp-файл
    tmp_file = tempfile.NamedTemporaryFile(prefix="mf-s3-", suffix=".part", delete=False)
    tmp_file.write(buf)
    spilled = True
```

Это предотвращает исчерпание оперативной памяти при загрузке больших файлов.

---

## 12. Расширение подсистемы хранилищ

### 12.1. Добавление нового типа бэкенда

1. Создайте новый файл в `packages/mf-core/mf_core/storage/`, например, `gcs.py`
2. Реализуйте класс, наследующий от `Backend`:

```python
class GcsBackend(Backend):
    kind = "gcs"
    
    async def stat(self, path: str) -> Entry:
        ...
    
    async def list(self, path: str = "", recursive: bool = False) -> AsyncIterator[Entry]:
        ...
    
    # ... остальные обязательные методы
```

3. Зарегистрируйте бэкенд в `registry.py`:

```python
from mf_core.storage.gcs import GcsBackend

BUILDERS["gcs"] = GcsBackend
```

4. Добавьте проверку в `models.py`:

```python
STORAGE_KINDS = ("local", "nfs", "smb", "s3", "ftp", "gcs")
```

### 12.2. Кастомные операции

Если вашему бэкенду нужны специфичные операции, вы можете добавить их как дополнительные методы:

```python
class S3Backend(Backend):
    async def generate_presigned_url(self, path: str, expires_in: int = 3600) -> str:
        fs = await self.fs()
        return fs.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': self._key(path)},
            ExpiresIn=expires_in
        )
```

---

## 13. Заключение

Подсистема хранилищ MediaFarm обеспечивает:

✅ **Унифицированный интерфейс** для работы с различными типами хранилищ  
✅ **Высокую производительность** через асинхронность и connection pooling  
✅ **Надежность** через атомарные операции и паттерн "write-to-temp-then-publish"  
✅ **Безопасность** через защиту от Directory Traversal и управление секретами  
✅ **Гибкость** через шаблоны путей и динамическое разрешение хранилищ  
✅ **Расширяемость** через простой интерфейс `Backend`

Для получения дополнительной информации см. исходный код в `packages/mf-core/mf_core/storage/` и примеры использования в `services/ui/ui/services.py`.