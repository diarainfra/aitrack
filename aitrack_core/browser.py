"""Brauseri aktiivsed minutid: ajaloofailid + valikuline laiendus, ühendatud AI-minutitega.

Kõik arvutused tehakse lokaalselt. Väljapoole (serverisse) lähevad ainult projektiga seotud
minutiperioodid, mitte URL-id. Muu brauseritegevus jäetakse kõrvale juba siin.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

UTC = dt.timezone.utc
CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000   # 1601-01-01 → 1970-01-01
SAFARI_EPOCH_OFFSET_S = 978_307_200                   # 2001-01-01 → 1970-01-01

EXTENSION_ID = "pcjlnmemcbkhhollegnfjgcfgmiheooh"      # browser-extension/manifest.json "key" järgi
FIREFOX_EXTENSION_ID = "aitrack-browser@aitrack.local"
NATIVE_HOST_NAME = "com.aitrack.browser"


@dataclass
class BrowserVisit:
    browser: str
    ts: dt.datetime        # UTC
    url: str
    duration_s: float = 0  # brauseri enda teada kestus (Chromium), 0 = teadmata


@dataclass
class BrowserSpan:
    browser: str
    url: str
    start: dt.datetime
    end: dt.datetime
    source: str            # "history" | "extension"


# --- ajaloofailide asukohad ----------------------------------------------------
def _chromium_roots(home: Path, plat: str) -> list[tuple[str, Path]]:
    if plat == "windows":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return [
            ("Chrome", local / "Google" / "Chrome" / "User Data"),
            ("Edge", local / "Microsoft" / "Edge" / "User Data"),
            ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ("Vivaldi", local / "Vivaldi" / "User Data"),
            ("Chromium", local / "Chromium" / "User Data"),
            ("Opera", roaming / "Opera Software" / "Opera Stable"),
            ("Opera GX", roaming / "Opera Software" / "Opera GX Stable"),
        ]
    if plat == "macos":
        sup = home / "Library" / "Application Support"
        return [
            ("Chrome", sup / "Google" / "Chrome"),
            ("Edge", sup / "Microsoft Edge"),
            ("Brave", sup / "BraveSoftware" / "Brave-Browser"),
            ("Vivaldi", sup / "Vivaldi"),
            ("Chromium", sup / "Chromium"),
            ("Arc", sup / "Arc" / "User Data"),
            ("Opera", sup / "com.operasoftware.Opera"),
        ]
    cfg = home / ".config"
    return [
        ("Chrome", cfg / "google-chrome"),
        ("Edge", cfg / "microsoft-edge"),
        ("Brave", cfg / "BraveSoftware" / "Brave-Browser"),
        ("Vivaldi", cfg / "vivaldi"),
        ("Chromium", cfg / "chromium"),
        ("Chromium", home / "snap" / "chromium" / "common" / "chromium"),
        ("Opera", cfg / "opera"),
    ]


def _firefox_roots(home: Path, plat: str) -> list[Path]:
    if plat == "windows":
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return [roaming / "Mozilla" / "Firefox" / "Profiles"]
    if plat == "macos":
        return [home / "Library" / "Application Support" / "Firefox" / "Profiles"]
    return [home / ".mozilla" / "firefox", home / "snap" / "firefox" / "common" / ".mozilla" / "firefox"]


def history_sources(home: Path, plat: str) -> list[tuple[str, str, Path]]:
    """Leia kõik teadaolevad brauseri ajaloofailid → [(brauser, tüüp, tee)]."""
    out: list[tuple[str, str, Path]] = []
    for name, root in _chromium_roots(home, plat):
        if not root.is_dir():
            continue
        if (root / "History").is_file():                 # Opera: profiil = juurkaust
            out.append((name, "chromium", root / "History"))
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and (child.name == "Default" or child.name.startswith("Profile ")):
                if (child / "History").is_file():
                    out.append((name, "chromium", child / "History"))
    for root in _firefox_roots(home, plat):
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if (child / "places.sqlite").is_file():
                out.append(("Firefox", "firefox", child / "places.sqlite"))
    if plat == "macos":
        safari = home / "Library" / "Safari" / "History.db"
        if safari.is_file():
            out.append(("Safari", "safari", safari))
    return out


# --- ajaloofailide lugemine ------------------------------------------------------
def _copy_db(src: Path, dst_dir: Path) -> Path:
    """Brauser hoiab faili lukus → loe koopiat (koos WAL/journal failidega)."""
    dst = dst_dir / src.name
    shutil.copyfile(src, dst)
    for suffix in ("-wal", "-journal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.is_file():
            try:
                shutil.copyfile(side, dst_dir / side.name)
            except OSError:
                pass
    return dst


def _query_history(kind: str, db: Path, since: dt.datetime, until: dt.datetime) -> list[tuple[dt.datetime, str, float]]:
    s_us = int(since.timestamp() * 1_000_000)
    u_us = int(until.timestamp() * 1_000_000)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if kind == "chromium":
            rows = conn.execute(
                "SELECT v.visit_time, u.url, v.visit_duration FROM visits v JOIN urls u ON u.id = v.url "
                "WHERE v.visit_time >= ? AND v.visit_time < ? ORDER BY v.visit_time",
                (s_us + CHROME_EPOCH_OFFSET_US, u_us + CHROME_EPOCH_OFFSET_US),
            ).fetchall()
            return [(dt.datetime.fromtimestamp((t - CHROME_EPOCH_OFFSET_US) / 1e6, UTC), str(url), (d or 0) / 1e6)
                    for t, url, d in rows]
        if kind == "firefox":
            rows = conn.execute(
                "SELECT h.visit_date, p.url FROM moz_historyvisits h JOIN moz_places p ON p.id = h.place_id "
                "WHERE h.visit_date >= ? AND h.visit_date < ? ORDER BY h.visit_date",
                (s_us, u_us),
            ).fetchall()
            return [(dt.datetime.fromtimestamp(t / 1e6, UTC), str(url), 0.0) for t, url in rows]
        if kind == "safari":
            rows = conn.execute(
                "SELECT v.visit_time, i.url FROM history_visits v JOIN history_items i ON i.id = v.history_item "
                "WHERE v.visit_time >= ? AND v.visit_time < ? ORDER BY v.visit_time",
                (since.timestamp() - SAFARI_EPOCH_OFFSET_S, until.timestamp() - SAFARI_EPOCH_OFFSET_S),
            ).fetchall()
            return [(dt.datetime.fromtimestamp(t + SAFARI_EPOCH_OFFSET_S, UTC), str(url), 0.0) for t, url in rows]
    finally:
        conn.close()
    return []


def read_history_visits(sources: Iterable[tuple[str, str, Path]], since: dt.datetime, until: dt.datetime,
                        log: Callable[[str], None] | None = None) -> dict[str, list[BrowserVisit]]:
    """Loe külastused igast ajaloofailist eraldi → {allika-id: [külastus]} (aja järjekorras)."""
    out: dict[str, list[BrowserVisit]] = {}
    for browser, kind, path in sources:
        try:
            with tempfile.TemporaryDirectory(prefix="aitrack-browser-") as tmp:
                db = _copy_db(path, Path(tmp))
                rows = _query_history(kind, db, since, until)
        except (OSError, sqlite3.Error) as e:
            if log:
                log(f"browser: {browser} ajalugu ei saanud lugeda ({path.parent.name}): {e}")
            continue
        if rows:
            out[f"{browser}|{path}"] = [BrowserVisit(browser, ts, url, dur) for ts, url, dur in rows]
    return out


def visits_to_spans(visits_by_source: dict[str, list[BrowserVisit]], *, max_minutes: float = 5) -> list[BrowserSpan]:
    """Külastus = aktiivne kuni järgmise külastuseni samas brauseris, aga mitte kauem kui max_minutes.

    Järgmise külastuse arvestus käib KÕIGI külastuste peale (ka mitte-seotud lehtede), et
    projektivälisele lehele liikumine lõpetaks projekti aja kohe.
    """
    cap = dt.timedelta(minutes=max_minutes)
    spans: list[BrowserSpan] = []
    for visits in visits_by_source.values():
        ordered = sorted(visits, key=lambda v: v.ts)
        for i, v in enumerate(ordered):
            end = v.ts + cap
            if i + 1 < len(ordered):
                end = min(end, ordered[i + 1].ts)
            if v.duration_s and v.duration_s > 0:
                end = min(end, v.ts + dt.timedelta(seconds=v.duration_s))
            if end <= v.ts:
                end = v.ts + dt.timedelta(seconds=1)   # vähemalt külastuse minut ise
            spans.append(BrowserSpan(v.browser, v.url, v.ts, end, "history"))
    return spans


# --- laienduse logi (native messaging host kirjutab) --------------------------------
def append_extension_samples(path: Path, samples: list[dict]) -> int:
    """Native host: lisa laienduse minutiproovid lokaalsesse JSONL-faili (ainult skeem+host+tee)."""
    lines = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        ts = str(s.get("ts") or "")
        url = strip_url(str(s.get("url") or ""))
        if not ts or not url:
            continue
        lines.append(json.dumps({"ts": ts, "url": url, "browser": str(s.get("browser") or "")[:40]},
                                ensure_ascii=False))
    if not lines:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(lines)


def read_extension_spans(path: Path, since: dt.datetime, until: dt.datetime) -> list[BrowserSpan]:
    out: list[BrowserSpan] = []
    if not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
            ts = dt.datetime.fromisoformat(str(rec["ts"]).replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        ts = ts.astimezone(UTC).replace(second=0, microsecond=0)
        if not (since <= ts < until):
            continue
        out.append(BrowserSpan(str(rec.get("browser") or "laiendus"), str(rec.get("url") or ""),
                               ts, ts + dt.timedelta(minutes=1), "extension"))
    return out


def prune_extension_log(path: Path, keep_days: int, now: dt.datetime) -> None:
    """Hoia laienduse logi väike: vanemad read kustutatakse (atomaarne ümberkirjutus)."""
    if not path.is_file():
        return
    cutoff = now - dt.timedelta(days=keep_days)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    keep = []
    for line in lines:
        try:
            ts = dt.datetime.fromisoformat(str(json.loads(line)["ts"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
        if ts >= cutoff:
            keep.append(line)
    if len(keep) == len(lines):
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    os.replace(tmp, path)


# --- URL-ide sobitamine projektiga ------------------------------------------------------
def strip_url(url: str) -> str:
    """Jäta alles ainult skeem://host[:port]/tee (päring ja fragment võivad sisaldada saladusi)."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return urllib.parse.urlunsplit((p.scheme, p.netloc.split("@")[-1], p.path, "", ""))


def normalize_url(url: str) -> str:
    """'https://www.GitHub.com/a/b?x#y' → 'github.com/a/b'."""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        p = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    try:
        port = p.port
    except ValueError:
        port = None
    if port and not ((p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = p.path.rstrip("/").lower()
    return host + path


def pattern_score(url_norm: str, pattern: str) -> int:
    """>0 kui URL sobib mustriga (pikem muster = täpsem vaste), 0 kui ei sobi."""
    pat = normalize_url(pattern)
    if not pat or not url_norm:
        return 0
    if "/" not in pat:   # ainult host[:port] → sama host või alamdomeen
        host = url_norm.split("/", 1)[0]
        if host == pat or (":" not in pat and host.split(":")[0].endswith("." + pat)):
            return len(pat)
        return 0
    if url_norm == pat or url_norm.startswith(pat + "/"):
        return len(pat)
    return 0


def assign_project(url: str, patterns_by_project: dict[str, list[str]]) -> str | None:
    """Vali URL-ile kõige täpsema mustriga projekt; mitte-seotud URL → None (jäetakse välja)."""
    norm = normalize_url(url)
    best, best_score = None, 0
    for proj, patterns in patterns_by_project.items():
        for pat in patterns:
            score = pattern_score(norm, pat)
            if score > best_score:
                best, best_score = proj, score
    return best


# --- minutiaritmeetika -----------------------------------------------------------------
def _minute_no(d: dt.datetime) -> int:
    return int(d.timestamp() // 60)


def span_minutes(start: dt.datetime, end: dt.datetime) -> set[int]:
    """Kõik minutid, mida vahemik [start, end) puudutab (epohhi-minutite numbrid)."""
    if end <= start:
        return set()
    first = _minute_no(start)
    last = math.ceil(end.timestamp() / 60)
    return set(range(first, max(first + 1, last)))


def ai_minutes(timestamps: Iterable[dt.datetime], *, gap_minutes: float = 10,
               tail_minutes: float = 2) -> set[int]:
    """AI-tegevuse minutid: lähestikku (≤ gap) kirjed = pidev töö; iga kirje järel lühike saba."""
    ts = sorted(timestamps)
    minutes: set[int] = set()
    gap = dt.timedelta(minutes=gap_minutes)
    tail = dt.timedelta(minutes=tail_minutes)
    for i, t in enumerate(ts):
        minutes |= span_minutes(t, t + tail)
        if i + 1 < len(ts) and ts[i + 1] - t <= gap:
            minutes |= span_minutes(t, ts[i + 1])
    return minutes


def minutes_to_periods(minutes: Iterable[int]) -> list[tuple[dt.datetime, dt.datetime]]:
    """Minutihulk → järjestikused perioodid [(algus, lõpp)] UTC-s (kattuvused on juba ühendatud)."""
    out: list[tuple[dt.datetime, dt.datetime]] = []
    for m in sorted(set(minutes)):
        start = dt.datetime.fromtimestamp(m * 60, UTC)
        if out and out[-1][1] == start:
            out[-1] = (out[-1][0], start + dt.timedelta(minutes=1))
        else:
            out.append((start, start + dt.timedelta(minutes=1)))
    return out


def split_by_hour(minutes: Iterable[int]) -> dict[dt.datetime, set[int]]:
    out: dict[dt.datetime, set[int]] = {}
    for m in minutes:
        hstart = dt.datetime.fromtimestamp((m // 60) * 3600, UTC)
        out.setdefault(hstart, set()).add(m)
    return out


# --- native messaging protokoll -------------------------------------------------------
def native_host_loop(stdin, stdout, handle: Callable[[dict], dict]) -> None:
    """Chrome/Firefox native messaging: 4-baidine pikkus (native byte order) + UTF-8 JSON."""
    while True:
        header = stdin.read(4)
        if len(header) < 4:
            return
        size = int.from_bytes(header, sys.byteorder)
        if size <= 0 or size > 1024 * 1024:
            return
        body = stdin.read(size)
        try:
            msg = json.loads(body.decode("utf-8"))
            reply = handle(msg if isinstance(msg, dict) else {})
        except (ValueError, UnicodeDecodeError):
            reply = {"ok": False, "error": "vigane sõnum"}
        data = json.dumps(reply).encode("utf-8")
        stdout.write(len(data).to_bytes(4, sys.byteorder) + data)
        stdout.flush()
