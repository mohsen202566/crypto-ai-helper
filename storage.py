"""ذخیره تنظیمات، آمار و سیگنال‌ها با JSON ساده."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

import config
from utils import now_utc_iso


def _default_state() -> dict[str, Any]:
    return {
        "settings": {
            "trade_amount_usdt": config.DEFAULT_TRADE_AMOUNT_USDT,
            "leverage": config.DEFAULT_LEVERAGE,
            "max_positions": config.DEFAULT_MAX_POSITIONS,
            "trade_enabled": config.DEFAULT_TRADE_ENABLED,
            "margin_type": config.DEFAULT_MARGIN_TYPE,
        },
        "stats": {
            "signals_total": 0,
            "normal_signals_total": 0,
            "real_signals_total": 0,
            "normal_tp": 0,
            "normal_sl": 0,
            "normal_open": 0,
            "normal_pnl": 0.0,
            "real_tp": 0,
            "real_sl": 0,
            "real_open": 0,
            "real_failed": 0,
            "real_pnl": 0.0,
            "last_reset_utc": now_utc_iso(),
        },
        "signals": {},
        "runtime": {
            "last_symbol_errors": {},
            "validated_symbols": {},
        },
    }


class JSONStorage:
    def __init__(self, path: Path = config.STATE_FILE):
        self.path = path
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            state = _default_state()
            self._write_state(state)
            return state
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            default = _default_state()
            for key, val in default.items():
                if key not in data:
                    data[key] = copy.deepcopy(val)
            for key, val in default["settings"].items():
                data["settings"].setdefault(key, val)
            for key, val in default["stats"].items():
                data["stats"].setdefault(key, val)
            data.setdefault("signals", {})
            data.setdefault("runtime", copy.deepcopy(default["runtime"]))
            data["runtime"].setdefault("last_symbol_errors", {})
            data["runtime"].setdefault("validated_symbols", {})
            return data
        except Exception:
            backup = self.path.with_suffix(".broken.json")
            try:
                self.path.rename(backup)
            except Exception:
                pass
            state = _default_state()
            self._write_state(state)
            return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def save(self) -> None:
        with self._lock:
            self._write_state(self.state)

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.state["settings"])

    def update_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self.state["settings"][key] = value
            self.save()

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.state["stats"])

    def inc_stat(self, key: str, amount: float = 1) -> None:
        with self._lock:
            current = self.state["stats"].get(key, 0)
            self.state["stats"][key] = current + amount
            # شمارنده‌های باز منفی نشوند
            if key in ("normal_open", "real_open") and self.state["stats"][key] < 0:
                self.state["stats"][key] = 0
            self.save()

    def reset_stats(self, clear_signals: bool = True) -> None:
        with self._lock:
            self.state["stats"] = _default_state()["stats"]
            if clear_signals:
                self.state["signals"] = {}
            self.save()

    def save_signal(self, signal: dict[str, Any]) -> None:
        with self._lock:
            self.state["signals"][signal["signal_id"]] = copy.deepcopy(signal)
            self.save()

    def update_signal(self, signal_id: str, **updates: Any) -> None:
        with self._lock:
            if signal_id in self.state["signals"]:
                self.state["signals"][signal_id].update(updates)
                self.save()

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self._lock:
            signal = self.state["signals"].get(signal_id)
            return copy.deepcopy(signal) if signal else None

    def all_signals(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.state["signals"])

    def active_signals(self) -> list[dict[str, Any]]:
        """سیگنال‌های عادی/داخلی که هنوز نتیجه نگرفته‌اند."""
        with self._lock:
            out = []
            for s in self.state["signals"].values():
                mode = str(s.get("execution_mode") or "NORMAL").upper()
                if mode == "NORMAL" and not s.get("normal_result"):
                    out.append(copy.deepcopy(s))
            return out

    def active_real_signals(self) -> list[dict[str, Any]]:
        """سیگنال‌های رئال که هنوز نتیجه واقعی ندارند، حتی اگر تایید سفارش در انتظار باشد."""
        with self._lock:
            out = []
            for s in self.state["signals"].values():
                mode = str(s.get("execution_mode") or "NORMAL").upper()
                if mode == "REAL" and not s.get("real_result"):
                    out.append(copy.deepcopy(s))
            return out

    def has_active_symbol(self, internal_symbol: str) -> bool:
        """از هر ارز فقط یک سیگنال تا بسته‌شدن کامل مجاز است.

        عادی با normal_result بسته می‌شود؛ رئال با real_result بسته می‌شود.
        اگر رئال بعداً به عادی downgrade شود، normal_result ملاک بسته‌شدن است.
        """
        with self._lock:
            for s in self.state["signals"].values():
                if s.get("symbol") != internal_symbol:
                    continue
                mode = str(s.get("execution_mode") or "NORMAL").upper()
                if mode == "REAL":
                    if not s.get("real_result"):
                        return True
                else:
                    if not s.get("normal_result"):
                        return True
            return False

    def count_open_real(self) -> int:
        """اسلات‌های رئال رزروشده/باز؛ تا ثبت real_result آزاد نمی‌شود."""
        with self._lock:
            return sum(
                1
                for s in self.state["signals"].values()
                if str(s.get("execution_mode") or "").upper() == "REAL" and not s.get("real_result")
            )

    def set_validated_symbols(self, mapping: dict[str, Any]) -> None:
        with self._lock:
            self.state["runtime"]["validated_symbols"] = copy.deepcopy(mapping)
            self.save()

    def get_validated_symbols(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.state["runtime"].get("validated_symbols", {}))

    def set_symbol_error(self, symbol: str, message: str, ts: float) -> None:
        with self._lock:
            self.state["runtime"].setdefault("last_symbol_errors", {})[symbol] = {"message": message, "ts": ts}
            self.save()
