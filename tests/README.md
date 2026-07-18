# Тесты

Автотесты пакета лежат в `tests/` и запускаются pytest-ом из корня репозитория:

```bash
python3 -m pytest tests/
```

Железо и брокер не нужны. Эти же тесты запускает CI при каждой сборке.

## Что покрыто

### `tests/test_protocol.py`

Разбор кадров RS-485 (`wb/dauerhaft_pro/protocol.py`): кадр корректной длины
разбирается, а каждый режим некорректного кадра даёт свой диагноз.

| Функция | Что проверяет |
|---|---|
| `test_valid_frame_parses` | кадр корректной длины (записан с живого привода) разбирается и декодируется |
| `test_truncated_frames_raise_frame_error` | короче минимума / короче заявленного байтом длины → `FrameError` |
| `test_frame_longer_than_declared_is_trimmed` | хвостовой мусор после целого кадра обрезается с предупреждением в лог |
| `test_corrupted_byte_raises_crc_error` | повреждённый байт → `CrcError` |
| `test_angle_scales_round_trip` | пересчёт градусов в сырой байт и обратно для прямой и сжатой шкалы |
| `test_command_frames_match_the_controls_table` | кадры команд байт в байт совпадают с таблицей контролов |
| `test_learning_frame_goes_to_the_learning_address` | learning-запись адреса уходит на служебный адрес 0xFF |

### `tests/test_transport.py`

Классификация ошибок RPC wb-mqtt-serial (`wb/dauerhaft_pro/transport.py`).

| Функция | Что проверяет |
|---|---|
| `test_rpc_errors_are_classified` | таймауты (−32000 «timed out», −32100, −32600) → `DeviceTimeout`; реальная неисправность порта → `TransportError`, не «устройство молчит» |

### `tests/test_device.py`

Модель привода (`wb/dauerhaft_pro/device.py`).

| Функция | Что проверяет |
|---|---|
| `test_offline_only_after_consecutive_misses` | доступность падает ровно после 3 промахов подряд; один-два промаха её не роняют |
| `test_learning_write_accepts_any_reply_address_and_keeps_the_config` | ответ на learning-запись принимается с любого адреса; адрес в конфиге не меняется |
| `test_learning_timeouts_do_not_flap_availability` | ожидаемый таймаут learning-записи не считается промахом доступности |

### `tests/test_commands.py`

Командный слой (`wb/dauerhaft_pro/commands.py`): очередь и телеметрия контролов.

| Функция | Что проверяет |
|---|---|
| `test_stop_cancels_queued_movement_and_runs_first` | стоп уходит первым и отменяет команду движения, ждавшую в очереди |
| `test_new_movement_replaces_the_queued_one` | новое движение заменяет старое в очереди; чужие ключи не трогает |
| `test_telemetry_publishes_markers_and_mirrors_reverse` | маркер «пределы не заданы» публикуется текстом; реверс зеркалит отображаемую позицию |
