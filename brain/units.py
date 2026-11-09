"""Unit helpers. Everything user-facing is US units: mi, °F, mph, kWh, Wh/mi."""

from __future__ import annotations

from typing import Optional

KM_PER_MI = 1.609344


def km_to_mi(value: float) -> float:
    return value / KM_PER_MI


def c_to_f(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def kph_to_mph(value: float) -> float:
    return value / KM_PER_MI


def wh_per_km_to_wh_per_mi(value: float) -> float:
    return value * KM_PER_MI


def convert_distance(value: float, unit: Optional[str]) -> float:
    """Convert a distance reading in ``unit`` to miles (unknown/None units are taken as miles)."""
    u = (unit or "mi").strip().lower()
    if u in ("km", "kilometer", "kilometers"):
        return km_to_mi(value)
    if u in ("m", "meter", "meters"):
        return km_to_mi(value / 1000.0)
    return value


def convert_temperature(value: float, unit: Optional[str]) -> float:
    """Convert a temperature reading in ``unit`` to °F (unknown/None units are taken as °F)."""
    u = (unit or "°F").strip().lower().replace("º", "°")
    if u in ("°c", "c", "celsius"):
        return c_to_f(value)
    if u in ("k", "kelvin"):
        return c_to_f(value - 273.15)
    return value


def convert_speed(value: float, unit: Optional[str]) -> float:
    """Convert a speed reading in ``unit`` to mph (unknown/None units are taken as mph)."""
    u = (unit or "mph").strip().lower()
    if u in ("km/h", "kph", "kmh"):
        return kph_to_mph(value)
    if u in ("m/s",):
        return value * 2.236936
    return value


def convert_power(value: float, unit: Optional[str]) -> float:
    """Convert a power reading in ``unit`` to kW (unknown/None units are taken as kW)."""
    u = (unit or "kW").strip().lower()
    if u == "w":
        return value / 1000.0
    if u == "mw":
        return value * 1000.0
    return value


def convert_energy(value: float, unit: Optional[str]) -> float:
    """Convert an energy reading in ``unit`` to kWh (unknown/None units are taken as kWh)."""
    u = (unit or "kWh").strip().lower()
    if u == "wh":
        return value / 1000.0
    if u == "mwh":
        return value * 1000.0
    return value


def convert_by_dimension(value: float, dimension: str, unit: Optional[str]) -> float:
    """Dispatch on a profile ``unit`` dimension name (distance, temperature, speed, power, energy)."""
    if dimension == "distance":
        return convert_distance(value, unit)
    if dimension == "temperature":
        return convert_temperature(value, unit)
    if dimension == "speed":
        return convert_speed(value, unit)
    if dimension == "power":
        return convert_power(value, unit)
    if dimension == "energy":
        return convert_energy(value, unit)
    return value
