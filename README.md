# wb-dauerhaft-pro

MQTT-драйвер Wiren Board для приводов штор и жалюзи **Dauerhaft PRO RS-485**.

> Пока это **скелет пакета**: базовая сборка deb через Jenkins и схема для
> редактора конфигураций (wb-mqtt-confed). Сам драйвер добавляется отдельными
> изменениями.

## Состав

- `Jenkinsfile` — сборка на WB Jenkins (`buildDebArchAll`, пакет `arch: all`).
- `setup.py` + `wb_dauerhaft_pro/` — Python-пакет (собирается через pybuild).
- `debian/` — метаданные пакета (`control`, `rules`, `changelog`, `install`, …).
- `configs/wb-dauerhaft-pro.schema.json` — схема для wb-mqtt-confed; создаёт
  страницу конфигурации в веб-интерфейсе.

## Сборка

Пакет собирается автоматически на
[jenkins.wirenboard.com](https://jenkins.wirenboard.com) при пуше в ветку или PR.
Локально (в WB-окружении):

```sh
dpkg-buildpackage -us -uc -b
```

## Конфигурация

После установки в веб-интерфейсе контроллера появляется страница конфигурации
(**Настройки → Конфигурационные файлы**). Конфигурация хранится в
`/etc/wb-dauerhaft-pro.conf`.
