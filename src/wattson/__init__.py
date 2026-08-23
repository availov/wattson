"""Wattson — laptop power and cooling: fans, charging, power draw.

Fan control works on ASUS only (the ``asus_custom_fan_curve`` driver); the
charge threshold and the power audit work on any machine whose kernel
exposes them.
"""

__version__ = '1.2.0'
