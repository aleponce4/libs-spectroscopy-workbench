"""Plate autosave helpers for acquisition high-throughput mode."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
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
PLATE_STATE_FILENAME = "_plate_state.json"
PLATE_FILE_PATTERN = re.compile(
    r"^(?P<safe_plate_name>.+)_(?P<well>[A-Z]\d+)_shot(?P<shot_number>\d+)_"
    r"(?P<timestamp>\d{8}_\d{6})_(?P<shot_index>\d+)\.csv$"
)


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

    @classmethod
    def from_mapping(cls, values: dict[str, Any], plate_dir: str | None = None) -> "PlateShotRecord":
        filepath = str(values.get("filepath", ""))
        if plate_dir and filepath and not os.path.isabs(filepath):
            filepath = os.path.join(plate_dir, filepath)

        return cls(
            well=str(values.get("well", "")),
            shot_number=int(values.get("shot_number", 0)),
            shot_index=int(values.get("shot_index", 0)),
            filepath=filepath,
        )

    def to_mapping(self, plate_dir: str | None = None) -> dict[str, Any]:
        filepath = self.filepath
        if plate_dir and filepath:
            try:
                filepath = os.path.relpath(filepath, plate_dir)
            except ValueError:
                pass

        return {
            "well": self.well,
            "shot_number": self.shot_number,
            "shot_index": self.shot_index,
            "filepath": filepath,
        }


@dataclass
class PlateRunState:
    config: PlateAutosaveConfig
    shots_by_well: dict[str, int] = field(default_factory=dict)
    history: list[PlateShotRecord] = field(default_factory=list)

    def __post_init__(self):
        for well in self.config.ordered_wells:
            self.shots_by_well.setdefault(well, 0)

    @classmethod
    def from_mapping(cls, values: dict[str, Any], plate_dir: str | None = None) -> "PlateRunState":
        config = values.get("config")
        if isinstance(config, dict):
            config_obj = PlateAutosaveConfig.from_mapping(config)
        else:
            config_obj = PlateAutosaveConfig.from_mapping(values)

        state = cls(config_obj)

        raw_counts = values.get("shots_by_well", {})
        if isinstance(raw_counts, dict):
            for well, count in raw_counts.items():
                if well in state.shots_by_well:
                    state.shots_by_well[well] = max(0, min(int(count), config_obj.shots_per_well))

        history = []
        for item in values.get("history", []):
            if not isinstance(item, dict):
                continue
            record = PlateShotRecord.from_mapping(item, plate_dir=plate_dir)
            if record.well in state.shots_by_well:
                history.append(record)

        state.history = sorted(
            history,
            key=lambda record: (record.shot_index, record.well, record.shot_number, record.filepath),
        )

        if state.history:
            rebuilt = cls.from_records(config_obj, state.history)
            rebuilt.shots_by_well.update(state.shots_by_well)
            rebuilt.history = state.history
            for well in rebuilt.shots_by_well:
                rebuilt.shots_by_well[well] = max(
                    rebuilt.shots_by_well[well],
                    state.shots_by_well.get(well, 0),
                )
            return rebuilt

        return state

    @classmethod
    def from_records(cls, config: PlateAutosaveConfig, records: list[PlateShotRecord]) -> "PlateRunState":
        state = cls(config)
        shots_seen = {well: set() for well in config.ordered_wells}

        for record in sorted(records, key=lambda item: (item.shot_index, item.well, item.shot_number, item.filepath)):
            if record.well not in shots_seen:
                raise ValueError(f"Found saved well {record.well} outside the selected plate layout.")
            if record.shot_number < 1 or record.shot_number > config.shots_per_well:
                raise ValueError(
                    f"Found {record.well} shot {record.shot_number}, which exceeds the selected shots-per-well."
                )
            if record.shot_number in shots_seen[record.well]:
                raise ValueError(f"Found duplicate saved data for {record.well} shot {record.shot_number}.")

            shots_seen[record.well].add(record.shot_number)
            state.history.append(record)

        for well, seen in shots_seen.items():
            if seen and seen != set(range(1, len(seen) + 1)):
                raise ValueError(f"Saved files for {well} are missing a shot number in the middle.")
            state.shots_by_well[well] = len(seen)

        return state

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

    def to_mapping(self, plate_dir: str | None = None, **extra: Any) -> dict[str, Any]:
        data = {
            "config": {
                "plate_type": self.config.plate_type,
                "plate_name": self.config.plate_name,
                "shots_per_well": self.config.shots_per_well,
                "order_mode": self.config.order_mode,
            },
            "shots_by_well": dict(self.shots_by_well),
            "history": [record.to_mapping(plate_dir=plate_dir) for record in self.history],
            "complete": self.is_complete,
        }
        data.update(extra)
        return data


def plate_state_path(plate_dir: str) -> str:
    return os.path.join(plate_dir, PLATE_STATE_FILENAME)


def save_plate_run_state(plate_dir: str, state: PlateRunState, *, closed_early: bool = False) -> str:
    os.makedirs(plate_dir, exist_ok=True)
    filepath = plate_state_path(plate_dir)
    payload = state.to_mapping(plate_dir=plate_dir, closed_early=closed_early, version=1)
    payload["complete"] = bool(state.is_complete and not closed_early)

    with open(filepath, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    return filepath


def load_plate_run_state(plate_dir: str) -> tuple[PlateRunState, dict[str, Any]] | None:
    filepath = plate_state_path(plate_dir)
    if not os.path.isfile(filepath):
        return None

    with open(filepath, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError("Plate state file must contain a JSON object.")

    return PlateRunState.from_mapping(payload, plate_dir=plate_dir), payload


def parse_plate_filename(filename: str, expected_safe_plate_name: str | None = None) -> dict[str, Any] | None:
    match = PLATE_FILE_PATTERN.match(filename)
    if not match:
        return None

    data = match.groupdict()
    if expected_safe_plate_name and data["safe_plate_name"] != expected_safe_plate_name:
        return None

    return {
        "safe_plate_name": data["safe_plate_name"],
        "well": data["well"],
        "shot_number": int(data["shot_number"]),
        "timestamp": data["timestamp"],
        "shot_index": int(data["shot_index"]),
    }


def discover_resumable_plate_runs(save_directory: str) -> list[dict[str, Any]]:
    if not os.path.isdir(save_directory):
        return []

    candidates: list[dict[str, Any]] = []
    for entry in sorted(os.scandir(save_directory), key=lambda item: item.name.lower()):
        if not entry.is_dir():
            continue

        try:
            candidate = _resume_candidate_from_state(entry.path)
            if candidate is None:
                candidate = _resume_candidate_from_files(entry.path)
        except Exception:
            candidate = None

        if candidate is not None:
            candidates.append(candidate)

    return candidates


def _resume_candidate_from_state(plate_dir: str) -> dict[str, Any] | None:
    try:
        loaded = load_plate_run_state(plate_dir)
    except Exception:
        return None

    if loaded is None:
        return None

    state, metadata = loaded
    if metadata.get("closed_early") or metadata.get("complete"):
        return None

    payload = state.progress_payload()
    return {
        "plate_dir": plate_dir,
        "plate_name": state.config.plate_name,
        "safe_plate_name": state.config.safe_plate_name,
        "state": state,
        "records": list(state.history),
        "payload": payload,
        "source_label": "Saved state",
        "needs_confirmation": False,
    }


def _resume_candidate_from_files(plate_dir: str) -> dict[str, Any] | None:
    safe_plate_name = os.path.basename(plate_dir)
    records: list[PlateShotRecord] = []

    for entry in sorted(os.scandir(plate_dir), key=lambda item: item.name.lower()):
        if not entry.is_file():
            continue
        parsed = parse_plate_filename(entry.name, expected_safe_plate_name=safe_plate_name)
        if parsed is None:
            continue

        records.append(
            PlateShotRecord(
                well=parsed["well"],
                shot_number=parsed["shot_number"],
                shot_index=parsed["shot_index"],
                filepath=entry.path,
            )
        )

    if not records:
        return None

    guessed_plate_type = _guess_plate_type(records)
    guessed_order_mode = _guess_order_mode(records, guessed_plate_type)
    guessed_shots_per_well = max(record.shot_number for record in records)
    guessed_config = PlateAutosaveConfig(
        plate_type=guessed_plate_type,
        plate_name=safe_plate_name,
        shots_per_well=max(1, guessed_shots_per_well),
        order_mode=guessed_order_mode,
    )
    state = PlateRunState.from_records(guessed_config, records)

    return {
        "plate_dir": plate_dir,
        "plate_name": guessed_config.plate_name,
        "safe_plate_name": guessed_config.safe_plate_name,
        "state": state,
        "records": records,
        "payload": state.progress_payload(),
        "source_label": "Scanned files",
        "needs_confirmation": True,
    }


def _guess_plate_type(records: list[PlateShotRecord]) -> int:
    min_rows = 1
    min_columns = 1
    for record in records:
        row_index = ord(record.well[0]) - 64
        column_index = int(record.well[1:])
        min_rows = max(min_rows, row_index)
        min_columns = max(min_columns, column_index)

    compatible = [
        plate_type
        for plate_type, (rows, columns) in PLATE_FORMATS.items()
        if rows >= min_rows and columns >= min_columns
    ]
    return compatible[0] if compatible else 96


def _guess_order_mode(records: list[PlateShotRecord], plate_type: int) -> str:
    rows, columns = PLATE_FORMATS[plate_type]
    observed_wells = []
    for record in sorted(records, key=lambda item: (item.shot_index, item.well, item.shot_number, item.filepath)):
        if not observed_wells or observed_wells[-1] != record.well:
            observed_wells.append(record.well)

    row_order = ordered_wells(rows, columns, ORDER_ROW)
    column_order = ordered_wells(rows, columns, ORDER_COLUMN)
    if observed_wells and observed_wells == row_order[:len(observed_wells)]:
        return ORDER_ROW
    if observed_wells and observed_wells == column_order[:len(observed_wells)]:
        return ORDER_COLUMN
    return ORDER_ROW


def _unique_path(directory: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}_{counter}{ext}")
        counter += 1
    return candidate
