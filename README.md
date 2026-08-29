# KRC — Keenetic Route Manager Pro

Десктоп-приложение для управления Keenetic по Telnet: статические маршруты, переадресация портов, системный монитор.

## Запуск

```bash
pip install telnetlib3
python keenetic.py
```

Telnet нужно включить на роутере: веб-интерфейс → Системные настройки → Опции.

Локальные настройки подключения сохраняются в `keenetic_settings.json` и в git не попадают.
