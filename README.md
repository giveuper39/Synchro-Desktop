<p align="center"><img src="docs/assets/synchro.svg" alt="synchro · Синхро" width="100%"></p>
<p align="center">
  <a href="https://github.com/giveuper39/syncro-desktop/releases/latest">Скачать для Windows</a> ·
  <a href="https://github.com/giveuper39/syncro-android/releases/latest">Скачать для Android</a>
</p>

Синхро связывает телефон и компьютер в одной сети. Можно уйти от ПК, продолжить фильм на телефоне и вернуть просмотр обратно по паузе.

<p align="center"><img src="docs/assets/player.png" alt="Полноэкранный просмотр видео с компьютера" width="65%"> <img src="docs/assets/mini-player.png" alt="Видео в системном мини-плеере поверх буфера обмена" width="28%"></p>
<p align="center"><sub>Реальные экраны текущей версии 0.3.0.</sub></p>

### Сейчас

- Видео и плейлисты из PotPlayer с сохранением позиции, озвучки и субтитров.
- Последнее копирование между телефоном и ПК, в обе стороны.
- Передача ссылок между браузерами.
- Системный мини-плеер Android и обновления из приложения.

### Подключение

1. Распакуйте Windows ZIP и запустите **Запустить Синхро.cmd**.
2. Подключите телефон по QR в открывшемся мастере.
3. Откройте видео в PotPlayer и нажмите **Продолжить на телефоне**.

Для автоматического буфера нужен системный QR-сканер беспроводной отладки Android. Для видео и ручного обмена достаточно обычного QR приложения. Python и ADB включены в Windows ZIP.

### 0.4.0 — в работе

Выбор источника просмотра, стандартный Медиаплеер Windows, YouTube из открытого браузера и новый интерфейс. Скриншоты новой версии появятся вместе с готовой сборкой.

### Исходники

Это Windows-сервис и расширение браузера. Android-приложение находится в [отдельном репозитории](https://github.com/giveuper39/syncro-android).

```sh
python -m pip install -e server
python -m unittest discover -s tests
```

[Выпуск версий](docs/RELEASING.md) · [Работа с видео](docs/PLAYER.md) · [Лицензии](THIRD_PARTY_NOTICES.md)

Собственный код — MIT. PotPlayer не входит в дистрибутив.
