"""Plate autosave helpers for acquisition high-throughput mode."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import shutil
from typing import Any


PLATE_FORMATS: dict[int, tuple[int, int]] = {
    6: (2, 3),
    12: (3, 4),
    24: (4, 6),
    48: (6, 8),
    96: (8, 12),
    384: (16, 24),
}

ORDER_ROW = "row"
ORDER_COLUMN = "column"
ORDER_LABELS = {
    ORDER_ROW: "Row by row",
    ORDER_COLUMN: "Column by column",
}


def sanitize_filename_part(value: str, fallback: str = "Plate") -> str:
    """Return a filesystem-friendly name segment."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or fallback


def well_labels(rows: int, columns: int) -> list[str]:
    """Return standard multiwell labels such as A1 and B12."""
    return [f"{chr(65 + row)}{column}" for row in range(rows) for column in range(1, columns + 1)]


def ordered_wells(rows: int, columns: int, order_mode: str) -> list[str]:
    """Return the straight row-by-row or column-by-column acquisition order."""
    if order_mode == ORDER_COLUMN:
        return [f"{chr(65 + row)}{column}" for column in range(1, columns + 1) for row in range(rows)]
    return well_labels(rows, columns)


@dataclass(frozen=True)
class PlateAutosaveConfig:
    plate_type: int = 96
    plate_name: str = "Plate"
    shots_per_well: int = 1
    order_mode: str = ORDER_ROW

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "PlateAutosaveConfig":
        plate_type = int(values.get("plate_type", 96))
        if plate_type not in PLATE_FORMATS:
            plate_type = 96

        shots_per_well = max(1, int(values.get("shots_per_well", 1)))
        order_mode = values.get("order_mode", ORDER_ROW)
        if order_mode not in ORDER_LABELS:
            order_mode = ORDER_ROW

        plate_name = str(values.get("plate_name", "Plate")).strip() or "Plate"
        return cls(
            plate_type=plate_type,
            plate_name=plate_name,
            shots_per_well=shots_per_well,
            order_mode=order_mode,
        )

    @property
    def rows(self) -> int:
        return PLATE_FORMATS[self.plate_type][0]

    @property
    def columns(self) -> int:
        return PLATE_FORMATS[self.plate_type][1]

    @property
    def safe_plate_name(self) -> str:
        return sanitize_filename_part(self.plate_name)

    @property
    def ordered_wells(self) -> list[str]:
        return ordered_wells(self.rows, self.columns, self.order_mode)


@dataclass
class PlateShotRecord:
    well: str
    shot_number: int
    shot_index: int
    filepath: str


@dataclass
class PlateRunState:
    config: PlateAutosaveConfig
    shots_by_well: dict[str, int] = field(default_factory=dict)
    history: list[PlateShotRecord] = field(default_factory=list)

    def __post_init__(self):
        for well in self.config.ordered_wells:
            self.shots_by_well.setdefault(well, 0)

    @property
    def is_complete(self) -> bool:
        return self.current_well() is None

    def current_well(self) -> str | None:
        for well in self.config.ordered_wells:
            if self.shots_by_well.get(well, 0) < self.config.shots_per_well:
                return well
        return None

    def next_assignment(self) -> tuple[str, int] | None:
        well = self.current_well()
        if well is None:
            return None
        return well, self.shots_by_well.get(well, 0) + 1

    def record_saved(self, filepath: str, shot_index: int) -> dict[str, Any]:
        assignment = self.next_assignment()
        if assignment is None:
            raise RuntimeError("Plate is already complete.")

        well, shot_number = assignment
        self.shots_by_well[well] = shot_number
        self.history.append(
            PlateShotRecord(
                well=well,
                shot_number=shot_number,
                shot_index=shot_index,
                filepath=filepath,
            )
        )
        return self.progress_payload(last_saved=filepath)

    def discard_last(self, discarded_dir: str) -> tuple[PlateShotRecord | None, dict[str, Any]]:
        if not self.history:
            return None, self.progress_payload()

        record = self.history.pop()
        self.shots_by_well[record.well] = max(0, self.shots_by_well.get(record.well, 0) - 1)

        if os.path.exists(record.filepath):
            os.makedirs(discarded_dir, exist_ok=True)
            record.filepath = shutil.move(record.filepath, _unique_path(discarded_dir, os.path.basename(record.filepath)))

        return record, self.progress_payload(discarded=record.filepath)

    def progress_payload(
        self,
        last_saved: str | None = None,
        discarded: str | None = None,
    ) -> dict[str, Any]:
        total_wells = len(self.config.ordered_wells)
        complete_wells = sum(
            1 for count in self.shots_by_well.values()
            if count >= self.config.shots_per_well
        )
        total_shots = total_wells * self.config.shots_per_well
        saved_shots = sum(self.shots_by_well.values())

        return {
            "plate_type": self.config.plate_type,
            "plate_name": self.config.plate_name,
            "safe_plate_name": self.config.safe_plate_name,
            "rows": self.config.rows,
            "columns": self.config.columns,
            "order_mode": self.config.order_mode,
            "order_label": ORDER_LABELS[self.config.order_mode],
            "shots_per_well": self.config.shots_per_well,
            "shots_by_well": dict(self.shots_by_well),
            "current_well": self.current_well(),
            "complete": self.is_complete,
            "complete_wells": complete_wells,
            "total_wells": total_wells,
            "saved_shots": saved_shots,
            "total_shots": total_shots,
            "last_saved": last_saved,
            "discarded": discarded,
            "can_discard": bool(self.history),
        }


def _unique_path(directory: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}_{counter}{ext}")
        counter += 1
    return candidate
