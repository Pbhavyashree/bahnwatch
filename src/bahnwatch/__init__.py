"""bahnwatch — commute alerting for Berlin public transport.

Tells you when to leave, not just that something is late. The distinction
matters: delay magnitude is the wrong alert trigger, and an alerting system
that cries wolf gets muted.
"""

from bahnwatch import baseline, bvg, commute, models

__version__ = "0.1.0"
__all__ = ["baseline", "bvg", "commute", "models"]
