# read version from installed package
from importlib.metadata import version

__version__ = version("dec_ansi_parser")

from . import formatter
from .parser import *
