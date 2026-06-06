"""Loads a pool of tenant users from users.csv for simulated caller identity."""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SimulatedUser:
    user_id: str
    user_name: str
    user_email: str


class UserPool:
    def __init__(self, users: list[SimulatedUser]) -> None:
        if not users:
            raise ValueError("User pool is empty.")
        self._users = users

    @classmethod
    def load(cls, csv_path: str | Path) -> "UserPool":
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"User pool CSV not found: {path}")

        users: list[SimulatedUser] = []
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if len(row) >= 3 and row[0].strip():
                    users.append(
                        SimulatedUser(
                            user_id=row[0].strip(),
                            user_name=row[1].strip(),
                            user_email=row[2].strip(),
                        )
                    )
        return cls(users)

    def pick_random(self) -> SimulatedUser:
        return random.choice(self._users)
