from prometheus_client import Counter, Gauge, Histogram

SCANS = Counter("mf_watch_scans_total", "Сканирования папок", ["folder", "result"])
SCAN_DURATION = Histogram("mf_watch_scan_duration_seconds", "Длительность скана", ["folder"],
                          buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 300))
ASSETS_CREATED = Counter("mf_watch_assets_created_total", "Новые ассеты", ["folder"])
JOBS_CREATED = Counter("mf_watch_jobs_created_total", "Созданные задачи", ["folder"])
PARSE_ERRORS = Counter("mf_watch_parse_errors_total", "Ошибки парсинга", ["folder", "parser"])
PENDING = Gauge("mf_watch_pending_stability", "Файлы, ожидающие стабилизации", ["folder"])
BACKLOG = Gauge("mf_watch_backlog", "Задач в очереди по папке", ["folder"])
OWNED = Gauge("mf_watch_owned_folders", "Папок во владении реплики")
ARCHIVED = Counter("mf_watch_archived_total", "Архивировано исходников", ["folder", "mode"])