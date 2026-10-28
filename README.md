# KiCad 10 → Wokwi: экспорт `diagram.json`

Action Plugin для KiCad 10. Берёт схему открытого проекта и сохраняет готовый
`diagram.json` для симулятора [Wokwi](https://wokwi.com) в корень проекта (рядом с `.kicad_pro`).

```
wokwi_exporter/
├── __init__.py        регистрация плагина в KiCad
├── wokwi_exporter.py  весь код: маппинг деталей, нетлист, генерация JSON
└── icon.png           иконка кнопки на панели инструментов
```

## Установка (Windows, KiCad 10)

1. Скопируйте **папку** `wokwi_exporter` целиком в

   ```
   %USERPROFILE%\Documents\KiCad\10.0\scripting\plugins\
   ```

   В итоге должен получиться путь
   `...\Documents\KiCad\10.0\scripting\plugins\wokwi_exporter\__init__.py`.
   Если папки `scripting\plugins` нет, создайте её. Точную папку всегда покажет сам KiCad:
   **PCB Editor → Tools → External Plugins → Open Plugin Directory**.
   KiCad 10 также ищет плагины в `%APPDATA%\kicad\10.0\scripting\plugins\`.
2. В редакторе платы: **Tools → External Plugins → Refresh Plugins** (или перезапустите KiCad).
3. В меню **Tools → External Plugins** появится **Export to Wokwi (diagram.json)**, а на
   панели инструментов — зелёная кнопка.

macOS: `~/Documents/KiCad/10.0/scripting/plugins/`, Linux: `~/.local/share/kicad/10.0/scripting/plugins/`.

## Использование

1. **Сохраните схему.** Плагин читает файл `.kicad_sch` с диска.
2. Откройте плату проекта в **PCB Editor**. Action-плагины KiCad работают только в редакторе плат;
   печатная плата при этом может быть пустой.
3. **Tools → External Plugins → Export to Wokwi (diagram.json)**.
4. В окне отчёта видно, что экспортировано, какие детали пропущены и какие пины не нашли
   пару в Wokwi. Файл `diagram.json` лежит в папке проекта, а предыдущая версия сохраняется
   в `diagram.json.bak`.
5. В Wokwi откройте вкладку `diagram.json` проекта и вставьте содержимое файла.
   Скетч (`sketch.ino` / `main.py`) Wokwi нужен отдельно.

Если в Wokwi вы двигали детали, перекрашивали или трассировали провода, повторный экспорт это
сохранит (`PRESERVE_LAYOUT = True`): обновятся только состав деталей и связи.

## Как добавить свою деталь

Откройте `wokwi_exporter.py`, раздел **«2. МАППИНГ КОМПОНЕНТОВ»**, словарь `PART_MAP`:

```python
PART_MAP = {
    # ключ: имя символа KiCad, полный lib_id или поле Value (можно шаблон с *)
    "LED": {"type": "wokwi-led", "attrs": {"color": "red"}, "pins": {"A": "A", "K": "C"}},

    # ваш символ: пины KiCad (имя или номер) → пины Wokwi
    "MyRelayModule": {"type": "wokwi-relay-module", "pins": {"VCC": "VCC", "GND": "GND", "IN": "IN"}},

    # своя плата: role = "mcu", pin_style переводит имена пинов автоматически
    "ESP32-DevKitC-32E": {"type": "board-esp32-devkit-c-v4", "role": "mcu", "pin_style": "esp32"},
}
```

* Типы деталей и имена их пинов: <https://docs.wokwi.com/parts>
* `pin_style`: `arduino` (`D13` → `13`), `esp32` (`IO2` → `2`, `TXD0` → `TX`), `pico` (`GPIO5` → `GP5`),
  `stm32` (`PA0` → `A0`). Все пины GND/питания сводятся к одному пину платы (`GND.1`, `5V` …).
* `None` вместо описания означает, что деталь пропускается молча (так сделано для конденсаторов).

Из коробки поддерживаются Arduino Uno/Nano, ESP32-WROOM-32 (как DevKitC v4), Raspberry Pi Pico / Pico W,
STM32F103C8 (как Blue Pill), светодиоды (включая RGB), резисторы (номинал из Value), потенциометры,
кнопки, переключатели, зуммер, сервопривод, DHT11/22, HC-SR04, OLED SSD1306, LCD 1602.

## Откуда берутся связи

По умолчанию (`NETLIST_SOURCE = "auto"`) плагин вызывает `kicad-cli sch export netlist`.
Связность тогда считает сам KiCad: иерархия, шины, метки — ровно как в ERC.
Если `kicad-cli` не найден, используется встроенный парсер `.kicad_sch`. Он понимает провода,
точки соединения, локальные, глобальные и иерархические метки, power-символы и вложенные листы,
но не шины, проходящие между листами. Детали с флагом «Исключить из платы» `kicad-cli` не
выгружает; если такие детали есть в `PART_MAP`, плагин сам переключается на встроенный парсер.

## Запуск без KiCad (для отладки маппинга)

```bash
"C:\Program Files\KiCad\10.0\bin\python.exe" wokwi_exporter\wokwi_exporter.py D:\path\to\project.kicad_pro
```

Опции: `--source auto|kicad-cli|schematic`, `--layout grid|schematic`, `-o путь\diagram.json`.

## Про API KiCad 10

В KiCad 10 SWIG-модуль `pcbnew` объявлен устаревшим в пользу нового IPC API, но всё ещё
поставляется (проверено на 10.0.7), поэтому плагин использует классический `pcbnew.ActionPlugin`.
Вся логика экспорта от `pcbnew` не зависит и работает из командной строки.
