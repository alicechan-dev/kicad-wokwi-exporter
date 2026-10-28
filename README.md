**English** | [Русский](README.ru.md)

# KiCad 10 → Wokwi: `diagram.json` exporter

An Action Plugin for KiCad 10. It takes the schematic of the open project and writes a ready-to-use
`diagram.json` for the [Wokwi](https://wokwi.com) simulator into the project root (next to `.kicad_pro`).

```
wokwi_exporter/
├── __init__.py        registers the plugin in KiCad
├── wokwi_exporter.py  all the code: netlist, merging, JSON generation
├── icon.png           toolbar button icon
└── mapping.json       your part mapping — created on the first export, not in the repository
```

The report window, the comments in `mapping.json` and the code comments are in Russian.

## Installation (Windows, KiCad 10)

1. Copy the whole `wokwi_exporter` **folder** into

   ```
   %USERPROFILE%\Documents\KiCad\10.0\scripting\plugins\
   ```

   You should end up with
   `...\Documents\KiCad\10.0\scripting\plugins\wokwi_exporter\__init__.py`.
   Create the `scripting\plugins` folder if it doesn't exist. KiCad itself always shows the exact folder:
   **PCB Editor → Tools → External Plugins → Open Plugin Directory**.
   KiCad 10 also looks for plugins in `%APPDATA%\kicad\10.0\scripting\plugins\`.
2. In the PCB Editor: **Tools → External Plugins → Refresh Plugins** (or restart KiCad).
3. **Export to Wokwi (diagram.json)** appears under **Tools → External Plugins**, plus a green
   button on the toolbar.

macOS: `~/Documents/KiCad/10.0/scripting/plugins/`, Linux: `~/.local/share/kicad/10.0/scripting/plugins/`.

**Updating the plugin:** copy the new `__init__.py`, `wokwi_exporter.py` and `icon.png` over the old ones.
Don't delete the whole folder: your `mapping.json` lives there.

## Usage

1. **Save the schematic.** The plugin reads the `.kicad_sch` file from disk.
2. Open the project's board in the **PCB Editor**. KiCad Action Plugins only run in the PCB Editor;
   the board itself can be empty.
3. **Tools → External Plugins → Export to Wokwi (diagram.json)**.
4. The report window shows what was exported, what was carried over from the previous
   `diagram.json`, which parts were skipped and which pins have no Wokwi counterpart. The same numbers
   are printed to the console (`[Wokwi] …` lines). The previous file is kept as `diagram.json.bak`.
5. In Wokwi, open the project's `diagram.json` tab and paste the file contents.
   Wokwi still needs a sketch (`sketch.ino` / `main.py`).

## Re-exporting and edits made in Wokwi

If `diagram.json` already exists, the plugin doesn't overwrite it but merges the new export into it
(`SMART_MERGE = True`). To keep the changes you made in the browser, copy `diagram.json` from Wokwi
into the project folder before the next export.

| What | What happens on re-export |
|---|---|
| A part or wire from the KiCad schematic | Updated from the schematic; rotation, manual attributes and wire routing from Wokwi are kept |
| Position of a part and colour of a wire from KiCad | Kept if you changed them in Wokwi; otherwise the plugin sets them again (new layout, new colours) |
| A part or wire added by hand in Wokwi (a logic analyzer, etc.) | Carried over as is |
| A part or wire deleted or rewired in the KiCad schematic | Removed |
| A manual wire to a part that no longer exists | Dropped and listed in the report |
| A manual part whose id clashes with a new part from KiCad | Renamed to `<id>_wokwi`, its wires are reconnected |
| A schematic part that was deleted only in Wokwi | Comes back: the schematic is the source of truth for it |

The `.diagram.kicad-state.json` file next to `diagram.json` tells manual edits apart from stale items:
it records what the previous export created, together with the positions and colours the plugin set.
Without it (the first export after updating the plugin) everything that isn't in the schematic is carried
over and positions and colours from the old file are left untouched; the report warns about this. To
re-lay out and recolour everything once, set `RESET_LAYOUT = True` (or pass `--reset-layout` on the
command line): parts added by hand in Wokwi stay where they are. If nothing should ever be removed, set
`DROP_DELETED_IN_KICAD = False`. But then, for example, after moving an LED from D13 to D12 the old wire to
D13 stays and shorts the pins in the simulation.

The plugin never overwrites a corrupted `diagram.json`: the export stops with an error message.

## Layout and wire colours

**Layout** (`LAYOUT = "grid"`). The board is centred on `[0, 0]`, peripherals are placed in groups in
columns to its left and right:

* a group is a set of parts linked by a signal net: a resistor and its LED, three resistors and an
  RGB LED, a button with its pull-up. Parts sitting directly on board pins are stacked closest to the
  board, the next parts along the net go behind them;
* groups follow the order of board pins and go to whichever side currently has the shorter column;
  a column taller than `LAYOUT_COLUMN_MAX_HEIGHT` continues in a new column further from the board;
* part sizes come from `PART_SIZES` (bounding boxes of the Wokwi SVGs) with `LAYOUT_PADDING` px
  between parts, so an LCD or a servo doesn't overlap its neighbours. Unknown types use `DEFAULT_PART_SIZE`;
* parts you moved in Wokwi and parts added there by hand stay in place, and new parts are placed
  around them.

`LAYOUT = "schematic"` keeps the relative placement from the KiCad schematic (ignoring part sizes).

**Colours** follow the KiCad net name (`NET_COLOR_RULES`); if the name is auto-generated
(`Net-(R1-Pad2)`), the pin names at the ends of the wire are used instead:

| Name contains | Colour |
|---|---|
| `GND`, `VSS` | black |
| `VCC`, `VDD`, `5V`, `3V3`, `3.3V`, `VIN`, `VBUS`, `VBAT`, `+12V`… | red |
| `SDA`, `RX` (`RXD`, `RX0`, `U0RXD`) | green |
| `SCL`, `TX` | blue |
| anything else | cycles through `WIRE_COLORS` (`orange`, `purple`, `yellow`, `cyan`, …) in board pin order, so neighbouring wires differ |

## Adding your own part

The mapping lives in `mapping.json` next to the plugin (in the `plugins\wokwi_exporter` folder). The
file is created from the built-in defaults on the first export and is read on every export after that.
Updating the plugin doesn't touch it. To restore the defaults, delete the file.

```json
{
  "parts": {
    "LED": {"type": "wokwi-led", "attrs": {"color": "red"}, "pins": {"A": "A", "K": "C"}},

    "MyRelayModule": {"type": "wokwi-relay-module", "pins": {"VCC": "VCC", "GND": "GND", "IN": "IN"}},

    "ESP32-DevKitC-32E": {"type": "board-esp32-devkit-c-v4", "role": "mcu", "pin_style": "esp32"},

    "C": null
  },
  "board_pins": {
    "wokwi-arduino-uno": ["0", "1", "2", "…"]
  }
}
```

* The key is the KiCad symbol name, the full `lib_id` or the Value field; `*` patterns are allowed. Keys
  starting with `_` are treated as comments.
* `pins` maps a KiCad pin (name or number) to a Wokwi pin. Wokwi part types and their pin names:
  <https://docs.wokwi.com/parts>.
* `pin_style`: `arduino` (`D13` → `13`), `esp32` (`IO2` → `2`, `TXD0` → `TX`), `pico` (`GPIO5` → `GP5`),
  `stm32` (`PA0` → `A0`). All ground and power pins collapse to one board pin (`GND.1`, `5V` …).
* `null` instead of a description skips the part silently (that's how capacitors are handled).
* `board_pins` lists the pins of Wokwi boards; add your board here if you connect it with `pin_style`.

If `mapping.json` contains a JSON error, the export stops and shows the line number. The file is not
overwritten.

Supported out of the box: Arduino Uno/Nano, ESP32-WROOM-32 (as DevKitC v4), Raspberry Pi Pico / Pico W,
STM32F103C8 (as Blue Pill), LEDs (including RGB), resistors (value taken from Value), potentiometers,
buttons, switches, a buzzer, a servo, DHT11/22, HC-SR04, SSD1306 OLED, LCD 1602.

## Where the connections come from

By default (`NETLIST_SOURCE = "auto"`) the plugin runs `kicad-cli sch export netlist`, so the
connectivity is computed by KiCad itself: hierarchy, buses, labels — exactly as in ERC.
If `kicad-cli` isn't found, the built-in `.kicad_sch` parser is used. It understands wires, junctions,
local, global and hierarchical labels, power symbols and nested sheets, but not buses that run between
sheets. `kicad-cli` doesn't export parts marked "Exclude from board"; if such parts are in `mapping.json`,
the plugin switches to the built-in parser automatically.

## Running without KiCad (for debugging the mapping)

```bash
"C:\Program Files\KiCad\10.0\bin\python.exe" wokwi_exporter\wokwi_exporter.py D:\path\to\project.kicad_pro
```

Options: `--source auto|kicad-cli|schematic`, `--layout grid|schematic`, `-o path\diagram.json`,
`--mapping path\mapping.json` (defaults to the one next to the script), `--reset-layout`.

## About the KiCad 10 API

KiCad 10 deprecates the SWIG `pcbnew` module in favour of the new IPC API, but still ships it
(checked on 10.0.7), so the plugin uses the classic `pcbnew.ActionPlugin`.
The export logic doesn't depend on `pcbnew` and also works from the command line.
