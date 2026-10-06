# improv-video

Insta360 Ace Pro 2 → YouTube: вставил карту — на YouTube появляется смонтированный ролик дня
(«Тренировка ДД.ММ.ГГГГ» или «Занятие ДД.ММ.ГГГГ»).

План: см. документ «Автоматизация: Insta360 Ace Pro 2 → YouTube (v3)».

## Состояние

Этап 1 (ядро), в работе. Готово:

- поиск карты и клипов `DCIM/Camera*/VID_*.mp4`, день съёмки с границей 04:00;
- склейка кусков одной записи, журнал состояния в SQLite, «часть 2» при досъёмке;
- проверка места и копирование в архив с проверкой размера;
- автояркость (замер раз в 2 с, поправка в стопах до LUT, ±0.5 стопа), LUT, 10 бит, BT.709;
- звук: каждый клип точно по длине видео, шумодав (afftdn / RNNoise / DeepFilterNet), loudnorm −14 LUFS;
- HEVC Main10 с выбором кодировщика по железу, склейка без перекодирования.

- загрузка на YouTube «по ссылке» с повторами, вход через браузер, токен в Keychain;
- куски записи Ace Pro 2 (одно время в имени, разные номера), копии Finder «… 2.mp4» отбрасываются;
- автояркость одной поправкой на день (ровный свет в помещении).

- приложение в менюбаре (`improv_video/app`): слежение за флешками, вопрос «Что снимали?»,
  ручная загрузка через YouTube Studio до аудита, автоматическая после;
- сборка `.app` в GitHub Actions (`.github/workflows/build-mac.yml`) с ffmpeg, RNNoise и DeepFilterNet внутри,
  ad-hoc подписью и самопроверкой.

## Запуск из командной строки

Нужны Python 3.11+ и ffmpeg в PATH.

```
pip install -r requirements.txt
python -m improv_video login --client-secrets ~/Downloads/client_secret.json
python -m improv_video --lut ilog.cube import /Volumes/CARD --kind training --upload
python -m improv_video status
python -m improv_video --lut ilog.cube build ~/Footage/2026-10-01 --kind lesson
```

## Тесты

```
pip install pytest
python -m pytest
```
