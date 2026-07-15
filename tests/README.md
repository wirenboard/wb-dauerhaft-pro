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

### `tests/test_config.py`

Чтение конфига (`wb/dauerhaft_pro/config.py`).

| Функция | Что проверяет |
|---|---|
| `test_config_parses_with_unit_conversion` | поля устройства читаются корректно, интервал из мс в конфиге превращается в секунды |
| `test_duplicate_device_id_rejected` | два одинаковых `device_id` → `ConfigError` (проверка, которую схема выразить не может) |

### `tests/test_mqtt.py`

MQTT-обвязка (`wb/dauerhaft_pro/mqtt.py`).

| Функция | Что проверяет |
|---|---|
| `test_unchanged_retained_values_are_not_republished` | неизменённые retained-значения не публикуются повторно; изменённые уходят с retain |
| `test_republish_restores_every_topic` | `republish()` переиздаёт все retained-топики устройства (восстановление после рестарта брокера) |
