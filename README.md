# wb-dauerhaft-pro

Пакет устанавливает драйвер для управления приводами Dauerhaft PRO RS-485 на контроллерах Wiren Board.

Драйвер работает как скрипт wb-rules и обменивается с приводами через RS-485 при помощи wb-mqtt-serial и MQTT-RPC. Поддерживается протокол «Профкарниз Dauerhaft PRO RS-485 v2.3», параметры линии по умолчанию: 9600 8N1.

**Установка:**
`apt install wb-dauerhaft-pro`

**Удаление:**
`apt remove wb-dauerhaft-pro`
`systemctl restart wb-rules`