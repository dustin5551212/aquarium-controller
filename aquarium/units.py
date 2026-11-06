"""Metric definitions and unit conversions.

The controller stores every reading in a canonical unit.  Where a hobbyist
thinks in a different unit (TDS in ppm, salinity in ppt) the value is derived
from a base metric so it stays consistent across sensors and alerts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetricInfo:
    key: str
    label: str
    unit: str
    low: float          # plausible range for a healthy probe
    high: float
    precision: int
    group: str = "water"

    def fmt(self, value: float) -> str:
        return f"{value:.{self.precision}f}"


METRICS: dict[str, MetricInfo] = {
    "temp_c": MetricInfo("temp_c", "Temperature", "°C", 0.0, 40.0, 2, "physical"),
    "ph": MetricInfo("ph", "pH", "", 0.0, 14.0, 2, "chemistry"),
    "ec_us": MetricInfo("ec_us", "Conductivity", "µS/cm", 0.0, 100_000.0, 0, "chemistry"),
    "salinity_ppt": MetricInfo("salinity_ppt", "Salinity", "ppt", 0.0, 50.0, 2, "chemistry"),
    "tds_ppm": MetricInfo("tds_ppm", "TDS", "ppm", 0.0, 60_000.0, 0, "chemistry"),
    "orp_mv": MetricInfo("orp_mv", "ORP", "mV", -500.0, 1200.0, 0, "chemistry"),
    "do_mg_l": MetricInfo("do_mg_l", "Dissolved O₂", "mg/L", 0.0, 20.0, 2, "chemistry"),
    "level_pct": MetricInfo("level_pct", "Sump level", "%", 0.0, 100.0, 1, "physical"),
    "turbidity_ntu": MetricInfo("turbidity_ntu", "Turbidity", "NTU", 0.0, 4000.0, 0, "physical"),
}

# metric -> (source metric, conversion key)
DERIVED: dict[str, tuple[str, str]] = {
    "salinity_ppt": ("ec_us", "ec_to_salinity_ppt"),
    "tds_ppm": ("ec_us", "ec_to_tds_ppm"),
}


def ec_to_salinity_ppt(ec_us: float, factor: float = 0.66) -> float:
    """Approximate practical salinity from conductivity.

    Seawater at 35 ppt reads ~53 mS/cm (53 000 µS/cm), hence 0.66 ppt per
    mS/cm.  Good to ~±0.5 ppt after a one-point calibration; fine for trend
    and for ATO supervision, not a replacement for a refractometer.
    """
    return max(0.0, ec_us / 1000.0 * factor)


def ec_to_tds_ppm(ec_us: float, factor: float = 0.5) -> float:
    """TDS from conductivity using the common 0.5 ppm per µS/cm assumption."""
    return max(0.0, ec_us * factor)


CONVERTERS = {
    "ec_to_salinity_ppt": ec_to_salinity_ppt,
    "ec_to_tds_ppm": ec_to_tds_ppm,
}


def metric_info(key: str) -> MetricInfo:
    return METRICS.get(key) or MetricInfo(key, key, "", float("-inf"), float("inf"), 1)


def is_plausible(key: str, value: float) -> bool:
    info = metric_info(key)
    return info.low <= value <= info.high
