"""Count configured curse words in timestamped AI session logs."""

import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "curse_words.json"
CACHE = ROOT / "data" / "cache"
OUTPUT = ROOT / "data" / "output" / "daily_counts.json"
CHART = ROOT / "data" / "output" / "daily_counts.svg"
TOKEN_PATTERN = re.compile(r"\w+")
TIMESTAMP_PATTERNS = {
    "claude": re.compile(r'"timestamp"\s*:\s*"([^"\\]+)"'),
    "gemini": re.compile(r'"created_at"\s*:\s*"([^"\\]+)"'),
    "grok": re.compile(r'"timestamp"\s*:\s*"([^"\\]+)"'),
}


class Settings:
    """Fixed scanner settings."""

    CACHE_FORMAT_VERSION = 4
    SERIES_COLORS = {
        "claude": "#5144d8",
        "codex": "#d04f89",
        "gemini": "#d88317",
        "grok": "#16835b",
    }
    USER_CAPTURE_PATTERNS = {
        "claude": re.compile(r'"type"\s*:\s*"user".*?"message"\s*:\s*\{\s*"role"\s*:\s*"user"\s*,\s*"content"\s*:\s*"'),
        "gemini": re.compile(r'"source"\s*:\s*"USER_EXPLICIT"'),
        "grok": re.compile(r'"type"\s*:\s*"user"'),
    }


@dataclass(frozen=True)
class SessionLog:
    """A timestamped transcript store from a supported AI client."""

    source: str
    path: Path
    format: str


@dataclass(frozen=True)
class CurseMatcher:
    """A fast matcher for plain words plus punctuation and phrase entries."""

    words: frozenset[str]
    complex_words: tuple[str, ...]

    def count(self, text: str) -> int:
        """Count non-overlapping configured terms, preferring the longest match."""
        lowered_text = text.lower()
        complex_candidates: list[tuple[int, int]] = []
        for word in self.complex_words:
            start = lowered_text.find(word)
            while start != -1:
                end = start + len(word)
                preceding_is_word = start > 0 and is_word_character(lowered_text[start - 1])
                following_is_word = end < len(lowered_text) and is_word_character(lowered_text[end])
                if not preceding_is_word and not following_is_word:
                    complex_candidates.append((start, end))
                start = lowered_text.find(word, start + 1)

        selected_complex_matches: list[tuple[int, int]] = []
        for candidate in sorted(complex_candidates, key=lambda match: (-(match[1] - match[0]), match[0])):
            if not any(matches_overlap(candidate, selected) for selected in selected_complex_matches):
                selected_complex_matches.append(candidate)

        plain_count = 0
        for token in TOKEN_PATTERN.finditer(text):
            if token.group().lower() in self.words and not any(
                matches_overlap((token.start(), token.end()), selected) for selected in selected_complex_matches
            ):
                plain_count += 1
        return plain_count + len(selected_complex_matches)


worker_matcher: CurseMatcher | None = None


def format_duration(seconds: float) -> str:
    """Format a duration as an hours, minutes, and seconds estimate."""
    total_seconds = round(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def count_curses(log: Path, words: list[str]) -> int:
    """Count whole-word occurrences in a text file, ignoring case."""
    matcher = build_matcher(words)
    with log.open(encoding="utf-8") as lines:
        return sum(matcher.count(line) for line in lines)


def is_plain_word(word: str) -> bool:
    """Return whether a dictionary entry can be matched by token lookup."""
    return all(character.isalnum() or character == "_" for character in word)


def is_word_character(character: str) -> bool:
    """Return whether a character participates in a whole-word boundary."""
    return character.isalnum() or character == "_"


def matches_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """Return whether two text spans share at least one character."""
    return left[0] < right[1] and right[0] < left[1]


def build_matcher(words: list[str]) -> CurseMatcher:
    """Build a matcher that avoids a huge regex for ordinary word entries."""
    plain_words = frozenset(word.lower() for word in words if is_plain_word(word))
    complex_words = tuple(word.lower() for word in words if not is_plain_word(word))
    return CurseMatcher(plain_words, complex_words)


def discover_logs(home: Path) -> list[SessionLog]:
    """Discover exactly one primary transcript per session for every supported client."""
    logs = [
        *(SessionLog("claude", path, "jsonl") for path in (home / ".claude" / "projects").rglob("*.jsonl")),
        *(SessionLog("grok", path, "jsonl") for path in (home / ".grok" / "sessions").rglob("chat_history.jsonl")),
        *(SessionLog("gemini", path, "jsonl") for path in (home / ".gemini").glob("**/.system_generated/logs/transcript.jsonl")),
        *(SessionLog("codex", path, "sqlite") for path in (home / ".codex").glob("thread_history_*.sqlite")),
    ]
    return sorted(logs, key=lambda log: (log.source, log.path))


def date_from_timestamp(timestamp: object) -> str:
    """Return the UTC calendar date for a required ISO-8601 timestamp."""
    if not isinstance(timestamp, str) or not timestamp:
        raise ValueError("Every JSONL capture must include a non-empty timestamp")
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC).date().isoformat()


def require_daily_counts(value: object, entry: Path) -> dict[str, int]:
    """Validate the completed per-log bucket payload stored in a cache entry."""
    if not isinstance(value, dict):
        raise ValueError(f"Invalid cache entry: {entry}")
    object_value = cast(dict[object, object], value)
    if any(not isinstance(date, str) or not isinstance(count, int) for date, count in object_value.items()):
        raise ValueError(f"Invalid cache entry: {entry}")
    return cast(dict[str, int], value)


def require_words(value: object) -> list[str]:
    """Validate the configured curse dictionary before scanning any logs."""
    if not isinstance(value, list) or not value:
        raise ValueError("Curse word dictionary must be a non-empty list of non-empty strings")
    object_value = cast(list[object], value)
    if any(not isinstance(word, str) or not word.strip() for word in object_value):
        raise ValueError("Curse word dictionary must be a non-empty list of non-empty strings")
    return cast(list[str], value)


def jsonl_counts(log: SessionLog, matcher: CurseMatcher) -> Iterator[tuple[str, int]]:
    """Yield every timestamped user capture and its raw-line curse count."""
    timestamp_pattern = TIMESTAMP_PATTERNS[log.source]
    user_capture_pattern = Settings.USER_CAPTURE_PATTERNS[log.source]
    with log.path.open(encoding="utf-8") as lines:
        for line in lines:
            if user_capture_pattern.search(line) is None:
                continue
            timestamp = timestamp_pattern.search(line)
            if timestamp is not None:
                yield date_from_timestamp(timestamp.group(1)), matcher.count(line)


def codex_records(log: SessionLog) -> Iterator[tuple[str, str]]:
    """Yield timestamped Codex capture payloads from its SQLite transcript store."""
    with sqlite3.connect(f"file:{log.path}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT created_at_ms, item_json FROM thread_realtime_items")
        for created_at_ms, item_json in rows:
            if not isinstance(created_at_ms, int) or not isinstance(item_json, str):
                raise ValueError(f"Invalid Codex capture in {log.path}")
            date = datetime.fromtimestamp(created_at_ms / 1000, tz=UTC).date().isoformat()
            yield date, item_json


def count_daily_curses(log: SessionLog, matcher: CurseMatcher) -> dict[str, int]:
    """Count curses by capture day for one complete session log."""
    counts: Counter[str] = Counter()
    if log.format == "sqlite":
        for date, text in codex_records(log):
            counts[date] += matcher.count(text)
    else:
        for date, matches in jsonl_counts(log, matcher):
            counts[date] += matches
    return dict(sorted(counts.items()))


def cache_entry(log: SessionLog, cache: Path) -> Path:
    """Return a per-log cache file named for the analyzed log without collisions."""
    parent_key = hashlib.sha256(str(log.path.parent.resolve(strict=True)).encode("utf-8")).hexdigest()
    return cache / log.source / parent_key / f"{log.path.name}.json"


def cached_count_with_matcher(log: SessionLog, matcher: CurseMatcher, cache: Path) -> dict[str, int]:
    """Reuse completed buckets unless the analyzed source file changed size."""
    entry = cache_entry(log, cache)
    source_size = log.path.stat().st_size
    if entry.exists():
        with entry.open(encoding="utf-8") as cached:
            cached_entry: object = json.load(cached)
        if isinstance(cached_entry, dict):
            cached_object = cast(dict[str, object], cached_entry)
            cached_version: object = cached_object.get("format_version")
            cached_size: object = cached_object.get("source_size")
            cached_counts: object = cached_object.get("daily_counts")
            if cached_version == Settings.CACHE_FORMAT_VERSION and cached_size == source_size and isinstance(cached_size, int):
                return require_daily_counts(cached_counts, entry)

    daily_counts = count_daily_curses(log, matcher)
    entry.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=entry.parent, delete=False) as temporary:
        json.dump(
            {"format_version": Settings.CACHE_FORMAT_VERSION, "source_size": source_size, "daily_counts": daily_counts},
            temporary,
            sort_keys=True,
        )
        temporary_path = Path(temporary.name)
    temporary_path.replace(entry)
    return daily_counts


def cached_count(log: SessionLog, words: list[str], cache: Path) -> dict[str, int]:
    """Process one log directly with a matcher built for its configured words."""
    return cached_count_with_matcher(log, build_matcher(words), cache)


def initialize_worker(words: list[str]) -> None:
    """Build the shared matcher once for each parallel log-processing worker."""
    global worker_matcher
    worker_matcher = build_matcher(words)


def cached_worker_count(log: SessionLog, cache: Path) -> dict[str, int]:
    """Process one log with the worker's precompiled matcher."""
    if worker_matcher is None:
        raise RuntimeError("Log-processing worker was not initialized")
    return cached_count_with_matcher(log, worker_matcher, cache)


def worker_count() -> int:
    """Return the available CPU count, failing when the host cannot report it."""
    count = os.cpu_count()
    if count is None:
        raise RuntimeError("Unable to determine the CPU count for parallel log processing")
    return count


def build_daily_chart(source_daily_counts: dict[str, dict[str, int]]) -> str:
    """Build a standalone SVG line chart with one series per AI source."""
    if not source_daily_counts:
        raise ValueError("Cannot chart an empty daily count collection")

    all_dates = [date.fromisoformat(day) for counts in source_daily_counts.values() for day in counts]
    if not all_dates:
        raise ValueError("Cannot chart an empty daily count collection")
    first_day = min(all_dates)
    last_day = max(all_dates)
    calendar_day_count = (last_day - first_day).days + 1
    values = [count for counts in source_daily_counts.values() for count in counts.values()]
    maximum = max(values)
    chart_maximum = maximum if maximum > 0 else 1

    width = 1400
    height = 720
    left = 100
    right = 40
    top = 140
    bottom = 100
    plot_width = width - left - right
    plot_height = height - top - bottom

    def x_position(current_day: date) -> float:
        if calendar_day_count == 1:
            return left + plot_width / 2
        return left + (current_day - first_day).days / (calendar_day_count - 1) * plot_width

    def y_position(value: int) -> float:
        return top + plot_height - value / chart_maximum * plot_height

    gridlines: list[str] = []
    for tick in range(5):
        value = chart_maximum * tick / 4
        y = y_position(round(value))
        gridlines.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" class="grid"/>')
        gridlines.append(f'<text x="{left - 16}" y="{y + 6:.1f}" text-anchor="end" class="axis-label">{value:.0f}</text>')

    x_labels: list[str] = []
    for tick in range(6):
        index = round((calendar_day_count - 1) * tick / 5)
        current_day = first_day + timedelta(days=index)
        x = x_position(current_day)
        x_labels.append(f'<line x1="{x:.1f}" y1="{top + plot_height}" x2="{x:.1f}" y2="{top + plot_height + 8}" class="axis"/>')
        x_labels.append(
            f'<text x="{x:.1f}" y="{top + plot_height + 38}" text-anchor="middle" class="axis-label">{current_day:%b %-d}</text>'
        )

    series_lines: list[str] = []
    legend: list[str] = []
    for index, source in enumerate(sorted(source_daily_counts)):
        color = Settings.SERIES_COLORS[source]
        interaction_days = [(date.fromisoformat(day), count) for day, count in sorted(source_daily_counts[source].items())]
        points = " ".join(f"{x_position(current_day):.1f},{y_position(value):.1f}" for current_day, value in interaction_days)
        circles = "".join(
            f'<circle cx="{x_position(current_day):.1f}" cy="{y_position(value):.1f}" r="3.5" fill="{color}" class="point"/>'
            for current_day, value in interaction_days
        )
        series_lines.append(f'<polyline points="{points}" stroke="{color}" class="line"/>{circles}')
        legend_x = left + index * 170
        legend_y = 105
        legend.append(
            f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 26}" y2="{legend_y}" stroke="{color}" class="legend-line"/>'
            f'<text x="{legend_x + 36}" y="{legend_y + 6}" class="legend-label">{html.escape(source.title())}</text>'
        )
    title = html.escape(f"Curse words per day · {first_day:%b %-d, %Y} – {last_day:%b %-d, %Y}")
    interaction_day_count = len({day for counts in source_daily_counts.values() for day in counts})
    subtitle = html.escape(f"{interaction_day_count} interaction days · {sum(values):,} total matches")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
  viewBox="0 0 {width} {height}" role="img" aria-labelledby="title description">
  <title id="title">{title}</title>
  <desc id="description">Daily count of configured curse words across all processed AI session logs.</desc>
  <style>
    .background {{ fill: #fcfcfd; }} .plot {{ fill: #f7f5fb; }} .grid {{ stroke: #dedbe7; stroke-width: 1; }}
    .axis {{ stroke: #6b6875; stroke-width: 1.5; }} .axis-label {{ fill: #55515f; font: 22px system-ui, sans-serif; }}
    .title {{ fill: #1f1c26; font: 700 34px system-ui, sans-serif; }} .subtitle {{ fill: #6b6875; font: 20px system-ui, sans-serif; }}
    .line {{ fill: none; stroke-width: 4; stroke-linejoin: round; stroke-linecap: round; }}
    .point {{ stroke: #fcfcfd; stroke-width: 2; }} .legend-line {{ stroke-width: 4; stroke-linecap: round; }}
    .legend-label {{ fill: #55515f; font: 18px system-ui, sans-serif; }}
  </style>
  <rect width="100%" height="100%" class="background"/>
  <text x="{left}" y="48" class="title">Curse words per day</text>
  <text x="{left}" y="76" class="subtitle">{subtitle}</text>
  {"".join(legend)}
  <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" class="plot"/>
  {"".join(gridlines)}
  <line x1="{left}" y1="{top + plot_height}" x2="{width - right}" y2="{top + plot_height}" class="axis"/>
  {"".join(x_labels)}
  {"".join(series_lines)}
</svg>
"""


def write_daily_chart(source_daily_counts: dict[str, dict[str, int]], chart: Path) -> None:
    """Write the daily-count SVG after all source logs have been aggregated."""
    chart.parent.mkdir(parents=True, exist_ok=True)
    chart.write_text(build_daily_chart(source_daily_counts), encoding="utf-8")


def main() -> None:
    """Process all supported logs and write their aggregate timestamped daily totals."""
    with CONFIG.open(encoding="utf-8") as config:
        configured_words: object = json.load(config)
    words = require_words(configured_words)
    logs = discover_logs(Path.home())
    if not logs:
        raise FileNotFoundError("No supported AI session logs found")

    total_logs = len(logs)
    started_at = time.monotonic()
    daily_counts: Counter[str] = Counter()
    source_log_counts: Counter[str] = Counter()
    source_curse_counts: Counter[str] = Counter()
    source_daily_counts: dict[str, Counter[str]] = {}
    processed = 0
    with ProcessPoolExecutor(max_workers=worker_count(), initializer=initialize_worker, initargs=(words,)) as executor:
        for source in sorted({log.source for log in logs}):
            source_logs = [log for log in logs if log.source == source]
            futures = {executor.submit(cached_worker_count, log, CACHE): log for log in source_logs}
            for future in as_completed(futures):
                log = futures[future]
                eta = (
                    "calculating..."
                    if processed == 0
                    else format_duration((time.monotonic() - started_at) / processed * (total_logs - processed))
                )
                percentage = processed / total_logs * 100
                sys.stderr.write(
                    f"\r\033[2KProcessing {percentage:.1f}% ({processed}/{total_logs}) ETA: {eta} | [{log.source}] {log.path.name}"
                )
                sys.stderr.flush()
                log_daily_counts = future.result()
                daily_counts.update(log_daily_counts)
                source_log_counts[log.source] += 1
                source_curse_counts[log.source] += sum(log_daily_counts.values())
                source_daily_counts.setdefault(log.source, Counter()).update(log_daily_counts)
                processed += 1
            sys.stderr.write("\n")
            sys.stderr.flush()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as output:
        json.dump(dict(sorted(daily_counts.items())), output, indent=2)
        output.write("\n")
    write_daily_chart({source: dict(counts) for source, counts in source_daily_counts.items()}, CHART)
    total_count = sum(daily_counts.values())
    print(f"Total curse words: {total_count}")
    for source in sorted(source_log_counts):
        print(f"[{source}] {source_log_counts[source]} logs, {source_curse_counts[source]} curse words")
    print(f"Daily counts: {OUTPUT}")
    print(f"Daily chart: {CHART}")
    if total_count == 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
