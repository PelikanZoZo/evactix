from .astro import AstroCollector
from .calendar import CalendarCollector
from .cbr import CbrFxCollector, CbrKeyRateCollector, CbrMetalsCollector
from .hazards import GdacsCollector, UsgsQuakesCollector
from .metno import MetnoCollector

ALL = [CbrFxCollector, CbrMetalsCollector, CbrKeyRateCollector, CalendarCollector, AstroCollector,
       MetnoCollector, UsgsQuakesCollector, GdacsCollector]
