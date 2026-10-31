from .astro import AstroCollector
from .calendar import CalendarCollector
from .cbr import CbrFxCollector, CbrKeyRateCollector, CbrMetalsCollector
from .hazards import GdacsCollector, UsgsQuakesCollector
from .consensus import WeatherConsensusCollector
from .dwd import DwdMosmixCollector
from .ecmwf import EcmwfIfsCollector
from .gfs import GfsCollector
from .metno import MetnoCollector

ALL = [CbrFxCollector, CbrMetalsCollector, CbrKeyRateCollector, CalendarCollector, AstroCollector,
       MetnoCollector, UsgsQuakesCollector, GdacsCollector,
       DwdMosmixCollector, GfsCollector, EcmwfIfsCollector, WeatherConsensusCollector]
