from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

# Типичная школьная сетка (можно сбросить в дефолт)
DEFAULT_BELLS: dict[int, tuple[str, str]] = {
    1: ("08:00", "08:45"),
    2: ("08:55", "09:40"),
    3: ("09:50", "10:35"),
    4: ("10:55", "11:40"),
    5: ("12:00", "12:45"),
    6: ("12:55", "13:40"),
    7: ("13:50", "14:35"),
    8: ("14:45", "15:30"),
    9: ("15:40", "16:25"),
}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_RANGE_RE = re.compile(
    r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*[-–—]\s*([01]?\d|2[0-3]):([0-5]\d)\s*$"
)


@dataclass
class UserSettings:
    user_id: int
    class_name: str
    subgroup: int | None


@dataclass
class BellPeriod:
    lesson_number: int
    start: str  # HH:MM
    end: str  # HH:MM


@dataclass
class CalendarPrefs:
    user_id: int
    title_template: str = "{lesson}"
    custom_naming: bool = False


@dataclass
class CalDavCredentials:
    user_id: int
    url: str
    username: str
    password: str | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.url and self.username and self.password)


DEFAULT_TITLE_TEMPLATE = "{lesson}"

def normalize_time(value: str) -> str | None:
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def parse_bell_range(text: str) -> tuple[str, str] | None:
    """Парсит '08:00-08:45' или '8:00 – 8:45'."""
    match = _RANGE_RE.match(text)
    if not match:
        return None
    start = f"{int(match.group(1)):02d}:{match.group(2)}"
    end = f"{int(match.group(3)):02d}:{match.group(4)}"
    if start >= end:
        return None
    return start, end


def default_bells() -> dict[int, BellPeriod]:
    return {
        n: BellPeriod(lesson_number=n, start=s, end=e)
        for n, (s, e) in DEFAULT_BELLS.items()
    }


class Database:
    def __init__(self, path: str):
        self._path = path

    async def init(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    class_name TEXT NOT NULL,
                    subgroup INTEGER,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bell_schedules (
                    user_id INTEGER PRIMARY KEY,
                    periods_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_prefs (
                    user_id INTEGER PRIMARY KEY,
                    title_template TEXT NOT NULL DEFAULT '{lesson}',
                    custom_naming INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS lesson_aliases (
                    user_id INTEGER NOT NULL,
                    source_name TEXT NOT NULL,
                    alias_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, source_name)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS caldav_credentials (
                    user_id INTEGER PRIMARY KEY,
                    url TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def get_user_settings(self, user_id: int) -> UserSettings | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT user_id, class_name, subgroup FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return UserSettings(
                user_id=row["user_id"],
                class_name=row["class_name"],
                subgroup=row["subgroup"],
            )

    async def save_user_settings(
        self, user_id: int, class_name: str, subgroup: int | None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO user_settings (user_id, class_name, subgroup, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    class_name = excluded.class_name,
                    subgroup = excluded.subgroup,
                    updated_at = excluded.updated_at
                """,
                (user_id, class_name, subgroup, now),
            )
            await db.commit()

    async def clear_user_settings(self, user_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
            await db.commit()

    async def get_bells(self, user_id: int) -> dict[int, BellPeriod]:
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT periods_json FROM bell_schedules WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return default_bells()

            raw = json.loads(row[0])
            bells = default_bells()
            for key, value in raw.items():
                n = int(key)
                if 1 <= n <= 9:
                    bells[n] = BellPeriod(
                        lesson_number=n,
                        start=value["start"],
                        end=value["end"],
                    )
            return bells

    async def save_bells(self, user_id: int, bells: dict[int, BellPeriod]) -> None:
        payload = {
            str(n): {"start": p.start, "end": p.end}
            for n, p in sorted(bells.items())
            if 1 <= n <= 9
        }
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO bell_schedules (user_id, periods_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    periods_json = excluded.periods_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(payload, ensure_ascii=False), now),
            )
            await db.commit()

    async def reset_bells(self, user_id: int) -> dict[int, BellPeriod]:
        bells = default_bells()
        await self.save_bells(user_id, bells)
        return bells

    async def set_bell_period(
        self, user_id: int, lesson_number: int, start: str, end: str
    ) -> dict[int, BellPeriod]:
        bells = await self.get_bells(user_id)
        bells[lesson_number] = BellPeriod(
            lesson_number=lesson_number, start=start, end=end
        )
        await self.save_bells(user_id, bells)
        return bells

    async def get_calendar_prefs(self, user_id: int) -> CalendarPrefs:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT title_template, custom_naming FROM calendar_prefs WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return CalendarPrefs(user_id=user_id)
            return CalendarPrefs(
                user_id=user_id,
                title_template=row["title_template"] or DEFAULT_TITLE_TEMPLATE,
                custom_naming=bool(row["custom_naming"]),
            )

    async def save_calendar_prefs(
        self,
        user_id: int,
        *,
        title_template: str | None = None,
        custom_naming: bool | None = None,
    ) -> CalendarPrefs:
        current = await self.get_calendar_prefs(user_id)
        template = title_template if title_template is not None else current.title_template
        naming = current.custom_naming if custom_naming is None else custom_naming
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO calendar_prefs (user_id, title_template, custom_naming, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    title_template = excluded.title_template,
                    custom_naming = excluded.custom_naming,
                    updated_at = excluded.updated_at
                """,
                (user_id, template, int(naming), now),
            )
            await db.commit()
        return CalendarPrefs(user_id=user_id, title_template=template, custom_naming=naming)

    async def get_lesson_aliases(self, user_id: int) -> dict[str, str]:
        """source_name (как в расписании) → alias для {lesson}."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "SELECT source_name, alias_name FROM lesson_aliases WHERE user_id = ?",
                (user_id,),
            )
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}

    async def set_lesson_alias(self, user_id: int, source_name: str, alias_name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO lesson_aliases (user_id, source_name, alias_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, source_name) DO UPDATE SET
                    alias_name = excluded.alias_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, source_name.strip(), alias_name.strip(), now),
            )
            await db.commit()

    async def delete_lesson_alias(self, user_id: int, source_name: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM lesson_aliases WHERE user_id = ? AND source_name = ?",
                (user_id, source_name),
            )
            await db.commit()

    async def clear_lesson_aliases(self, user_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM lesson_aliases WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()

    async def get_caldav_credentials(self, user_id: int) -> CalDavCredentials | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT url, username, password FROM caldav_credentials WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return CalDavCredentials(
                user_id=user_id,
                url=row["url"],
                username=row["username"],
                password=row["password"],
            )

    async def save_caldav_credentials(
        self,
        user_id: int,
        *,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        clear_password: bool = False,
    ) -> CalDavCredentials:
        current = await self.get_caldav_credentials(user_id)
        new_url = url if url is not None else (current.url if current else "")
        new_user = username if username is not None else (current.username if current else "")
        if clear_password:
            new_pass = None
        elif password is not None:
            new_pass = password
        else:
            new_pass = current.password if current else None

        if not new_url or not new_user:
            raise ValueError("URL и логин обязательны")

        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO caldav_credentials (user_id, url, username, password, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    url = excluded.url,
                    username = excluded.username,
                    password = excluded.password,
                    updated_at = excluded.updated_at
                """,
                (user_id, new_url.strip(), new_user.strip(), new_pass, now),
            )
            await db.commit()
        return CalDavCredentials(
            user_id=user_id, url=new_url.strip(), username=new_user.strip(), password=new_pass
        )

    async def clear_caldav_password(self, user_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE caldav_credentials SET password = NULL, updated_at = ? WHERE user_id = ?",
                (datetime.now(timezone.utc).isoformat(), user_id),
            )
            await db.commit()

    async def delete_caldav_credentials(self, user_id: int) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM caldav_credentials WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()

