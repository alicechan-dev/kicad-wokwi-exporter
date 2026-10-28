# -*- coding: utf-8 -*-
"""
KiCad 10 → Wokwi: экспорт схемы в diagram.json
==============================================

Action Plugin для редактора плат KiCad 10 (pcbnew). Берёт корневую схему
текущего проекта, строит нетлист и сохраняет diagram.json для симулятора
Wokwi в корень проекта (рядом с .kicad_pro).

Откуда берутся связи (настройка NETLIST_SOURCE):
  * "kicad-cli"  — `kicad-cli sch export netlist`: связность считает сам KiCad
                   (иерархия, шины, метки — ровно как в ERC). Самый надёжный.
  * "schematic"  — встроенный парсер .kicad_sch (S-expressions): провода,
                   соединения, локальные/глобальные/иерархические метки,
                   power-символы, вложенные листы. Шины между листами
                   не поддерживаются.
  * "auto"       — kicad-cli, а если его нет — встроенный парсер.

Схема читается с диска, поэтому перед экспортом схему нужно сохранить.

Маппинг деталей хранится в mapping.json рядом с этим файлом. При первом
запуске он создаётся из DEFAULT_PART_MAP / DEFAULT_BOARD_PINS, дальше
плагин читает только его — обновление плагина ваши привязки не затрёт.

Если diagram.json уже есть, экспорт сливается с ним (SMART_MERGE): детали и
провода из KiCad обновляются, а добавленное вручную в Wokwi сохраняется.

Скрипт работает и без KiCad — удобно для отладки маппинга:
    python wokwi_exporter.py путь/к/проекту.kicad_sch
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections import defaultdict

try:
    import pcbnew
except ImportError:  # запуск вне KiCad (CLI, тесты)
    pcbnew = None


# ═══════════════════════════════════════════════════════════════════════════
# 1. НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════════════════

NETLIST_SOURCE = "auto"      # "auto" | "kicad-cli" | "schematic"
KICAD_CLI_PATH = ""          # явный путь к kicad-cli, если автопоиск его не находит
OUTPUT_FILE_NAME = "diagram.json"

LAYOUT = "grid"              # "grid": плата слева, периферия сеткой справа
                             # "schematic": взаимное расположение как на схеме
SCHEMATIC_SCALE = 4.0        # пикселей Wokwi на 1 мм схемы (для LAYOUT = "schematic")
GRID_COLUMNS = 6
GRID_STEP_X = 110
GRID_STEP_Y = 130
GRID_OFFSET_X = 340          # отступ периферии от платы (Arduino Uno ≈ 275 px в ширину)

MAPPING_FILE_NAME = "mapping.json"  # маппинг деталей; лежит рядом с этим .py

SMART_MERGE = True           # diagram.json уже есть — слить с ним, а не перезаписать:
                             # детали и провода из KiCad обновляются (позиции, атрибуты и
                             # трассировка из Wokwi остаются), добавленное вручную переносится
DROP_DELETED_IN_KICAD = True # удалять то, что прошлый экспорт создал из KiCad, а в схеме этого
                             # больше нет. При False старые провода KiCad остаются в файле и
                             # могут замкнуть цепи в симуляции (например, после переноса
                             # светодиода с D13 на D12 он окажется подключён к обоим пинам)
STATE_FILE_SUFFIX = ".kicad-state.json"  # «.diagram.kicad-state.json» рядом с diagram.json:
                                         # какие детали и провода создал прошлый экспорт

# Цвета проводов для сигнальных цепей (GND всегда чёрный, питание — красный)
WIRE_COLORS = ["green", "blue", "orange", "purple", "gold", "cyan",
               "magenta", "limegreen", "violet", "gray"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. МАППИНГ ПО УМОЛЧАНИЮ KiCad → Wokwi
# ═══════════════════════════════════════════════════════════════════════════
#
# Свои детали добавляйте в mapping.json рядом с плагином, а не сюда: этот
# словарь только записывается в mapping.json при первом запуске, и при
# обновлении плагина он будет перезаписан. Формат записей тот же.
#
# КЛЮЧ — как найти символ KiCad. Сравнивается без учёта регистра с:
#     1) полным lib_id            "Device:LED"
#     2) именем символа            "LED"
#     3) полем Value               "SSD1306"
#   Можно писать шаблоны с * и ?: "Arduino_Mega*", "*SSD1306*".
#   Точные ключи важнее шаблонов; шаблоны проверяются сверху вниз.
#
# ЗНАЧЕНИЕ — описание детали Wokwi:
#   "type"      — тип детали Wokwi (обязательно), см. https://docs.wokwi.com/parts
#   "attrs"     — атрибуты по умолчанию, например {"color": "red"}
#   "pins"      — пин KiCad → пин Wokwi. Ключ — ИМЯ пина KiCad ("K", "VDD")
#                 или его НОМЕР ("1"). Сначала ищется имя, потом номер.
#   "pin_style" — автоматический перевод имён пинов платы ("D13" → "13",
#                 "IO2" → "2", "GPIO5" → "GP5", "PA0" → "A0"):
#                 "arduino" | "esp32" | "pico" | "stm32" (правила в PIN_RULES).
#   "role"      — "mcu" у центрального микроконтроллера.
#   "hook"      — имя функции из HOOKS: получает компонент и возвращает dict
#                 атрибутов, например номинал резистора.
#
# None (в JSON — null) вместо описания — символ пропускается без предупреждения.

_LCD1602_PINS = {
    "VSS": "VSS", "GND": "VSS", "VDD": "VDD", "VCC": "VDD",
    "VO": "V0", "V0": "V0", "VEE": "V0",
    "RS": "RS", "R/W": "RW", "RW": "RW", "E": "E", "EN": "E",
    "LED(+)": "A", "LEDA": "A", "LED+": "A", "A/VEE": "A", "A": "A", "A1": "A", "A2": "A", "BLA": "A",
    "LED(-)": "K", "LEDK": "K", "LED-": "K", "K": "K", "K1": "K", "K2": "K", "BLK": "K",
}
_LCD1602_PINS.update({"DB%d" % i: "D%d" % i for i in range(8)})
_LCD1602_PINS.update({"D%d" % i: "D%d" % i for i in range(8)})

DEFAULT_PART_MAP = {
    # ─── Микроконтроллеры и платы (role = "mcu") ────────────────────────────
    "Arduino_UNO_R3":     {"type": "wokwi-arduino-uno",  "role": "mcu", "pin_style": "arduino"},
    "Arduino_UNO_R2":     {"type": "wokwi-arduino-uno",  "role": "mcu", "pin_style": "arduino"},
    "Arduino_Nano_v3.x":  {"type": "wokwi-arduino-nano", "role": "mcu", "pin_style": "arduino"},
    "Arduino_Nano_v2.x":  {"type": "wokwi-arduino-nano", "role": "mcu", "pin_style": "arduino"},
    "Arduino_Nano_Every": {"type": "wokwi-arduino-nano", "role": "mcu", "pin_style": "arduino"},
    "ESP32-WROOM-32":     {"type": "board-esp32-devkit-c-v4", "role": "mcu", "pin_style": "esp32"},
    "ESP32-WROOM-32D":    {"type": "board-esp32-devkit-c-v4", "role": "mcu", "pin_style": "esp32"},
    "ESP32-WROOM-32E":    {"type": "board-esp32-devkit-c-v4", "role": "mcu", "pin_style": "esp32"},
    "RaspberryPi_Pico":   {"type": "wokwi-pi-pico",   "role": "mcu", "pin_style": "pico"},
    "RaspberryPi_Pico_W": {"type": "board-pi-pico-w", "role": "mcu", "pin_style": "pico"},
    "STM32F103C8Tx":      {"type": "board-stm32-bluepill", "role": "mcu", "pin_style": "stm32"},
    "STM32F103CBTx":      {"type": "board-stm32-bluepill", "role": "mcu", "pin_style": "stm32"},

    # ─── Светодиоды ─────────────────────────────────────────────────────────
    "LED":              {"type": "wokwi-led", "attrs": {"color": "red"}, "pins": {"A": "A", "K": "C"}, "hook": "led_color"},
    "LED_Small":        {"type": "wokwi-led", "attrs": {"color": "red"}, "pins": {"A": "A", "K": "C"}, "hook": "led_color"},
    "LED_Filled":       {"type": "wokwi-led", "attrs": {"color": "red"}, "pins": {"A": "A", "K": "C"}, "hook": "led_color"},
    "LED_Small_Filled": {"type": "wokwi-led", "attrs": {"color": "red"}, "pins": {"A": "A", "K": "C"}, "hook": "led_color"},
    # RGB: общий анод / общий катод
    "LED_RGBA": {"type": "wokwi-rgb-led", "attrs": {"common": "anode"},   "pins": {"RK": "R", "GK": "G", "BK": "B", "A": "COM"}},
    "LED_ARGB": {"type": "wokwi-rgb-led", "attrs": {"common": "anode"},   "pins": {"RK": "R", "GK": "G", "BK": "B", "A": "COM"}},
    "LED_RGBK": {"type": "wokwi-rgb-led", "attrs": {"common": "cathode"}, "pins": {"RA": "R", "GA": "G", "BA": "B", "K": "COM"}},
    "LED_KRGB": {"type": "wokwi-rgb-led", "attrs": {"common": "cathode"}, "pins": {"RA": "R", "GA": "G", "BA": "B", "K": "COM"}},

    # ─── Резисторы и потенциометры ──────────────────────────────────────────
    "R":          {"type": "wokwi-resistor", "pins": {"1": "1", "2": "2"}, "hook": "resistor_value"},
    "R_Small":    {"type": "wokwi-resistor", "pins": {"1": "1", "2": "2"}, "hook": "resistor_value"},
    "R_US":       {"type": "wokwi-resistor", "pins": {"1": "1", "2": "2"}, "hook": "resistor_value"},
    "R_Small_US": {"type": "wokwi-resistor", "pins": {"1": "1", "2": "2"}, "hook": "resistor_value"},
    # Пин 1 у символа KiCad сверху — туда обычно приходит +V
    "R_Potentiometer":       {"type": "wokwi-potentiometer", "pins": {"1": "VCC", "2": "SIG", "3": "GND"}},
    "R_Potentiometer_Small": {"type": "wokwi-potentiometer", "pins": {"1": "VCC", "2": "SIG", "3": "GND"}},
    "R_Potentiometer_US":    {"type": "wokwi-potentiometer", "pins": {"1": "VCC", "2": "SIG", "3": "GND"}},
    "R_Potentiometer_Trim":  {"type": "wokwi-potentiometer", "pins": {"1": "VCC", "2": "SIG", "3": "GND"}},

    # ─── Кнопки и переключатели ─────────────────────────────────────────────
    # У wokwi-pushbutton пары 1.l/1.r и 2.l/2.r замкнуты внутри, кнопка соединяет 1 ↔ 2
    "SW_Push":       {"type": "wokwi-pushbutton", "attrs": {"color": "green"}, "pins": {"1": "1.l", "2": "2.l"}},
    "SW_Push_45deg": {"type": "wokwi-pushbutton", "attrs": {"color": "green"}, "pins": {"1": "1.l", "2": "2.l"}},
    "SW_Push_Open":  {"type": "wokwi-pushbutton", "attrs": {"color": "green"}, "pins": {"A": "1.l", "B": "2.l"}},
    "SW_MEC_5G":     {"type": "wokwi-pushbutton", "attrs": {"color": "green"}, "pins": {"A": "1.l", "B": "2.l"}},
    # Ползунковый переключатель: средний вывод 2 — общий (как B у SW_SPDT)
    "SW_SPDT":       {"type": "wokwi-slide-switch", "pins": {"A": "1", "B": "2", "C": "3"}},
    "SW_SPST":       {"type": "wokwi-slide-switch", "pins": {"A": "1", "B": "2"}},

    # ─── Звук, сервопривод, датчики ─────────────────────────────────────────
    "Buzzer":        {"type": "wokwi-buzzer", "pins": {"+": "2", "-": "1"}},
    "Motor_Servo*":  {"type": "wokwi-servo", "pins": {"PWM": "PWM", "+": "V+", "-": "GND"}},
    "DHT11":         {"type": "wokwi-dht22", "pins": {"VDD": "VCC", "DATA": "SDA", "NC": "NC", "GND": "GND"}},
    "DHT22":         {"type": "wokwi-dht22", "pins": {"VDD": "VCC", "VCC": "VCC", "DATA": "SDA", "SDA": "SDA", "NC": "NC", "GND": "GND"}},
    "AM2302":        {"type": "wokwi-dht22", "pins": {"VDD": "VCC", "SDA": "SDA", "GND": "GND"}},
    # HC-SR04 в стандартных библиотеках KiCad нет — ключ сработает по имени
    # вашего символа или по Value. Номера пинов — порядок на модуле.
    "HC-SR04":       {"type": "wokwi-hc-sr04", "pins": {"VCC": "VCC", "TRIG": "TRIG", "ECHO": "ECHO", "GND": "GND",
                                                       "1": "VCC", "2": "TRIG", "3": "ECHO", "4": "GND"}},

    # ─── Дисплеи ────────────────────────────────────────────────────────────
    # OLED 128x64 I2C. Подходит для своего символа "SSD1306…" или разъёма
    # Conn_01x04 с Value = "SSD1306" (тогда пины 1..4 = GND, VCC, SCL, SDA).
    "*SSD1306*":     {"type": "board-ssd1306", "attrs": {"i2cAddress": "0x3c"},
                      "pins": {"GND": "GND", "VCC": "VCC", "VDD": "VCC", "3V3": "VCC", "VIN": "VCC",
                               "SCL": "SCL", "SCK": "SCL", "SDA": "SDA",
                               "1": "GND", "2": "VCC", "3": "SCL", "4": "SDA"}},
    "OLED":          {"type": "board-ssd1306", "attrs": {"i2cAddress": "0x3c"},
                      "pins": {"GND": "GND", "VCC": "VCC", "SCL": "SCL", "SDA": "SDA",
                               "1": "GND", "2": "VCC", "3": "SCL", "4": "SDA"}},
    # LCD 16x2 с I2C-адаптером (свой символ или Value вида "LCD1602_I2C")
    "*1602*I2C*":    {"type": "wokwi-lcd1602", "attrs": {"pins": "i2c"},
                      "pins": {"GND": "GND", "VCC": "VCC", "SDA": "SDA", "SCL": "SCL"}},
    # LCD 16x2 с параллельным интерфейсом (WC1602A, RC1602A, HY1602E, LCD-016N002L …)
    "LCD-016N002L":  {"type": "wokwi-lcd1602", "pins": _LCD1602_PINS},
    "*1602*":        {"type": "wokwi-lcd1602", "pins": _LCD1602_PINS},

    # ─── Платы по шаблону (если ваш символ называется иначе) ────────────────
    "Arduino_Mega*":   {"type": "wokwi-arduino-mega", "role": "mcu", "pin_style": "arduino"},
    "*Mega2560*":      {"type": "wokwi-arduino-mega", "role": "mcu", "pin_style": "arduino"},
    "*ESP32*DevKit*":  {"type": "board-esp32-devkit-c-v4", "role": "mcu", "pin_style": "esp32"},
    "*BluePill*":      {"type": "board-stm32-bluepill", "role": "mcu", "pin_style": "stm32"},
    "*Blue_Pill*":     {"type": "board-stm32-bluepill", "role": "mcu", "pin_style": "stm32"},

    # ─── Не нужны в Wokwi: пропускаются молча ───────────────────────────────
    # (шаблоны держите в конце, чтобы они не перехватили детали выше)
    "C": None, "C_Small": None, "C_Polarized": None, "C_Polarized_Small": None,
    "CP": None, "CP_Small": None, "C_US": None, "PWR_FLAG": None,
    "MountingHole*": None, "TestPoint*": None, "Fiducial*": None, "NetTie*": None,
}


# ═══════════════════════════════════════════════════════════════════════════
# 3. ПИНЫ ПЛАТ WOKWI
# ═══════════════════════════════════════════════════════════════════════════
# Списки взяты из исходников wokwi-elements / wokwi-boards. Для плат из этого
# словаря автопереводчик пинов выбирает только существующие имена. Как и
# маппинг деталей, при первом запуске попадают в mapping.json ("board_pins").

def _pins(text):
    return frozenset(text.split())

DEFAULT_BOARD_PINS = {
    "wokwi-arduino-uno": _pins(
        "0 1 2 3 4 5 6 7 8 9 10 11 12 13 A0 A1 A2 A3 A4 A5 A4.2 A5.2 "
        "AREF IOREF RESET 3.3V 5V VIN GND.1 GND.2 GND.3"),
    "wokwi-arduino-nano": _pins(
        "0 1 2 3 4 5 6 7 8 9 10 11 12 13 A0 A1 A2 A3 A4 A5 A6 A7 "
        "AREF RESET RESET.2 RESET.3 3.3V 5V 5V.2 VIN GND.1 GND.2 GND.3 11.2 12.2 13.2"),
    "wokwi-arduino-mega": _pins(
        " ".join(str(i) for i in range(54)) + " " + " ".join("A%d" % i for i in range(16)) +
        " SCL SDA AREF IOREF RESET 3.3V 5V 5V.1 5V.2 VIN GND.1 GND.2 GND.3 GND.4 GND.5"),
    "board-esp32-devkit-c-v4": _pins(
        "0 2 4 5 12 13 14 15 16 17 18 19 21 22 23 25 26 27 32 33 34 35 VP VN TX RX "
        "EN 3V3 5V GND.1 GND.2 GND.3 CLK CMD D0 D1 D2 D3"),
    "wokwi-esp32-devkit-v1": _pins(
        "D2 D4 D5 D12 D13 D14 D15 D18 D19 D21 D22 D23 D25 D26 D27 D32 D33 D34 D35 "
        "VP VN TX0 RX0 TX2 RX2 EN 3V3 VIN GND.1 GND.2"),
    "wokwi-pi-pico": _pins(
        " ".join("GP%d" % i for i in list(range(23)) + [26, 27, 28]) +
        " RUN ADC_VREF 3V3 3V3_EN VSYS VBUS " + " ".join("GND.%d" % i for i in range(1, 9))),
    "board-stm32-bluepill": _pins(
        "A0 A1 A2 A3 A4 A5 A6 A7 A8 A9 A10 A11 A12 A15 B0 B1 B3 B4 B5 B6 B7 B8 B9 "
        "B10 B11 B12 B13 B14 B15 C13 C14 C15 R VBAT 5V.1 3V3.1 3V3.2 GND.1 GND.2 GND.3"),
}
DEFAULT_BOARD_PINS["board-pi-pico-w"] = DEFAULT_BOARD_PINS["wokwi-pi-pico"]

# Правила перевода имени пина KiCad в кандидаты имён Wokwi: (regex, [шаблоны]).
# Имя пина режется по "/" ("D0/RX" → "D0", "RX"), каждая часть проверяется
# правилами (fullmatch, без учёта регистра). Побеждает первый кандидат,
# который есть в board_pins для этой платы.
PIN_RULES = {
    "arduino": [
        (r"D(\d+)", [r"\1"]),
        (r"TXD?[01]?", ["1"]),
        (r"RXD?[01]?", ["0"]),
        (r"MOSI|COPI", ["11"]),  # SPI Uno/Nano без номера (у Nano Every пины подписаны только так)
        (r"MISO|CIPO", ["12"]),
        (r"SCK", ["13"]),
    ],
    "esp32": [
        (r"SENSOR_VP|SVP|(?:GPIO|IO|I)36", ["VP", "36", "D36"]),
        (r"SENSOR_VN|SVN|(?:GPIO|IO|I)39", ["VN", "39", "D39"]),
        (r"(?:U0)?TXD?0?|(?:GPIO|IO)1", ["TX", "TX0", "1"]),
        (r"(?:U0)?RXD?0?|(?:GPIO|IO)3", ["RX", "RX0", "3"]),
        (r"(?:GPIO|IO)16|U2RXD|RXD?2", ["16", "RX2", "D16"]),
        (r"(?:GPIO|IO)17|U2TXD|TXD?2", ["17", "TX2", "D17"]),
        (r"(?:GPIO|IO|GP|D|I)(\d+)", [r"\1", r"D\1"]),
        (r"CHIP_PU", ["EN"]),
    ],
    "pico": [
        (r"(?:GPIO|GP|IO|P)(\d+)(?:_\w+)?", [r"GP\1"]),
    ],
    "stm32": [
        (r"P([A-K])(\d{1,2})(?:[-_]\w*)?", [r"\1\2", r"P\1\2"]),
    ],
}
COMMON_PIN_RULES = [
    (r"GND\d*|AGND|DGND|PGND|GNDA|GNDD|VSS\w*|0V", ["GND.1", "GND"]),
    (r"\+?5V0?|VCC_?5V|VUSB|VBUS|USB", ["5V", "5V.1", "VBUS", "VIN"]),
    (r"\+?3V3|\+?3\.3V|VDD\w*|VCC_?3V3|3V3_OUT", ["3V3", "3.3V", "3V3.1"]),
    (r"VIN|VSYS|RAW", ["VIN", "VSYS"]),
    (r"RST|RESET|NRST|EN|CHIP_EN|RUN|MCLR", ["RESET", "EN", "RUN", "R"]),
]


# ═══════════════════════════════════════════════════════════════════════════
# 4. HOOKS: атрибуты из поля Value
# ═══════════════════════════════════════════════════════════════════════════

_COLOR_WORDS = [
    ("red", ("red", "красн")),
    ("green", ("green", "grn", "зелен", "зелён")),
    ("blue", ("blue", "blu", "синий", "голуб")),
    ("yellow", ("yellow", "yel", "желт", "жёлт")),
    ("orange", ("orange", "оранж")),
    ("white", ("white", "wht", "белый")),
    ("purple", ("purple", "violet", "фиолет")),
    ("cyan", ("cyan",)),
    ("magenta", ("magenta", "pink", "розов")),
]


def parse_resistance(value):
    """'220' → '220', '4k7' → '4700', '10 kΩ' → '10000', '2R2' → '2.2'."""
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*([RrKkMmGg])?(\d+)?", value or "")
    if not m:
        return None
    number, mult, tail = m.group(1).replace(",", "."), (m.group(2) or "r").lower(), m.group(3)
    if tail and m.group(2) and "." not in number:
        number = "%s.%s" % (number, tail)
    ohms = float(number) * {"r": 1, "k": 1e3, "m": 1e6, "g": 1e9}[mult]
    return str(int(ohms)) if ohms.is_integer() else "%g" % ohms


def hook_resistor_value(comp):
    ohms = parse_resistance(comp.value)
    return {"value": ohms} if ohms else {}


def hook_led_color(comp):
    text = (comp.value or "").lower()
    for color, words in _COLOR_WORDS:
        if any(w in text for w in words):
            return {"color": color}
    return {}


# Регистрируйте здесь свои функции, чтобы ссылаться на них из mapping.json по имени
HOOKS = {
    "resistor_value": hook_resistor_value,
    "led_color": hook_led_color,
}


# ═══════════════════════════════════════════════════════════════════════════
# 4a. ФАЙЛ mapping.json
# ═══════════════════════════════════════════════════════════════════════════

_NOT_FOUND = object()


class ExportError(Exception):
    """Понятная пользователю причина остановки экспорта (без traceback)."""


class MappingError(ExportError):
    """mapping.json нельзя прочитать — экспорт останавливается, файл не трогаем."""


def _is_pattern(key):
    return any(ch in key for ch in "*?[")


class Mapping:
    def __init__(self, parts, board_pins, path="", created=False, warnings=None):
        self.parts = parts                      # ключ → описание | None; порядок важен для шаблонов
        self.board_pins = {k: frozenset(v) for k, v in board_pins.items()}
        self.path = path
        self.created = created
        self.warnings = warnings or []
        self._exact = {k.lower(): k for k in parts if not _is_pattern(k)}
        self._patterns = [(k.lower(), k) for k in parts if _is_pattern(k)]

    def lookup(self, comp):
        """(ключ, описание) для компонента или (None, _NOT_FOUND)."""
        keys = [k.lower() for k in (comp.lib_id, comp.part_name, comp.value) if k]
        for k in keys:
            if k in self._exact:
                return self._exact[k], self.parts[self._exact[k]]
        for pattern, key in self._patterns:
            if any(fnmatch.fnmatchcase(k, pattern) for k in keys):
                return key, self.parts[key]
        return None, _NOT_FOUND


def default_mapping():
    return Mapping(dict(DEFAULT_PART_MAP), DEFAULT_BOARD_PINS, "встроенный маппинг")


def mapping_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), MAPPING_FILE_NAME)


_MAPPING_SECTIONS = {
    "Arduino_UNO_R3": "Микроконтроллеры и платы (role: mcu)",
    "LED": "Светодиоды",
    "R": "Резисторы и потенциометры (пин 1 потенциометра в KiCad сверху — туда обычно приходит +V)",
    "SW_Push": "Кнопки и переключатели (у wokwi-pushbutton пары 1.l/1.r и 2.l/2.r замкнуты внутри)",
    "Buzzer": "Звук, сервопривод, датчики",
    "*SSD1306*": "Дисплеи (SSD1306 подходит и для Conn_01x04 с Value = SSD1306: пины 1..4 = GND, VCC, SCL, SDA)",
    "Arduino_Mega*": "Платы по шаблону, если ваш символ называется иначе",
    "C": "Не нужны в Wokwi: пропускаются молча (шаблоны держите в конце списка)",
}


def _json_entries(d, indent, width=150):
    """Записи словаря по одной на строку; длинные — с переносами."""
    pad, lines, items = " " * indent, [], list(d.items())
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        one = "%s%s: %s" % (pad, json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False))
        if width is None or len(one) <= width:
            lines.append(one + comma)
        else:
            body = json.dumps(v, ensure_ascii=False, indent=2).replace("\n", "\n" + pad)
            lines.append("%s%s: %s%s" % (pad, json.dumps(k, ensure_ascii=False), body, comma))
    return lines


def default_mapping_text():
    """Содержимое mapping.json по умолчанию: с пояснениями и разделами."""
    readme = [
        "Маппинг символов KiCad -> детали Wokwi. Плагин читает этот файл при каждом экспорте.",
        "Чтобы вернуть значения по умолчанию, удалите файл: при следующем экспорте он создастся заново.",
        "parts: КЛЮЧ — полный lib_id ('Device:LED'), имя символа ('LED') или поле Value; регистр не важен.",
        "  Шаблоны с * и ? ('Arduino_Mega*') проверяются сверху вниз, после точных ключей.",
        "  ЗНАЧЕНИЕ — null (пропустить деталь молча) или объект:",
        "    type      — тип детали Wokwi (обязательно), см. https://docs.wokwi.com/parts",
        "    attrs     — атрибуты по умолчанию, например {\"color\": \"red\"}",
        "    pins      — пин KiCad (ИМЯ или НОМЕР) -> пин Wokwi; сначала ищется имя, потом номер",
        "    pin_style — авто-перевод имён пинов платы: " + " | ".join(PIN_RULES),
        "    role      — \"mcu\" у центрального микроконтроллера",
        "    hook      — атрибуты из поля Value: " + " | ".join(HOOKS),
        "  Ключи, начинающиеся с '_', игнорируются — это комментарии.",
        "board_pins: имена пинов плат Wokwi; авто-перевод (pin_style) выбирает только существующие.",
    ]
    parts, n = {}, 0
    for key, entry in DEFAULT_PART_MAP.items():
        if key in _MAPPING_SECTIONS:
            n += 1
            parts["_%02d" % n] = "── %s ──" % _MAPPING_SECTIONS[key]
        parts[key] = entry
    boards = {k: sorted(v, key=_natural_key) for k, v in DEFAULT_BOARD_PINS.items()}
    lines = ["{", '  "_readme": [']
    lines += ["    %s%s" % (json.dumps(s, ensure_ascii=False), "," if i < len(readme) - 1 else "")
              for i, s in enumerate(readme)]
    lines += ["  ],", '  "parts": {'] + _json_entries(parts, 4) + ["  },", '  "board_pins": {']
    lines += _json_entries(boards, 4, width=None) + ["  }", "}"]
    text = "\n".join(lines) + "\n"
    json.loads(text)
    return text


def load_mapping(path=None):
    """Читает mapping.json; если его нет — создаёт из значений по умолчанию."""
    path = os.path.abspath(path or mapping_path())
    created, warnings = False, []
    if not os.path.exists(path):
        try:
            _write_text(path, default_mapping_text())
            created = True
        except OSError as exc:
            m = default_mapping()
            m.warnings.append("Не удалось создать %s (%s) — использован встроенный маппинг" % (path, exc))
            return m
    try:
        with open(path, encoding="utf-8-sig") as f:  # -sig: Блокнот Windows может добавить BOM
            data = json.load(f)
    except ValueError as exc:
        raise MappingError("Ошибка в %s:\n  %s\n\nИсправьте файл или удалите его — тогда при следующем "
                           "экспорте он создастся заново со значениями по умолчанию." % (path, exc))
    except OSError as exc:
        raise MappingError("Не удалось прочитать %s: %s" % (path, exc))
    if not isinstance(data, dict) or not isinstance(data.get("parts"), dict):
        raise MappingError('%s: в файле нет объекта "parts"' % path)

    parts = {}
    for key, entry in data["parts"].items():
        if key.startswith("_"):
            continue
        if entry is None:
            parts[key] = None
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str) or not entry["type"].strip():
            warnings.append('mapping.json: «%s» — нужен объект с полем "type" или null; запись пропущена' % key)
            continue
        entry = dict(entry)
        for field in ("pins", "attrs"):
            if field in entry and not (isinstance(entry[field], dict)
                                       and (field != "pins" or all(isinstance(v, str) for v in entry[field].values()))):
                warnings.append('mapping.json: «%s» — поле "%s" должно быть объектом%s; поле пропущено'
                                % (key, field, " со строками" if field == "pins" else ""))
                entry.pop(field)
        if entry.get("hook") and entry["hook"] not in HOOKS:
            warnings.append('mapping.json: «%s» — неизвестный hook "%s" (есть: %s)' % (key, entry["hook"], ", ".join(HOOKS)))
        if entry.get("pin_style") and entry["pin_style"] not in PIN_RULES:
            warnings.append('mapping.json: «%s» — неизвестный pin_style "%s" (есть: %s)'
                            % (key, entry["pin_style"], ", ".join(PIN_RULES)))
        parts[key] = entry

    board_pins = {}
    raw = data.get("board_pins", {})
    for key, pins in (raw.items() if isinstance(raw, dict) else ()):
        if key.startswith("_"):
            continue
        if isinstance(pins, list) and all(isinstance(p, str) for p in pins):
            board_pins[key] = pins
        else:
            warnings.append('mapping.json: board_pins «%s» — нужен список строк; пропущено' % key)
    return Mapping(parts, board_pins, path, created, warnings)


# ═══════════════════════════════════════════════════════════════════════════
# 5. S-EXPRESSIONS
# ═══════════════════════════════════════════════════════════════════════════

_SEXPR_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _unescape(s):
    if "\\" not in s:
        return s
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), s, flags=re.S)


def parse_sexpr(text):
    """Разбирает S-выражение KiCad в вложенные списки строк."""
    stack, current = [], []
    for m in _SEXPR_TOKEN.finditer(text):
        if m.group(1):
            stack.append(current)
            current = []
        elif m.group(2):
            if not stack:
                break
            done, current = current, stack.pop()
            current.append(done)
        elif m.group(3) is not None:
            current.append(_unescape(m.group(3)))
        elif m.group(4) is not None:
            current.append(m.group(4))
    while stack:  # файл оборван — закрываем незакрытые скобки
        done, current = current, stack.pop()
        current.append(done)
    return current[0] if current and isinstance(current[0], list) else []


def _find_all(node, key):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == key] if node else []


def _find(node, key):
    for c in (node[1:] if node else ()):
        if isinstance(c, list) and c and c[0] == key:
            return c
    return None


def _value(node, key, default=None):
    found = _find(node, key)
    return found[1] if found and len(found) > 1 and isinstance(found[1], str) else default


def _props(node):
    return {p[1]: p[2] for p in _find_all(node, "property")
            if len(p) > 2 and isinstance(p[1], str) and isinstance(p[2], str)}


def _float(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _at(node):
    at = _find(node, "at") or []
    return _float(at[1] if len(at) > 1 else 0), _float(at[2] if len(at) > 2 else 0), _float(at[3] if len(at) > 3 else 0)


def _key(v):
    """Координата в мм → целые внутренние единицы схемы KiCad (100 нм)."""
    return int(round(v * 10000))


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


# ═══════════════════════════════════════════════════════════════════════════
# 6. МОДЕЛЬ НЕТЛИСТА
# ═══════════════════════════════════════════════════════════════════════════

class Component:
    def __init__(self, ref, lib_id="", value="", footprint="", pos=None, dnp=False):
        self.ref = ref
        self.lib_id = lib_id
        self.value = value
        self.footprint = footprint
        self.pos = pos          # (x, y) в мм на листе схемы или None
        self.dnp = dnp
        self.pins = {}          # номер пина → имя пина

    @property
    def part_name(self):
        return self.lib_id.split(":", 1)[-1]


class Netlist:
    def __init__(self):
        self.components = {}    # ref → Component
        self.nets = []          # [(имя цепи, [(ref, номер пина), ...]), ...]
        self.source = ""
        self.warnings = []


# ═══════════════════════════════════════════════════════════════════════════
# 7. НЕТЛИСТ ЧЕРЕЗ kicad-cli
# ═══════════════════════════════════════════════════════════════════════════

def _version_key(path):
    m = re.search(r"[\\/](\d+)\.(\d+)[\\/]", path)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_kicad_cli():
    exe = "kicad-cli.exe" if os.name == "nt" else "kicad-cli"
    candidates = [KICAD_CLI_PATH, os.environ.get("KICAD_CLI", ""),
                  os.path.join(os.path.dirname(sys.executable), exe),
                  shutil.which("kicad-cli") or ""]
    if sys.platform == "win32":
        found = []
        for base in {os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", "")}:
            kicad_dir = os.path.join(base, "KiCad") if base else ""
            if kicad_dir and os.path.isdir(kicad_dir):
                for ver in os.listdir(kicad_dir):
                    found.append(os.path.join(kicad_dir, ver, "bin", exe))
        candidates += sorted(found, key=_version_key, reverse=True)
    elif sys.platform == "darwin":
        candidates.append("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    else:
        candidates += ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _run_kicad_cli_netlist(cli, sch_path):
    fd, out = tempfile.mkstemp(suffix=".net", prefix="wokwi_")
    os.close(fd)
    try:
        cmd = [cli, "sch", "export", "netlist", "--format", "kicadsexpr", "--output", out, sch_path]
        extra = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120, **extra)
        if proc.returncode != 0 or not os.path.getsize(out):
            raise RuntimeError("код %s: %s" % (proc.returncode, (proc.stderr or proc.stdout).strip()[-500:]))
        with open(out, encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def netlist_from_kicad_cli(sch_path, cli):
    root = parse_sexpr(_run_kicad_cli_netlist(cli, sch_path))
    nl = Netlist()
    # Имена пинов берём из libparts: в KiCad 10 pinfunction имеет вид "D13_28"
    lib_pins = {}
    for libpart in _find_all(_find(root, "libparts"), "libpart"):
        lib_pins[(_value(libpart, "lib", ""), _value(libpart, "part", ""))] = {
            _value(p, "num", ""): _value(p, "name", "") for p in _find_all(_find(libpart, "pins"), "pin")}
    for comp in _find_all(_find(root, "components"), "comp"):
        ref = _value(comp, "ref", "")
        libsource = _find(comp, "libsource")
        lib, part = _value(libsource, "lib", ""), _value(libsource, "part", "")
        props = {_value(p, "name", ""): _value(p, "value", "") for p in _find_all(comp, "property")}
        c = Component(ref, "%s:%s" % (lib, part) if lib else part, _value(comp, "value", ""),
                      _value(comp, "footprint", ""), None, any(k.lower() == "dnp" for k in props))
        c.pins.update(lib_pins.get((lib, part), {}))
        nl.components[ref] = c
    for net in _find_all(_find(root, "nets"), "net"):
        nodes = []
        for node in _find_all(net, "node"):
            ref, pin = _value(node, "ref", ""), _value(node, "pin", "")
            comp = nl.components.get(ref)
            if comp is not None and pin not in comp.pins:
                func = _value(node, "pinfunction", "")
                if func.endswith("_" + pin):
                    func = func[:-len(pin) - 1]
                comp.pins[pin] = func
            nodes.append((ref, pin))
        nl.nets.append((_value(net, "name", ""), nodes))
    return nl


# ═══════════════════════════════════════════════════════════════════════════
# 8. ВСТРОЕННЫЙ ПАРСЕР .kicad_sch
# ═══════════════════════════════════════════════════════════════════════════

class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        parent = self.parent
        if x not in parent:
            parent[x] = x
            return x
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


class _LibPin:
    __slots__ = ("number", "name", "etype", "hidden", "x", "y")

    def __init__(self, node):
        self.number = _value(node, "number", "")
        self.name = _value(node, "name", "")
        self.etype = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
        self.hidden = "hide" in node or _value(node, "hide") == "yes"
        self.x, self.y, _ = _at(node)


def _lib_pins(lib, libs, unit, body, depth=0):
    """Пины символа библиотеки для нужного юнита и стиля (De Morgan)."""
    if lib is None:
        return []
    parent = _value(lib, "extends")
    if parent and depth < 8:  # производный символ (в кэше схемы обычно уже развёрнут)
        prefix = lib[1].split(":", 1)[0] + ":" if ":" in lib[1] else ""
        return _lib_pins(libs.get(prefix + parent) or libs.get(parent), libs, unit, body, depth + 1)
    pins = []
    for sub in _find_all(lib, "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", sub[1] if len(sub) > 1 else "")
        if not m or int(m.group(1)) not in (0, unit) or int(m.group(2)) not in (0, body):
            continue
        pins.extend(_LibPin(p) for p in _find_all(sub, "pin"))
    return pins


def _pin_point(pin, sx, sy, rotation, mirror):
    """Точка подключения пина на листе: поворот, затем зеркало (как в KiCad)."""
    x, y = pin.x, -pin.y  # в библиотеке ось Y направлена вверх, на листе — вниз
    rotation = int(round(rotation)) % 360
    if rotation == 90:
        x, y = y, -x
    elif rotation == 180:
        x, y = -x, -y
    elif rotation == 270:
        x, y = -y, x
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return _key(sx + x), _key(sy + y)


_ESCAPE_TOKENS = {"dblquote": '"', "quote": "'", "lt": "<", "gt": ">", "backslash": "\\", "slash": "/",
                  "bar": "|", "comma": ",", "colon": ":", "space": " ", "dollar": "$", "tab": "\t",
                  "return": "\n", "brace": "{"}
_ESCAPE_RE = re.compile(r"\{(%s)\}" % "|".join(_ESCAPE_TOKENS))


def _net_text(raw):
    """Текст метки → (имя цепи без экранирования KiCad, это шина?).

    "VPP{slash}MCLR" — это "VPP/MCLR"; "~{RST}" — надчёркивание, а не шина;
    шины — "D[0..7]" и группы вида "{SDA SCL}".
    """
    plain = re.sub(r"[~_^]\{[^{}]*\}", "", _ESCAPE_RE.sub("", raw))
    is_bus = "{" in plain or re.search(r"\[\s*\d+\s*\.\.\s*\d+\s*\]", plain) is not None
    return _ESCAPE_RE.sub(lambda m: _ESCAPE_TOKENS[m.group(1)], raw), is_bus


def _on_segment(q, a, b):
    if (b[0] - a[0]) * (q[1] - a[1]) != (b[1] - a[1]) * (q[0] - a[0]):
        return False
    return min(a[0], b[0]) <= q[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= q[1] <= max(a[1], b[1])


class SchematicReader:
    """Читает иерархию .kicad_sch: компоненты, а при with_nets — и связность."""

    # Приоритет имени цепи: меньше — главнее
    _PRIO_GLOBAL, _PRIO_HIDDEN_POWER, _PRIO_LOCAL, _PRIO_HIER = 0, 1, 2, 3

    def __init__(self, root_path):
        self.root_path = os.path.abspath(root_path)
        self.root_dir = os.path.dirname(self.root_path)
        self.components = {}
        self.warnings = []
        self._trees = {}
        self._uf = _UnionFind()
        self._pin_nodes = set()
        self._names = {}
        self._legacy = {}
        self._with_nets = True

    def read(self, with_nets=True):
        self._with_nets = with_nets
        root = self._load(self.root_path)
        root_uuid = _value(root, "uuid", "")
        # KiCad 6 хранил обозначения в конце корневого файла
        for path in _find_all(_find(root, "symbol_instances"), "path"):
            self._legacy[path[1]] = (_value(path, "reference"), _int(_value(path, "unit"), 0))
        self._walk(self.root_path, "/" + root_uuid if root_uuid else "", "", [self.root_path])
        nl = Netlist()
        nl.components = self.components
        nl.warnings = self.warnings
        if with_nets:
            nl.nets = self._collect_nets()
        return nl

    def _load(self, path):
        if path not in self._trees:
            with open(path, encoding="utf-8") as f:
                tree = parse_sexpr(f.read())
            if not tree or tree[0] != "kicad_sch":
                raise ValueError("%s: это не файл схемы KiCad" % path)
            self._trees[path] = tree
        return self._trees[path]

    def _name(self, node, prio, name):
        if node not in self._names or (prio, name) < self._names[node]:
            self._names[node] = (prio, name)

    def _walk(self, path, inst, legacy_prefix, chain):
        tree = self._load(path)
        libs = {s[1]: s for s in _find_all(_find(tree, "lib_symbols"), "symbol") if len(s) > 1}
        # points: (точка, узел, может ли цепляться к середине провода)
        points, segments, sheets = [], [], []
        local_names, global_names = set(), set()

        for item in tree[1:]:
            if not isinstance(item, list) or not item:
                continue
            kind = item[0]
            if kind == "symbol":
                self._add_symbol(item, libs, inst, legacy_prefix, points, global_names)
            elif kind == "wire":
                xys = [(_key(_float(p[1])), _key(_float(p[2]))) for p in _find_all(_find(item, "pts"), "xy")]
                segments.extend(zip(xys, xys[1:]))
            elif kind == "junction":
                x, y, _ = _at(item)
                points.append(((_key(x), _key(y)), None, True))
            elif kind in ("label", "global_label", "hierarchical_label"):
                name, is_bus = _net_text(item[1] if len(item) > 1 and isinstance(item[1], str) else "")
                if not name or is_bus:
                    continue
                if kind == "label":
                    node, prio = ("local", inst, name), self._PRIO_LOCAL
                    local_names.add(name)
                elif kind == "global_label":
                    node, prio = ("global", name), self._PRIO_GLOBAL
                    global_names.add(name)
                else:
                    node, prio = ("hier", inst, name), self._PRIO_HIER
                self._name(node, prio, name)
                x, y, _ = _at(item)
                points.append(((_key(x), _key(y)), node, True))
            elif kind == "sheet":
                sheets.append(item)
                child_inst = inst + "/" + _value(item, "uuid", "")
                for pin in _find_all(item, "pin"):
                    name, is_bus = _net_text(pin[1] if len(pin) > 1 and isinstance(pin[1], str) else "")
                    if name and not is_bus:
                        x, y, _ = _at(pin)
                        points.append(((_key(x), _key(y)), ("hier", child_inst, name), True))

        if self._with_nets:
            self._connect(inst, points, segments)
            # Как в KiCad: локальная метка сливается с глобальной цепью (power-символ
            # или глобальная метка) с тем же именем, если та есть на этом же листе
            for name in local_names & global_names:
                self._uf.union(("local", inst, name), ("global", name))

        for sheet in sheets:
            props = _props(sheet)
            file_name = props.get("Sheetfile") or props.get("Sheet file") or ""
            sheet_uuid = _value(sheet, "uuid", "")
            child = self._resolve_sheet_file(file_name, os.path.dirname(path))
            if not child:
                self.warnings.append("Не найден файл листа '%s'" % file_name)
            elif child in chain:
                self.warnings.append("Рекурсивная иерархия листов: %s" % file_name)
            else:
                self._walk(child, inst + "/" + sheet_uuid, legacy_prefix + "/" + sheet_uuid, chain + [child])

    def _resolve_sheet_file(self, file_name, parent_dir):
        if not file_name:
            return None
        file_name = file_name.replace("${KIPRJMOD}", self.root_dir).replace("$(KIPRJMOD)", self.root_dir)
        for base in (parent_dir, self.root_dir):
            candidate = os.path.abspath(os.path.join(base, file_name))
            if os.path.isfile(candidate):
                return candidate
        return None

    def _instance(self, sym, inst, legacy_prefix):
        paths = [p for proj in _find_all(_find(sym, "instances"), "project") for p in _find_all(proj, "path")]
        for p in paths:
            if len(p) > 1 and p[1] == inst:
                return _value(p, "reference"), _int(_value(p, "unit"), 0)
        legacy = self._legacy.get(legacy_prefix + "/" + _value(sym, "uuid", ""))
        if legacy:
            return legacy
        if len(paths) == 1:  # лист скопирован из другого проекта
            return _value(paths[0], "reference"), _int(_value(paths[0], "unit"), 0)
        return None, 0

    def _add_symbol(self, sym, libs, inst, legacy_prefix, points, global_names):
        lib_id = _value(sym, "lib_id", "")
        lib = libs.get(_value(sym, "lib_name") or lib_id)
        props = _props(sym)
        x, y, rotation = _at(sym)
        mirror = _value(sym, "mirror")
        unit = _int(_value(sym, "unit"), 1)
        body = _int(_value(sym, "body_style") or _value(sym, "convert"), 1)
        ref, inst_unit = self._instance(sym, inst, legacy_prefix)
        ref = ref or props.get("Reference", "")
        unit = inst_unit or unit
        value = props.get("Value", "")
        if lib is None:
            self.warnings.append("%s: символ %s не найден в lib_symbols схемы" % (ref, lib_id))

        power_flag = _find(lib, "power") if lib is not None else None
        local_power = power_flag is not None and len(power_flag) > 1 and power_flag[1] == "local"
        comp = None
        if power_flag is None and not ref.startswith("#"):  # не power-символ и не служебный "#…"
            comp = self.components.get(ref)
            if comp is None:
                comp = Component(ref, lib_id, value, props.get("Footprint", ""), (x, y), _value(sym, "dnp") == "yes")
                self.components[ref] = comp

        for pin in _lib_pins(lib, libs, unit, body):
            if comp is not None:
                comp.pins.setdefault(pin.number, pin.name)
            if not self._with_nets:
                continue
            xy = _pin_point(pin, x, y, rotation, mirror)
            net = None
            if pin.etype == "power_in" and (power_flag is not None or pin.hidden):
                # Вход питания power-символа (цепь = Value) или скрытый вход питания
                # обычного символа (цепь = имя пина) — подключение по имени, без проводов
                name = (value if power_flag is not None else "") or pin.name
                if name:
                    net = ("local", inst, name) if local_power else ("global", name)
                    self._name(net, self._PRIO_GLOBAL if power_flag is not None else self._PRIO_HIDDEN_POWER, name)
                    if not local_power:
                        global_names.add(name)
            if comp is not None:
                node = ("pin", comp.ref, pin.number)
                self._pin_nodes.add(node)
                points.append((xy, node, False))
                if net:
                    self._uf.union(node, net)
            elif net:
                points.append((xy, net, False))

    def _connect(self, inst, points, segments):
        uf = self._uf
        mid_points = set()
        for xy, node, attaches_mid in points:
            if node is not None:
                uf.union(node, ("pt", inst) + xy)
            if attaches_mid:
                mid_points.add(xy)
        for a, b in segments:
            uf.union(("pt", inst) + a, ("pt", inst) + b)
        # К середине провода цепляются только метки и точки соединения (junction).
        # Пин или конец другого провода в середине провода без junction в KiCad
        # не соединены — проверено на kicad-cli.
        by_x, by_y = defaultdict(list), defaultdict(list)
        for xy in mid_points:
            by_x[xy[0]].append(xy)
            by_y[xy[1]].append(xy)
        for a, b in segments:
            if a[0] == b[0]:
                candidates = by_x[a[0]]
            elif a[1] == b[1]:
                candidates = by_y[a[1]]
            else:
                candidates = mid_points
            for q in candidates:
                if q != a and q != b and _on_segment(q, a, b):
                    uf.union(("pt", inst) + q, ("pt", inst) + a)

    def _collect_nets(self):
        groups = defaultdict(list)
        for node in self._pin_nodes:
            groups[self._uf.find(node)].append((node[1], node[2]))
        best = {}
        for node, name in self._names.items():
            root = self._uf.find(node)
            if root in groups and (root not in best or name < best[root]):
                best[root] = name
        nets = []
        for root, nodes in groups.items():
            nodes.sort(key=lambda n: (_natural_key(n[0]), _natural_key(n[1])))
            name = best[root][1] if root in best else "Net-(%s-Pad%s)" % nodes[0]
            nets.append((name, nodes))
        nets.sort(key=lambda n: _natural_key(n[0]))
        return nets


def load_netlist(sch_path, source=None, mapping=None):
    source = (source or NETLIST_SOURCE).lower()
    mapping = mapping or default_mapping()
    notes = []
    if source in ("auto", "kicad-cli"):
        cli = find_kicad_cli()
        nl = None
        if cli:
            try:
                nl = netlist_from_kicad_cli(sch_path, cli)
                nl.source = "kicad-cli (%s)" % cli
            except Exception as exc:
                if source == "kicad-cli":
                    raise
                notes.append("kicad-cli не сработал (%s) — связи взяты встроенным парсером" % exc)
        if nl is not None:
            placed = {}
            try:  # координаты на листе нужны для раскладки деталей
                placed = SchematicReader(sch_path).read(with_nets=False).components
            except Exception as exc:
                nl.warnings.append("Не удалось прочитать координаты деталей: %s" % exc)
            for ref, comp in nl.components.items():
                if ref in placed:
                    comp.pos = placed[ref].pos
            # kicad-cli не выгружает детали с флагом «Исключить из платы» (on_board no),
            # а внешние модули (дисплей, датчик) часто помечены именно так
            dropped = sorted((ref for ref, c in placed.items() if ref not in nl.components and not c.dnp
                              and mapping.lookup(c)[1] not in (None, _NOT_FOUND)), key=_natural_key)
            if not dropped:
                return nl
            msg = "kicad-cli не выгрузил %s (флаг «Исключить из платы»?)" % ", ".join(dropped)
            if source == "kicad-cli":
                nl.warnings.append(msg + " — эти детали пропущены")
                return nl
            notes.append(msg + " — связи взяты встроенным парсером")
        elif not cli:
            if source == "kicad-cli":
                raise RuntimeError("kicad-cli не найден — укажите путь в KICAD_CLI_PATH")
            notes.append("kicad-cli не найден — связи взяты встроенным парсером")
    nl = SchematicReader(sch_path).read(with_nets=True)
    nl.source = "встроенный парсер .kicad_sch"
    nl.warnings[:0] = notes
    return nl


# ═══════════════════════════════════════════════════════════════════════════
# 9. СБОРКА diagram.json
# ═══════════════════════════════════════════════════════════════════════════

def clean_pin_name(name):
    """'~{RESET}' → 'RESET', '~' → ''."""
    if not name or name == "~":
        return ""
    name = _ESCAPE_RE.sub(lambda m: _ESCAPE_TOKENS[m.group(1)], name)
    return re.sub(r"[~_^]\{([^}]*)\}", r"\1", name).strip()


def pin_candidates(name, style):
    tokens = [name] + [t for t in re.split(r"[/,|\s]+", name) if t and t != name]
    rules = PIN_RULES.get(style, []) + COMMON_PIN_RULES
    out = []
    for token in tokens:
        for rx, templates in rules:
            m = re.fullmatch(rx, token, re.I)
            if m:
                out.extend(m.expand(t) for t in templates)
        out.append(token)
    return list(dict.fromkeys(out))


def resolve_pin(entry, number, name, board_pins):
    """Пин KiCad (номер, имя) → имя пина Wokwi или None."""
    explicit = {str(k).upper(): v for k, v in (entry.get("pins") or {}).items()}
    clean = clean_pin_name(name)
    for key in (name, clean, number):
        if key and key.upper() in explicit:
            return explicit[key.upper()]
    style = entry.get("pin_style")
    known = board_pins.get(entry["type"])
    if not style and known is None:
        return None
    candidates = pin_candidates(clean or number, style)
    if known is None:
        return candidates[0] if candidates else None
    by_upper = {p.upper(): p for p in known}
    for cand in candidates:
        if cand.upper() in by_upper:
            return by_upper[cand.upper()]
    return None


def _make_id(ref, used):
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", ref) or "part"
    pid, n = base, 2
    while pid in used:
        pid, n = "%s_%d" % (base, n), n + 1
    used.add(pid)
    return pid


_GND_RE = re.compile(r"GND|VSS|^0V$", re.I)
_PWR_RE = re.compile(r"^\+|VCC|VDD|VIN|VBUS|VSYS|VBAT|3V3|3\.3V|5V|^\d+V\d*$", re.I)


def _wire_color(net_name, endpoints, palette_index):
    name = (net_name or "").lstrip("/")
    # Имя цепи главнее имён пинов: у потенциометра пин "GND" может висеть на +5V
    if _GND_RE.search(name):
        return "black", False
    if _PWR_RE.search(name):
        return "red", False
    pins = [e.split(":", 1)[1] for e in endpoints]
    if any(p.upper().startswith("GND") for p in pins):
        return "black", False
    if any(re.match(r"(5V|3V3|3\.3V|VCC|VIN|VBUS|VSYS|V\+)(\.\d+)?$", p, re.I) for p in pins):
        return "red", False
    return WIRE_COLORS[palette_index % len(WIRE_COLORS)], True


def _log(message):
    """Строка в консоль KiCad (stdout); ошибки вывода экспорт не роняют."""
    try:
        print("[Wokwi] " + message, flush=True)
    except Exception:
        pass


class ExportReport:
    def __init__(self):
        self.output = ""
        self.source = ""
        self.mapping_path = ""
        self.mapping_created = False
        self.parts = []
        self.mcus = []
        self.connections = 0
        self.kicad_parts = 0              # пришло из KiCad
        self.kicad_connections = 0
        self.merged = False               # был старый diagram.json
        self.updated_parts = 0            # детали KiCad, уже бывшие в нём (расположение из Wokwi)
        self.updated_connections = 0
        self.kept_parts = []              # перенесены из старого файла: ручные правки Wokwi
        self.kept_connections = []
        self.removed_parts = []           # создал прошлый экспорт, а в схеме их больше нет
        self.removed_connections = []
        self.dangling = []                # ручные провода к деталям, которых больше нет
        self.unmapped = []
        self.unresolved = []
        self.ignored = []
        self.warnings = []

    def summary_lines(self):
        """Сводка по деталям и связям — она же уходит в консоль KiCad."""
        lines = ["Из KiCad: деталей %d, связей %d" % (self.kicad_parts, self.kicad_connections)]
        if self.merged:
            lines.append("  из них обновлены в старом diagram.json: деталей %d, связей %d"
                         % (self.updated_parts, self.updated_connections))
            lines.append("Бережно перенесено из старого diagram.json: деталей %d, связей %d"
                         % (len(self.kept_parts), len(self.kept_connections)))
            if self.removed_parts or self.removed_connections:
                lines.append("Удалено (больше нет в схеме KiCad): деталей %d, связей %d"
                             % (len(self.removed_parts), len(self.removed_connections)))
            if self.dangling:
                lines.append("Отброшено ручных проводов к удалённым деталям: %d" % len(self.dangling))
        else:
            lines.append("Старого diagram.json нет — файл создан с нуля")
        lines.append("Итого в файле: деталей %d, связей %d" % (len(self.parts), self.connections))
        return lines

    def text(self):
        lines = ["Файл сохранён:", "  " + self.output, "",
                 "Источник связей: " + self.source,
                 "Маппинг: %s%s" % (self.mapping_path, " (создан со значениями по умолчанию)"
                                    if self.mapping_created else ""), ""]
        lines += self.summary_lines()
        if self.mcus:
            lines.append("Микроконтроллер: " + ", ".join(self.mcus))
        else:
            lines.append("⚠ Микроконтроллер не найден: добавьте его символ в mapping.json с \"role\": \"mcu\"")
        sections = [
            ("Перенесены из Wokwi (нет в схеме KiCad):", self.kept_parts),
            ("Удалены — их больше нет в схеме KiCad:", self.removed_parts + self.removed_connections),
            ("Отброшены ручные провода к несуществующим деталям:", self.dangling),
            ("Нет в mapping.json — пропущены (добавьте при необходимости):", self.unmapped),
            ("Пины без соответствия в Wokwi — провода к ним не созданы:", self.unresolved),
            ("Предупреждения:", self.warnings),
        ]
        for title, items in sections:
            if items:
                lines += ["", title] + ["  • " + s for s in items]
        if self.ignored:
            lines += ["", "Не нужны в Wokwi: " + ", ".join(self.ignored)]
        return "\n".join(lines)


class _Part:
    def __init__(self, comp, key, entry, part, base_attrs, hook_attrs):
        self.comp = comp
        self.key = key
        self.entry = entry
        self.part = part
        self.base_attrs = base_attrs      # attrs из mapping.json
        self.hook_attrs = hook_attrs      # attrs, вычисленные из поля Value
        self.is_mcu = entry.get("role") == "mcu"


def _layout(parts):
    mcus = [p for p in parts if p.is_mcu]
    others = [p for p in parts if not p.is_mcu]
    if LAYOUT == "schematic":
        placed = [p for p in parts if p.comp.pos]
        if placed:
            origin = (mcus[0] if mcus and mcus[0].comp.pos else min(placed, key=lambda p: p.comp.pos)).comp.pos
            for p in placed:
                p.part["left"] = int(round((p.comp.pos[0] - origin[0]) * SCHEMATIC_SCALE))
                p.part["top"] = int(round((p.comp.pos[1] - origin[1]) * SCHEMATIC_SCALE))
        return
    for i, p in enumerate(mcus):
        p.part["top"], p.part["left"] = i * 320, 0
    others.sort(key=lambda p: (p.comp.pos[1], p.comp.pos[0]) if p.comp.pos else (1e9, 1e9))
    x0 = GRID_OFFSET_X if mcus else 0
    for i, p in enumerate(others):
        p.part["left"] = x0 + (i % GRID_COLUMNS) * GRID_STEP_X
        p.part["top"] = (i // GRID_COLUMNS) * GRID_STEP_Y


def _endpoint_part(endpoint):
    return endpoint.split(":", 1)[0]


def _rename_endpoint(endpoint, renamed):
    pid, sep, pin = endpoint.partition(":")
    return renamed.get(pid, pid) + sep + pin


def smart_merge(kicad_parts, kicad_connections, existing, state, owner, report):
    """Сливает свежий экспорт KiCad со старым diagram.json.

    kicad_parts — _Part из KiCad, kicad_connections — их провода [a, b, цвет, трассировка].
    state — что создал прошлый экспорт: {"parts": [...], "connections": [[a, b], ...]} или None.
    owner — точка подключения → цепь KiCad (чтобы заметить ручной провод между разными цепями).

    Детали и провода из KiCad обновляются, но расположение, атрибуты и трассировка из Wokwi
    у них остаются. Всё, чего нет в новом экспорте, переносится как есть — кроме того, что
    создал прошлый экспорт, а из схемы оно пропало (DROP_DELETED_IN_KICAD).
    """
    parts = [p.part for p in kicad_parts]
    if not existing:
        return parts, kicad_connections
    report.merged = True
    prev_parts = set(state.get("parts", [])) if state else None
    prev_connections = {frozenset(c[:2]) for c in state.get("connections", [])} if state else None
    old_parts = existing.get("parts") if isinstance(existing.get("parts"), list) else []
    old_connections = existing.get("connections") if isinstance(existing.get("connections"), list) else []
    by_id = {p.part["id"]: p for p in kicad_parts}
    used_ids = set(by_id) | {p.get("id") for p in old_parts if isinstance(p, dict)}
    kept_parts, renamed = [], {}

    for old in old_parts:
        pid = old.get("id") if isinstance(old, dict) else None
        if not isinstance(pid, str):
            kept_parts.append(old)  # непонятная запись — не наша, не трогаем
            continue
        new = by_id.get(pid)
        if new is not None:
            same_type = old.get("type") == new.part["type"]
            if same_type or prev_parts is None or pid in prev_parts:
                # Та же деталь: тип и связи задаёт KiCad, расположение — Wokwi
                for k in ("top", "left", "rotate", "hide"):
                    if k in old:
                        new.part[k] = old[k]
                if same_type and isinstance(old.get("attrs"), dict):
                    # attrs: значения по умолчанию < правки в Wokwi < то, что следует из Value
                    new.part["attrs"] = {**new.base_attrs, **old["attrs"], **new.hook_attrs}
                report.updated_parts += 1
                continue
            # Ручная деталь Wokwi, чей id заняла новая деталь из KiCad
            new_id = _make_id(pid + "_wokwi", used_ids)
            renamed[pid] = new_id
            kept_parts.append(dict(old, id=new_id))
            report.kept_parts.append("%s (%s)" % (new_id, old.get("type", "?")))
            report.warnings.append("Деталь %s из Wokwi совпала по id с новой деталью из KiCad — "
                                   "переименована в %s" % (pid, new_id))
            continue
        if DROP_DELETED_IN_KICAD and prev_parts is not None and pid in prev_parts:
            report.removed_parts.append("%s (%s)" % (pid, old.get("type", "?")))
            continue
        kept_parts.append(old)
        report.kept_parts.append("%s (%s)" % (pid, old.get("type", "?")))

    final_ids = set(by_id) | {p.get("id") for p in kept_parts if isinstance(p, dict)}
    kicad_by_key = {frozenset(c[:2]): c for c in kicad_connections}
    kept_connections = []
    for old in old_connections:
        if not (isinstance(old, list) and len(old) >= 2 and all(isinstance(x, str) for x in old[:2])):
            kept_connections.append(old)
            continue
        new = kicad_by_key.get(frozenset(old[:2]))
        if new is not None:  # провод из KiCad: цвет и трассировка остаются из Wokwi
            if len(old) > 2:
                new[2] = old[2]
            if len(old) > 3:
                new[3] = old[3]
            report.updated_connections += 1
            continue
        wire = "%s — %s" % (old[0], old[1])
        if DROP_DELETED_IN_KICAD and prev_connections is not None and frozenset(old[:2]) in prev_connections:
            report.removed_connections.append(wire)
            continue
        a, b = _rename_endpoint(old[0], renamed), _rename_endpoint(old[1], renamed)
        if _endpoint_part(a) not in final_ids or _endpoint_part(b) not in final_ids:
            report.dangling.append(wire)
            continue
        if owner.get(a) and owner.get(b) and owner[a] != owner[b]:
            report.warnings.append("Провод %s из старого diagram.json соединяет разные цепи KiCad «%s» и «%s»"
                                   % (wire, owner[a], owner[b]))
        kept_connections.append([a, b] + list(old[2:]))
        report.kept_connections.append(wire)

    if prev_parts is None and (report.kept_parts or report.kept_connections):
        report.warnings.append("Нет данных о прошлом экспорте (файл *%s рядом с diagram.json), поэтому "
                               "перенесено всё, чего нет в схеме. Если среди этого есть устаревшие провода "
                               "KiCad, удалите их в Wokwi; со следующего экспорта плагин различает их сам."
                               % STATE_FILE_SUFFIX)
    return parts + kept_parts, kicad_connections + kept_connections


def build_diagram(netlist, mapping, existing=None, state=None):
    """Возвращает (diagram, report, state): state — что сгенерировал KiCad в этот раз."""
    report = ExportReport()
    report.source = netlist.source
    report.mapping_path, report.mapping_created = mapping.path, mapping.created
    report.warnings.extend(mapping.warnings + netlist.warnings)

    parts, used_ids = {}, set()
    for ref in sorted(netlist.components, key=_natural_key):
        comp = netlist.components[ref]
        if not ref or ref.startswith("#"):
            continue
        if comp.dnp:
            report.ignored.append("%s (DNP)" % ref)
            continue
        key, entry = mapping.lookup(comp)
        if entry is _NOT_FOUND:
            report.unmapped.append("%s — %s, Value «%s»" % (ref, comp.lib_id or "?", comp.value))
            continue
        if entry is None:
            report.ignored.append(ref)
            continue
        if ref.endswith("?"):
            report.warnings.append("%s: схема не аннотирована — обозначения могут совпасть" % ref)
        pid = _make_id(ref, used_ids)
        base_attrs, hook_attrs = dict(entry.get("attrs") or {}), {}
        hook = entry.get("hook")
        if hook in HOOKS:
            try:
                hook_attrs = dict(HOOKS[hook](comp) or {})
            except Exception as exc:
                report.warnings.append("%s: ошибка hook '%s': %s" % (ref, hook, exc))
        part = {"type": entry["type"], "id": pid, "top": 0, "left": 0, "attrs": {**base_attrs, **hook_attrs}}
        parts[ref] = _Part(comp, key, entry, part, base_attrs, hook_attrs)

    ordered = sorted(parts.values(), key=lambda p: (not p.is_mcu, _natural_key(p.comp.ref)))
    _layout(ordered)

    connections, owner, palette_index = [], {}, 0
    for net_name, nodes in netlist.nets:
        endpoints, unresolved, anchor = [], [], None
        for ref, number in nodes:
            p = parts.get(ref)
            if p is None:
                continue
            pin_name = p.comp.pins.get(number, "")
            wpin = resolve_pin(p.entry, number, pin_name, mapping.board_pins)
            if not wpin:
                unresolved.append("%s пин %s%s (%s)" % (
                    ref, number, " «%s»" % pin_name if clean_pin_name(pin_name) else "", p.part["type"]))
                continue
            endpoint = "%s:%s" % (p.part["id"], wpin)
            if endpoint in endpoints:
                continue
            endpoints.append(endpoint)
            if p.is_mcu and anchor is None:
                anchor = endpoint
        if unresolved and len(endpoints) + len(unresolved) >= 2:
            report.unresolved.extend("%s — цепь %s" % (u, net_name) for u in unresolved)
        if len(endpoints) < 2:
            continue
        for endpoint in endpoints:
            if endpoint in owner and owner[endpoint] != net_name:
                report.warnings.append("%s есть в цепях «%s» и «%s» — в Wokwi они окажутся соединены"
                                       % (endpoint, owner[endpoint], net_name))
            owner.setdefault(endpoint, net_name)
        color, from_palette = _wire_color(net_name, endpoints, palette_index)
        palette_index += from_palette
        anchor = anchor or endpoints[0]
        connections.extend([anchor, e, color, []] for e in endpoints if e != anchor)

    new_state = {"version": 1, "parts": [p.part["id"] for p in ordered],
                 "connections": [list(c[:2]) for c in connections]}
    report.kicad_parts, report.kicad_connections = len(ordered), len(connections)
    final_parts, final_connections = smart_merge(ordered, connections, existing, state, owner, report)

    diagram = {
        "version": 1,
        "author": "KiCad → Wokwi exporter",
        "editor": "wokwi",
        "parts": final_parts,
        "connections": final_connections,
        "dependencies": {},
    }
    if existing:
        for k, v in existing.items():  # serialMonitor, dependencies, author …
            if k not in ("version", "editor", "parts", "connections"):
                diagram[k] = v

    report.parts = final_parts
    report.mcus = ["%s → %s" % (p.comp.ref, p.part["type"]) for p in ordered if p.is_mcu]
    report.connections = len(final_connections)
    if len(report.mcus) > 1:
        report.warnings.append("Найдено несколько микроконтроллеров — Wokwi симулирует один основной")
    return diagram, report, new_state


def format_diagram(diagram):
    """JSON в стиле редактора Wokwi: одна деталь / одно соединение на строку."""
    def dump(v):
        return json.dumps(v, ensure_ascii=False)

    order = ["version", "author", "editor", "parts", "connections", "dependencies"]
    keys = [k for k in order if k in diagram] + [k for k in diagram if k not in order]
    lines = []
    for i, k in enumerate(keys):
        v, comma = diagram[k], "," if i < len(keys) - 1 else ""
        if k in ("parts", "connections") and isinstance(v, list) and v:
            body = ",\n".join("    " + dump(x) for x in v)
            lines.append('  %s: [\n%s\n  ]%s' % (dump(k), body, comma))
        else:
            lines.append("  %s: %s%s" % (dump(k), dump(v), comma))
    return "{\n" + "\n".join(lines) + "\n}\n"


def _write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _load_existing(path):
    """Старый diagram.json или None. Повреждённый файл не перезаписываем: в нём могут быть правки из Wokwi."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except ValueError as exc:
        raise ExportError("%s повреждён:\n  %s\n\nФайл не перезаписан, чтобы не потерять правки из Wokwi. "
                          "Исправьте его или удалите." % (path, exc))
    if not isinstance(data, dict):
        raise ExportError("%s: ожидался JSON-объект — файл не перезаписан" % path)
    return data


def state_path(out_path):
    folder, base = os.path.split(out_path)
    return os.path.join(folder, "." + os.path.splitext(base)[0] + STATE_FILE_SUFFIX)


def _load_state(path, warnings):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            state = json.load(f)
        if isinstance(state.get("parts"), list) and isinstance(state.get("connections"), list):
            state["connections"] = [c for c in state["connections"] if isinstance(c, list) and len(c) >= 2]
            return state
    except (OSError, ValueError, AttributeError):
        pass
    warnings.append("%s повреждён — перенесено всё, чего нет в схеме" % path)
    return None


def _format_state(state):
    rows = ",\n".join("    " + json.dumps(c, ensure_ascii=False) for c in state["connections"])
    return ('{\n  "version": %d,\n  "parts": %s,\n  "connections": [\n%s\n  ]\n}\n'
            % (state["version"], json.dumps(state["parts"], ensure_ascii=False), rows))


def write_diagram(diagram, path):
    text = format_diagram(diagram)
    json.loads(text)  # на всякий случай: файл обязан быть валидным JSON
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    _write_text(path, text)


def find_root_schematic(path):
    """Корневая схема по .kicad_pcb / .kicad_pro / .kicad_sch / папке проекта."""
    path = os.path.abspath(path)
    stem = None
    if os.path.isdir(path):
        folder = path
    else:
        folder, base = os.path.split(path)
        stem, ext = os.path.splitext(base)
        if ext == ".kicad_sch" and os.path.isfile(path):
            return path
    if not os.path.isdir(folder):
        return None
    names = sorted(os.listdir(folder))
    candidates = [stem + ".kicad_sch"] if stem else []
    candidates += [os.path.splitext(n)[0] + ".kicad_sch" for n in names if n.endswith(".kicad_pro")]
    for name in candidates:
        if os.path.isfile(os.path.join(folder, name)):
            return os.path.join(folder, name)
    schematics = [n for n in names if n.endswith(".kicad_sch")]
    return os.path.join(folder, schematics[0]) if len(schematics) == 1 else None


def export_project(sch_path, out_path=None, source=None, mapping_file=None):
    sch_path = os.path.abspath(sch_path)
    out_path = os.path.abspath(out_path or os.path.join(os.path.dirname(sch_path), OUTPUT_FILE_NAME))
    mapping = load_mapping(mapping_file)
    _log("Маппинг: %s%s" % (mapping.path, " (создан со значениями по умолчанию)" if mapping.created else ""))
    netlist = load_netlist(sch_path, source, mapping)
    existing = _load_existing(out_path) if SMART_MERGE else None
    st_path, state_warnings = state_path(out_path), []
    state = _load_state(st_path, state_warnings) if existing else None
    diagram, report, new_state = build_diagram(netlist, mapping, existing, state)
    report.warnings.extend(state_warnings)
    write_diagram(diagram, out_path)
    _write_text(st_path, _format_state(new_state))
    report.output = out_path
    for line in report.summary_lines():
        _log(line)
    _log("Сохранено: " + out_path)
    return diagram, report


# ═══════════════════════════════════════════════════════════════════════════
# 10. ACTION PLUGIN ДЛЯ KiCad
# ═══════════════════════════════════════════════════════════════════════════

def _show_message(text, title, error=False):
    try:
        import wx
    except ImportError:
        print(text)
        return
    try:
        from wx.lib.dialogs import ScrolledMessageDialog
        dlg = ScrolledMessageDialog(None, text, title, size=(760, 520))
        dlg.ShowModal()
        dlg.Destroy()
    except Exception:
        wx.MessageBox(text, title, wx.OK | (wx.ICON_ERROR if error else wx.ICON_INFORMATION))


def _ask_schematic(default_dir):
    import wx
    with wx.FileDialog(None, "Выберите корневую схему проекта", defaultDir=default_dir or "",
                       wildcard="Схема KiCad (*.kicad_sch)|*.kicad_sch",
                       style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
        return dlg.GetPath() if dlg.ShowModal() == wx.ID_OK else None


if pcbnew is not None:
    class WokwiExporterPlugin(pcbnew.ActionPlugin):
        """PCB Editor → Tools → External Plugins → Export to Wokwi (diagram.json)."""

        def defaults(self):
            self.name = "Export to Wokwi (diagram.json)"
            self.category = "Export"
            self.description = "Экспорт компонентов и цепей схемы в diagram.json для симулятора Wokwi"
            self.show_toolbar_button = True
            icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
            self.icon_file_name = icon if os.path.isfile(icon) else ""
            self.dark_icon_file_name = self.icon_file_name

        def Run(self):
            try:
                board = pcbnew.GetBoard()
                board_file = board.GetFileName() if board is not None else ""
                sch = find_root_schematic(board_file) if board_file else None
                if not sch:
                    sch = _ask_schematic(os.path.dirname(board_file) if board_file else "")
                    if not sch:
                        return
                _, report = export_project(sch)
                _show_message(report.text(), "Wokwi exporter")
            except ExportError as exc:
                _log("Экспорт остановлен: %s" % exc)
                _show_message("Экспорт остановлен:\n\n%s" % exc, "Wokwi exporter", error=True)
            except Exception:
                _log(traceback.format_exc())
                _show_message("Экспорт не удался:\n\n" + traceback.format_exc(), "Wokwi exporter", error=True)


# ═══════════════════════════════════════════════════════════════════════════
# 11. ЗАПУСК ИЗ КОМАНДНОЙ СТРОКИ
# ═══════════════════════════════════════════════════════════════════════════

def main(argv=None):
    global LAYOUT
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="KiCad → Wokwi diagram.json")
    ap.add_argument("path", help=".kicad_sch, .kicad_pro, .kicad_pcb или папка проекта")
    ap.add_argument("-o", "--output", help="куда сохранить (по умолчанию diagram.json в папке проекта)")
    ap.add_argument("--source", choices=["auto", "kicad-cli", "schematic"], default=NETLIST_SOURCE)
    ap.add_argument("--layout", choices=["grid", "schematic"], default=LAYOUT)
    ap.add_argument("--mapping", help="путь к mapping.json (по умолчанию рядом с этим скриптом)")
    args = ap.parse_args(argv)
    LAYOUT = args.layout
    sch = find_root_schematic(args.path)
    if not sch:
        ap.error("не найдена корневая схема для %s" % args.path)
    try:
        _, report = export_project(sch, args.output, args.source, args.mapping)
    except ExportError as exc:
        print("Экспорт остановлен: %s" % exc)
        return 1
    print()
    print(report.text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
