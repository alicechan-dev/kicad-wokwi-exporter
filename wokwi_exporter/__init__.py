# -*- coding: utf-8 -*-
# KiCad 10 Action Plugin: экспорт схемы в Wokwi diagram.json.
# KiCad импортирует эту папку как пакет и регистрирует плагин в
# PCB Editor → Tools → External Plugins.
from .wokwi_exporter import WokwiExporterPlugin

WokwiExporterPlugin().register()
