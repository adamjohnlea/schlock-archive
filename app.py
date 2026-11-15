#!/usr/bin/env python3
"""
Schlock Archive - a local, SQLite-backed b-movie director/film archive driven by TMDB.

Run:  python3 app.py            (opens http://localhost:8787 in your browser)
      python3 app.py --help     (options)

Python 3.8+ standard library only. No pip installs.
"""
import argparse
import csv
import html
import io
import json
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_CSV = os.path.join(APP_DIR, "data", "directors.csv")
DEFAULT_DB = os.path.join(APP_DIR, "schlock.db")
DEFAULT_PORT = 8787
TMDB_API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"
STALE_DAYS_DEFAULT = 30
USER_AGENT = "SchlockArchive/1.0 (personal local app)"

CATEGORY_LABELS = {
    "01_core_video_store_schlock": ("The closest fit: video-store and RLM-flavor schlock", ""),
    "02_shot_on_video_horror": ("Homemade horror and shot-on-video territory",
                                "Some worked on film as well; this groups the microbudget scene rather than strictly the shooting format."),
    "03_classic_us_exploitation_drive_in": ("Drive-in, exploitation, gore, and cult staples", ""),
    "04_italian_cult_giallo_eurotrash": ("Italian cult, giallo, cannibals, and Eurotrash",
                                         "A few non-Italian filmmakers are included for their work in that scene."),
    "05_spanish_french_european_cult": ("Spanish, French, and other European cult", ""),
    "06_hong_kong_philippines_asian_trash": ("Hong Kong, Filipino, and Japanese action and cult",
                                             "Mixes exploitation specialists with accomplished martial-arts and monster-movie directors."),
    "07_creature_franchise_horror_adjacent": ("Creature features, franchise horror, and Full Moon neighbors", ""),
    "08_accomplished_cult_gateway_higher_craft": ("Bigger cult names worth mining for their early work",
                                                  "You probably know these already, but their early filmographies belong in the search."),
}

CONFIG = {"db": DEFAULT_DB, "fake": False, "static": False}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_days(iso):
    if not iso:
        return 1e9
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    except Exception:
        return 1e9


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS seed_directors (
    tmdb_id INTEGER PRIMARY KEY, name TEXT, category TEXT, example_films TEXT,
    notes TEXT, source_url TEXT, tmdb_url TEXT, sort_order INTEGER);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY, name TEXT, profile_path TEXT, known_for_department TEXT,
    biography TEXT, birthday TEXT, deathday TEXT, place_of_birth TEXT, imdb_id TEXT,
    popularity REAL, also_known_as TEXT, raw_json TEXT, fetched_at TEXT);

CREATE TABLE IF NOT EXISTS movies (
    media_type TEXT NOT NULL DEFAULT 'movie', id INTEGER NOT NULL,
    title TEXT, original_title TEXT, release_date TEXT, runtime INTEGER, overview TEXT,
    tagline TEXT, poster_path TEXT, backdrop_path TEXT, vote_average REAL, vote_count INTEGER,
    popularity REAL, imdb_id TEXT, original_language TEXT, status TEXT, budget INTEGER,
    revenue INTEGER, certification TEXT, trailer_key TEXT, raw_json TEXT, fetched_at TEXT,
    PRIMARY KEY (media_type, id));

CREATE TABLE IF NOT EXISTS credits (
    credit_id TEXT PRIMARY KEY, person_id INTEGER NOT NULL, media_type TEXT NOT NULL,
    title_id INTEGER NOT NULL, credit_type TEXT NOT NULL, character TEXT, job TEXT,
    department TEXT, ord INTEGER, episode_count INTEGER);
CREATE INDEX IF NOT EXISTS idx_credits_person ON credits(person_id);
CREATE INDEX IF NOT EXISTS idx_credits_title ON credits(media_type, title_id);

CREATE TABLE IF NOT EXISTS genres (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS movie_genres (media_type TEXT, movie_id INTEGER, genre_id INTEGER,
    PRIMARY KEY (media_type, movie_id, genre_id));
CREATE TABLE IF NOT EXISTS keywords (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS movie_keywords (media_type TEXT, movie_id INTEGER, keyword_id INTEGER,
    PRIMARY KEY (media_type, movie_id, keyword_id));
CREATE TABLE IF NOT EXISTS related (media_type TEXT, movie_id INTEGER, rel_type TEXT,
    other_media_type TEXT, other_id INTEGER, rank INTEGER,
    PRIMARY KEY (media_type, movie_id, rel_type, other_media_type, other_id));

CREATE TABLE IF NOT EXISTS user_data (
    media_type TEXT NOT NULL, movie_id INTEGER NOT NULL, status TEXT, rating INTEGER,
    notes TEXT, updated_at TEXT, PRIMARY KEY (media_type, movie_id));

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER, scope TEXT, total INTEGER,
    done INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, status TEXT, last_error TEXT,
    started_at TEXT, finished_at TEXT);
"""


def db():
    conn = sqlite3.connect(CONFIG["db"], timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # Any job left "running" by a previous process is dead now.
    conn.execute("UPDATE import_jobs SET status='interrupted' WHERE status='running'")
    conn.commit()
    seed_from_csv(conn)
    conn.close()


def seed_from_csv(conn, path=None):
    path = path or SEED_CSV
    if not os.path.exists(path):
        print("Note: seed CSV not found at %s" % path)
        return 0
    n = 0
    with open(path, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            try:
                pid = int((row.get("tmdb_id") or "").strip())
            except ValueError:
                continue
            conn.execute(
                "INSERT INTO seed_directors (tmdb_id,name,category,example_films,notes,source_url,tmdb_url,sort_order)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tmdb_id) DO UPDATE SET name=excluded.name,"
                " category=excluded.category, example_films=excluded.example_films, notes=excluded.notes,"
                " source_url=excluded.source_url, tmdb_url=excluded.tmdb_url, sort_order=excluded.sort_order",
                (pid, row.get("name"), row.get("category"), row.get("example_films"), row.get("notes"),
                 row.get("source_url"), row.get("tmdb_url"), i))
            conn.execute("INSERT OR IGNORE INTO people (id, name) VALUES (?,?)", (pid, row.get("name")))
            n += 1
    conn.commit()
    return n


def get_setting(conn, key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def set_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))
    conn.commit()


def stale_days(conn):
    try:
        return float(get_setting(conn, "stale_days", STALE_DAYS_DEFAULT))
    except ValueError:
        return STALE_DAYS_DEFAULT


def has_key(conn):
    return bool(get_setting(conn, "tmdb_credential"))

# ----------------------------------------------------------------------------
# TMDB client
# ----------------------------------------------------------------------------

class TMDBError(Exception):
    def __init__(self, kind, message, status=None):
        Exception.__init__(self, message)
        self.kind = kind  # auth | notfound | ratelimit | network | ssl | nokey | other
        self.status = status
        self.message = message


def detect_auth_type(cred):
    cred = (cred or "").strip()
    if cred.startswith("eyJ") and cred.count(".") == 2 and len(cred) > 60:
        return "bearer"
    return "api_key"


_throttle_lock = threading.Lock()
_last_request = [0.0]
MIN_INTERVAL = 0.08   # at most ~12 requests/second, well under TMDB's limits
_ssl_ctx = [None]


def _mac_system_ssl_context():
    """python.org Python on macOS can ship without root certificates. Build a context
    from the macOS system keychain using Apple's built-in `security` tool."""
    bundle = os.path.join(APP_DIR, ".macos-root-certs.pem")
    if not os.path.exists(bundle):
        pem = subprocess.run(
            ["/usr/bin/security", "find-certificate", "-a", "-p",
             "/System/Library/Keychains/SystemRootCertificates.keychain"],
            capture_output=True, timeout=20).stdout
        if not pem:
            raise RuntimeError("no certs from keychain")
        with open(bundle, "wb") as f:
            f.write(pem)
    return ssl.create_default_context(cafile=bundle)


def _urlopen(req, timeout):
    if _ssl_ctx[0] is not None:
        return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx[0])
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as err:
        if isinstance(getattr(err, "reason", None), ssl.SSLCertVerificationError):
            if sys.platform == "darwin":
                try:
                    _ssl_ctx[0] = _mac_system_ssl_context()
                except Exception:
                    _ssl_ctx[0] = None
                if _ssl_ctx[0] is not None:
                    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx[0])
            raise TMDBError("ssl", "Your Python can't verify HTTPS certificates. In Finder open Applications > "
                                   "Python 3.x and double-click 'Install Certificates.command', then try again.")
        raise


def real_tmdb_get(path, params=None, credential=None):
    if credential is None:
        conn = db()
        credential = get_setting(conn, "tmdb_credential")
        conn.close()
    if not credential:
        raise TMDBError("nokey", "No TMDB API key configured yet.")
    params = dict(params or {})
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if detect_auth_type(credential) == "bearer":
        headers["Authorization"] = "Bearer " + credential
    else:
        params["api_key"] = credential
    url = TMDB_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    attempts = 0
    while True:
        attempts += 1
        with _throttle_lock:
            wait = MIN_INTERVAL - (time.time() - _last_request[0])
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.time()
        try:
            with _urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            # Never put the URL in messages or logs: it can contain the api_key.
            if err.code == 429 and attempts < 4:
                try:
                    ra = float(err.headers.get("Retry-After", "2"))
                except (TypeError, ValueError):
                    ra = 2.0
                time.sleep(min(max(ra, 1.0), 10.0))
                continue
            if err.code == 401:
                raise TMDBError("auth", "TMDB rejected the API key (401). Check it in Settings.", 401)
            if err.code == 404:
                raise TMDBError("notfound", "TMDB has no record at that ID (404).", 404)
            if err.code == 429:
                raise TMDBError("ratelimit", "TMDB is rate-limiting us (429). Wait a few seconds and try again.", 429)
            if err.code >= 500 and attempts < 3:
                time.sleep(1.5)
                continue
            raise TMDBError("other", "TMDB returned HTTP %s." % err.code, err.code)
        except TMDBError:
            raise
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as err:
            if attempts < 2:
                time.sleep(1.0)
                continue
            reason = getattr(err, "reason", err)
            raise TMDBError("network", "Couldn't reach TMDB (%s). Are you online?" % type(reason).__name__)
        except ValueError:
            raise TMDBError("other", "TMDB sent a response that wasn't valid JSON.")


class FakeTMDB:
    """Deterministic synthetic TMDB used by --fake mode and the test-suite.
    Special IDs: 404404 -> 404, 429429 -> rate limit, 500500 -> network error.
    Any credential containing 'bad' is rejected as 401."""
    calls = []

    def __call__(self, path, params=None, credential=None):
        params = params or {}
        if credential is None:
            conn = db()
            credential = get_setting(conn, "tmdb_credential")
            conn.close()
        FakeTMDB.calls.append(path)
        if not credential:
            raise TMDBError("nokey", "No TMDB API key configured yet.")
        if "bad" in credential:
            raise TMDBError("auth", "TMDB rejected the API key (401). Check it in Settings.", 401)
        m = re.match(r"^/(person|movie|tv)/(\d+)$", path)
        if m:
            kind, i = m.group(1), int(m.group(2))
            if i == 404404:
                raise TMDBError("notfound", "TMDB has no record at that ID (404).", 404)
            if i == 429429:
                raise TMDBError("ratelimit", "TMDB is rate-limiting us (429). Wait a few seconds and try again.", 429)
            if i == 500500:
                raise TMDBError("network", "Couldn't reach TMDB (URLError). Are you online?")
            return getattr(self, kind)(i)
        if path == "/authentication":
            return {"success": True}
        if path == "/search/multi":
            qq = params.get("query", "")
            return {"results": [
                {"media_type": "person", "id": 777001, "name": "Fake %s Person" % qq, "known_for_department": "Directing"},
                {"media_type": "movie", "id": 888001, "title": "Fake %s Movie" % qq, "release_date": "1987-05-05"},
                {"media_type": "tv", "id": 999001, "name": "Fake %s Show" % qq, "first_air_date": "1991-01-01"}]}
        if path == "/discover/movie":
            return {"results": [{"id": 880000 + k, "title": "Discovered %d" % k, "release_date": "1985-01-01"}
                                for k in range(1, 4)]}
        raise TMDBError("notfound", "TMDB has no record at that ID (404).", 404)

    def _name(self, pid):
        conn = db()
        r = conn.execute("SELECT name FROM seed_directors WHERE tmdb_id=?", (pid,)).fetchone()
        conn.close()
        return r["name"] if r else "Fake Person %d" % pid

    def person(self, pid):
        films = [pid * 10 + k for k in range(1, 5)]
        crew = [{"id": f, "media_type": "movie", "title": "Fake Movie %d" % f,
                 "release_date": "%d-06-01" % (1970 + f % 40), "job": "Director", "department": "Directing",
                 "credit_id": "c-dir-%d-%d" % (pid, f), "poster_path": None} for f in films]
        crew.append({"id": films[0], "media_type": "movie", "title": "Fake Movie %d" % films[0],
                     "release_date": "%d-06-01" % (1970 + films[0] % 40), "job": "Writer",
                     "department": "Writing", "credit_id": "c-wri-%d" % pid})
        crew.append({"id": pid * 10 + 9, "media_type": "tv", "name": "Fake Show %d" % (pid * 10 + 9),
                     "first_air_date": "1995-09-09", "job": "Director", "department": "Directing",
                     "credit_id": "c-tv-%d" % pid, "episode_count": 2})
        return {
            "id": pid, "name": self._name(pid), "biography": "Synthetic biography for %d." % pid,
            "birthday": "1940-01-01", "deathday": None, "place_of_birth": "Burbank, California, USA",
            "known_for_department": "Directing", "profile_path": None, "popularity": 3.2,
            "also_known_as": ["Alias %d" % pid], "imdb_id": "nm%07d" % pid,
            "external_ids": {"imdb_id": "nm%07d" % pid},
            "combined_credits": {
                "cast": [{"id": pid * 10 + 5, "media_type": "movie", "title": "Cameo Movie %d" % (pid * 10 + 5),
                          "release_date": "1990-02-02", "character": "Himself", "credit_id": "c-cast-%d" % pid,
                          "order": 3}],
                "crew": crew},
        }

    def _title(self, mid, tv=False):
        director = mid // 10 if mid >= 10 else 1
        t = "t" if tv else ""
        d = {
            "id": mid, "overview": "A synthetic %s for testing (%d)." % ("show" if tv else "movie", mid),
            "tagline": "They came from the fixture file!", "poster_path": None, "backdrop_path": None,
            "vote_average": 4.2, "vote_count": 99, "popularity": 1.5, "original_language": "en", "status": "Released",
            "genres": [{"id": 27, "name": "Horror"}, {"id": 878, "name": "Science Fiction"}],
            "credits": {
                "cast": [{"id": mid + 1000, "name": "Actor %d" % (mid + 1000), "character": "Hero", "order": 0,
                          "credit_id": "m-cast-a-%s%d" % (t, mid), "known_for_department": "Acting"},
                         {"id": mid + 2000, "name": "Actor <%d> & Co" % (mid + 2000), "character": "Monster",
                          "order": 1, "credit_id": "m-cast-b-%s%d" % (t, mid)}],
                "crew": [{"id": director, "name": self._name(director), "job": "Director", "department": "Directing",
                          "credit_id": ("c-tv-%d" % director) if tv else ("c-dir-%d-%d" % (director, mid))},
                         {"id": mid + 7000, "name": "Writer %d" % (mid + 7000), "job": "Screenplay",
                          "department": "Writing", "credit_id": "m-wri-%s%d" % (t, mid)}]},
            "similar": {"results": [{"id": mid + 1, "title": "Similar %d" % (mid + 1), "name": "Similar %d" % (mid + 1),
                                     "release_date": "1988-01-01"},
                                    {"id": mid + 2, "title": "Similar %d" % (mid + 2), "name": "Similar %d" % (mid + 2)}]},
            "recommendations": {"results": [{"id": mid + 3, "title": "Rec %d" % (mid + 3), "name": "Rec %d" % (mid + 3),
                                             "media_type": "tv" if tv else "movie"}]},
            "external_ids": {"imdb_id": "tt%07d" % mid},
            "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "dQw4w9WgXcQ", "official": True}]},
        }
        if tv:
            d.update({"name": "Fake Show %d" % mid, "original_name": "Fake Show %d" % mid,
                      "first_air_date": "1995-09-09", "episode_run_time": [44],
                      "keywords": {"results": [{"id": 9663, "name": "sequel"}]}})
        else:
            d.update({"title": "Fake Movie %d" % mid, "original_title": "Fake Movie %d" % mid,
                      "release_date": "%d-06-01" % (1970 + mid % 40), "runtime": 88, "budget": 250000,
                      "revenue": 1000000, "imdb_id": "tt%07d" % mid,
                      "keywords": {"keywords": [{"id": 9663, "name": "sequel"}, {"id": 10349, "name": "killer robot"}]},
                      "release_dates": {"results": [{"iso_3166_1": "US",
                                                     "release_dates": [{"certification": "R", "type": 3}]}]}})
        return d

    def movie(self, mid):
        return self._title(mid)

    def tv(self, tid):
        return self._title(tid, tv=True)


def tmdb_get(path, params=None, credential=None):
    if CONFIG["fake"]:
        return FakeTMDB()(path, params, credential)
    return real_tmdb_get(path, params, credential)

# ----------------------------------------------------------------------------
# Import / normalisation
# ----------------------------------------------------------------------------

def _stub_title(conn, media_type, d):
    """Insert a lightweight row for a title we only know from a list (not fetched yet)."""
    if media_type not in ("movie", "tv") or not d.get("id"):
        return
    conn.execute(
        "INSERT INTO movies (media_type,id,title,original_title,release_date,poster_path,backdrop_path,overview,"
        "vote_average,vote_count,popularity,original_language) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(media_type,id) DO UPDATE SET title=COALESCE(excluded.title,movies.title),"
        " original_title=COALESCE(excluded.original_title,movies.original_title),"
        " release_date=COALESCE(excluded.release_date,movies.release_date),"
        " poster_path=COALESCE(excluded.poster_path,movies.poster_path),"
        " backdrop_path=COALESCE(excluded.backdrop_path,movies.backdrop_path),"
        " overview=COALESCE(excluded.overview,movies.overview),"
        " vote_average=COALESCE(excluded.vote_average,movies.vote_average),"
        " vote_count=COALESCE(excluded.vote_count,movies.vote_count),"
        " popularity=COALESCE(excluded.popularity,movies.popularity) WHERE movies.fetched_at IS NULL",
        (media_type, d["id"], d.get("title") or d.get("name"), d.get("original_title") or d.get("original_name"),
         d.get("release_date") or d.get("first_air_date") or None, d.get("poster_path"), d.get("backdrop_path"),
         d.get("overview"), d.get("vote_average"), d.get("vote_count"), d.get("popularity"),
         d.get("original_language")))


def _stub_person(conn, d):
    if not d.get("id"):
        return
    conn.execute(
        "INSERT INTO people (id,name,profile_path,known_for_department,popularity) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=COALESCE(excluded.name,people.name),"
        " profile_path=COALESCE(excluded.profile_path,people.profile_path),"
        " known_for_department=COALESCE(excluded.known_for_department,people.known_for_department),"
        " popularity=COALESCE(excluded.popularity,people.popularity) WHERE people.fetched_at IS NULL",
        (d["id"], d.get("name"), d.get("profile_path"), d.get("known_for_department"), d.get("popularity")))


def _insert_credit(conn, person_id, media_type, title_id, credit_type, c):
    cid = c.get("credit_id") or "x-%s-%s-%s-%s-%s-%s" % (person_id, media_type, title_id, credit_type,
                                                         c.get("job") or "", c.get("character") or "")
    conn.execute(
        "INSERT OR REPLACE INTO credits (credit_id,person_id,media_type,title_id,credit_type,character,job,"
        "department,ord,episode_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, person_id, media_type, title_id, credit_type, c.get("character"),
         c.get("job") if credit_type == "crew" else None,
         c.get("department") if credit_type == "crew" else "Acting",
         c.get("order"), c.get("episode_count")))


def save_person(conn, d):
    pid = d["id"]
    ext = d.get("external_ids") or {}
    conn.execute(
        "INSERT INTO people (id,name,profile_path,known_for_department,biography,birthday,deathday,place_of_birth,"
        "imdb_id,popularity,also_known_as,raw_json,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, profile_path=excluded.profile_path,"
        " known_for_department=excluded.known_for_department, biography=excluded.biography,"
        " birthday=excluded.birthday, deathday=excluded.deathday, place_of_birth=excluded.place_of_birth,"
        " imdb_id=excluded.imdb_id, popularity=excluded.popularity, also_known_as=excluded.also_known_as,"
        " raw_json=excluded.raw_json, fetched_at=excluded.fetched_at",
        (pid, d.get("name"), d.get("profile_path"), d.get("known_for_department"), d.get("biography"),
         d.get("birthday"), d.get("deathday"), d.get("place_of_birth"), d.get("imdb_id") or ext.get("imdb_id"),
         d.get("popularity"), json.dumps(d.get("also_known_as") or []), json.dumps(d), now_iso()))
    cc = d.get("combined_credits")
    if cc is not None:
        conn.execute("DELETE FROM credits WHERE person_id=?", (pid,))
        for ctype in ("cast", "crew"):
            for c in cc.get(ctype) or []:
                mt = c.get("media_type") or "movie"
                if mt not in ("movie", "tv") or not c.get("id"):
                    continue
                _stub_title(conn, mt, c)
                _insert_credit(conn, pid, mt, c["id"], ctype, c)
    conn.commit()


def save_title(conn, media_type, d):
    tid = d["id"]
    ext = d.get("external_ids") or {}
    cert = None
    for r in (d.get("release_dates") or {}).get("results") or []:
        if r.get("iso_3166_1") == "US":
            for rd in r.get("release_dates") or []:
                if rd.get("certification"):
                    cert = rd["certification"]
                    break
    for r in (d.get("content_ratings") or {}).get("results") or []:
        if r.get("iso_3166_1") == "US" and r.get("rating"):
            cert = r["rating"]
    vids = [v for v in (d.get("videos") or {}).get("results") or [] if v.get("site") == "YouTube" and v.get("key")]
    vids.sort(key=lambda v: (v.get("type") != "Trailer", not v.get("official")))
    trailer = vids[0]["key"] if vids else None
    runtime = d.get("runtime")
    if runtime is None and d.get("episode_run_time"):
        runtime = d["episode_run_time"][0]
    conn.execute(
        "INSERT INTO movies (media_type,id,title,original_title,release_date,runtime,overview,tagline,poster_path,"
        "backdrop_path,vote_average,vote_count,popularity,imdb_id,original_language,status,budget,revenue,"
        "certification,trailer_key,raw_json,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(media_type,id) DO UPDATE SET title=excluded.title, original_title=excluded.original_title,"
        " release_date=excluded.release_date, runtime=excluded.runtime, overview=excluded.overview,"
        " tagline=excluded.tagline, poster_path=excluded.poster_path, backdrop_path=excluded.backdrop_path,"
        " vote_average=excluded.vote_average, vote_count=excluded.vote_count, popularity=excluded.popularity,"
        " imdb_id=excluded.imdb_id, original_language=excluded.original_language, status=excluded.status,"
        " budget=excluded.budget, revenue=excluded.revenue, certification=excluded.certification,"
        " trailer_key=excluded.trailer_key, raw_json=excluded.raw_json, fetched_at=excluded.fetched_at",
        (media_type, tid, d.get("title") or d.get("name"), d.get("original_title") or d.get("original_name"),
         d.get("release_date") or d.get("first_air_date") or None, runtime, d.get("overview"), d.get("tagline"),
         d.get("poster_path"), d.get("backdrop_path"), d.get("vote_average"), d.get("vote_count"),
         d.get("popularity"), d.get("imdb_id") or ext.get("imdb_id"), d.get("original_language"), d.get("status"),
         d.get("budget"), d.get("revenue"), cert, trailer, json.dumps(d), now_iso()))
    conn.execute("DELETE FROM movie_genres WHERE media_type=? AND movie_id=?", (media_type, tid))
    for g in d.get("genres") or []:
        conn.execute("INSERT OR REPLACE INTO genres (id,name) VALUES (?,?)", (g["id"], g.get("name")))
        conn.execute("INSERT OR IGNORE INTO movie_genres VALUES (?,?,?)", (media_type, tid, g["id"]))
    kw = d.get("keywords") or {}
    conn.execute("DELETE FROM movie_keywords WHERE media_type=? AND movie_id=?", (media_type, tid))
    for k in (kw.get("keywords") or kw.get("results") or []):
        conn.execute("INSERT OR REPLACE INTO keywords (id,name) VALUES (?,?)", (k["id"], k.get("name")))
        conn.execute("INSERT OR IGNORE INTO movie_keywords VALUES (?,?,?)", (media_type, tid, k["id"]))
    cr = d.get("credits")
    if cr is not None:
        conn.execute("DELETE FROM credits WHERE media_type=? AND title_id=?", (media_type, tid))
        for ctype in ("cast", "crew"):
            for c in cr.get(ctype) or []:
                if not c.get("id"):
                    continue
                _stub_person(conn, c)
                _insert_credit(conn, c["id"], media_type, tid, ctype, c)
    conn.execute("DELETE FROM related WHERE media_type=? AND movie_id=?", (media_type, tid))
    for rel in ("similar", "recommendations"):
        for rank, r in enumerate((d.get(rel) or {}).get("results") or []):
            omt = r.get("media_type") or media_type
            if omt not in ("movie", "tv") or not r.get("id"):
                continue
            _stub_title(conn, omt, r)
            conn.execute("INSERT OR IGNORE INTO related VALUES (?,?,?,?,?,?)", (media_type, tid, rel, omt, r["id"], rank))
    conn.commit()


def fetch_person(conn, pid):
    d = tmdb_get("/person/%d" % pid, {"append_to_response": "combined_credits,external_ids,images"})
    save_person(conn, d)


def fetch_title(conn, media_type, tid):
    if media_type == "movie":
        app = "credits,similar,recommendations,keywords,external_ids,release_dates,videos"
    else:
        app = "credits,similar,recommendations,keywords,external_ids,content_ratings,videos"
    d = tmdb_get("/%s/%d" % (media_type, tid), {"append_to_response": app})
    save_title(conn, media_type, d)


def _ensure(conn, select, args, fetch, force):
    row = conn.execute(select, args).fetchone()
    if CONFIG.get("static"):
        return row, None  # static export never contacts TMDB; render whatever is in the DB
    warning = None
    if force or row is None or row["fetched_at"] is None or age_days(row["fetched_at"]) > stale_days(conn):
        try:
            fetch()
        except TMDBError as err:
            if row is not None and row["fetched_at"]:
                warning = "Showing your saved copy. Couldn't refresh from TMDB: " + err.message
            else:
                raise
        row = conn.execute(select, args).fetchone()
    return row, warning


def ensure_person(conn, pid, force=False):
    """Return (row, warning); fetches from TMDB if missing, stale, or forced."""
    return _ensure(conn, "SELECT * FROM people WHERE id=?", (pid,), lambda: fetch_person(conn, pid), force)


def ensure_title(conn, media_type, tid, force=False):
    return _ensure(conn, "SELECT * FROM movies WHERE media_type=? AND id=?", (media_type, tid),
                   lambda: fetch_title(conn, media_type, tid), force)


# ---- bulk filmography import -------------------------------------------------

def latest_job(conn, pid):
    return conn.execute("SELECT * FROM import_jobs WHERE person_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()


def filmography_targets(conn, pid, scope):
    sd = stale_days(conn)
    extra = "" if scope == "all" else " AND c.job='Director'"
    rows = conn.execute("SELECT DISTINCT c.media_type, c.title_id, m.fetched_at FROM credits c JOIN movies m ON "
                        "m.media_type=c.media_type AND m.id=c.title_id WHERE c.person_id=? AND c.media_type='movie'"
                        + extra, (pid,)).fetchall()
    return [(r["media_type"], r["title_id"]) for r in rows if age_days(r["fetched_at"]) > sd]


_job_lock = threading.Lock()


def start_filmography_import(pid, scope, delay=0.25):
    with _job_lock:
        conn = db()
        j = latest_job(conn, pid)
        if j and j["status"] == "running":
            conn.close()
            return j["id"]
        targets = filmography_targets(conn, pid, scope)
        cur = conn.execute("INSERT INTO import_jobs (person_id,scope,total,status,started_at) VALUES (?,?,?,?,?)",
                           (pid, scope, len(targets), "running", now_iso()))
        job_id = cur.lastrowid
        conn.commit()
        conn.close()

    def work():
        c = db()
        done = failed = 0
        last_err = None
        final = "finished"
        try:
            for mt, tid in targets:
                try:
                    fetch_title(c, mt, tid)
                    done += 1
                except TMDBError as err:
                    failed += 1
                    last_err = err.message
                    if err.kind in ("auth", "nokey", "ssl"):
                        final = "stopped"
                        break
                    if err.kind in ("network", "ratelimit"):
                        time.sleep(3)
                except Exception as err:
                    failed += 1
                    last_err = "%s: %s" % (type(err).__name__, err)
                c.execute("UPDATE import_jobs SET done=?, failed=?, last_error=? WHERE id=?",
                          (done, failed, last_err, job_id))
                c.commit()
                time.sleep(delay)  # be gentle with TMDB
        finally:
            c.execute("UPDATE import_jobs SET status=?, done=?, failed=?, last_error=?, finished_at=? WHERE id=?",
                      (final, done, failed, last_err, now_iso(), job_id))
            c.commit()
            c.close()

    threading.Thread(target=work, daemon=True).start()
    return job_id

# ----------------------------------------------------------------------------
# HTML rendering helpers
# ----------------------------------------------------------------------------

def e(s):
    return html.escape("" if s is None else str(s), quote=True)


def slugify(text, maxlen=60):
    """A URL-safe, ASCII slug: 'Chopping Mall!' -> 'chopping-mall'. Empty for name-less items."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:maxlen].strip("-")


def clip(text, n=160):
    """One-line, collapsed, truncated text for meta descriptions."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return (text[:n].rstrip() + "…") if len(text) > n else text


def img_url(path, size="w185"):
    return (IMG + size + path) if path else None


def local_time(iso):
    """Show a stored UTC timestamp in the Mac's local time."""
    if not iso:
        return "never"
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
        return t.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso


def year_of(date):
    return (date or "")[:4]


def q(s):
    return urllib.parse.quote(str(s), safe="")


def col(r, k, default=None):
    try:
        v = r[k]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


CSS = r"""
:root{--bg:#0e0d10;--panel:#18161c;--panel2:#211e27;--ink:#ece6da;--muted:#a69f95;--red:#ff3b3b;--yel:#ffcc33;
--teal:#35d0c4;--line:#2e2a35;--pink:#ff5fa2}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif}
body{background-image:repeating-linear-gradient(0deg,rgba(255,255,255,.012) 0 1px,transparent 1px 3px)}
a{color:var(--yel);text-decoration:none}a:hover{text-decoration:underline}
header.top{background:linear-gradient(180deg,#1d1020,#0e0d10);border-bottom:3px solid var(--red);position:sticky;top:0;z-index:5}
header.top .bar{max-width:1240px;margin:0 auto;display:flex;align-items:center;gap:18px;padding:10px 18px;flex-wrap:wrap}
.brand{font-family:Impact,"Arial Narrow Bold","Helvetica Neue",sans-serif;font-size:28px;letter-spacing:1px;text-transform:uppercase;
color:var(--red);text-shadow:2px 2px 0 #000,0 0 14px rgba(255,59,59,.45)}
.brand span{color:var(--yel)} .brand:hover{text-decoration:none}
nav a{color:var(--ink);margin-right:14px;font-weight:600;text-transform:uppercase;font-size:13px;letter-spacing:1px}
nav a:hover{color:var(--yel)}
form.search{margin-left:auto;display:flex;gap:6px}
input,textarea,select,button{font:inherit}
input[type=text],input[type=search],input[type=password],textarea,select{background:#0a090c;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:7px 10px}
input[type=search]{width:240px}
button,.btn{background:var(--red);color:#fff;border:0;border-radius:6px;padding:7px 14px;font-weight:700;cursor:pointer;
display:inline-block;text-transform:uppercase;font-size:13px;letter-spacing:.5px}
.btn.alt,button.alt{background:var(--panel2);color:var(--ink);border:1px solid var(--line)}
.btn.on,button.on{background:var(--yel);color:#111}
button:hover,.btn:hover{filter:brightness(1.12);text-decoration:none}
main{max-width:1240px;margin:0 auto;padding:22px 18px 60px}
h1,h2,h3{font-family:Impact,"Arial Narrow Bold","Helvetica Neue",sans-serif;letter-spacing:.6px;font-weight:400;line-height:1.15}
h1{font-size:40px;margin:.1em 0 .3em;text-transform:uppercase}
h2{font-size:26px;color:var(--yel);text-transform:uppercase;border-bottom:2px dashed var(--line);padding-bottom:4px;margin-top:34px}
h3{font-size:19px;color:var(--teal);text-transform:uppercase;margin:18px 0 6px}
.muted{color:var(--muted)}.small{font-size:13px}
.flash{padding:10px 14px;border-radius:8px;margin:0 0 16px;border:1px solid}
.flash.warn{background:#2a2110;border-color:#6b5414;color:#ffdc84}
.flash.err{background:#2a1214;border-color:#7a2028;color:#ffb3b3}
.flash.ok{background:#102a20;border-color:#1f6b4d;color:#a8f0cf}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card:hover{border-color:#4a4354}
.card .name{font-weight:800;font-size:17px}
.card .films{font-size:13.5px;opacity:.9;margin-top:4px}
.card .notes{font-size:13px;color:var(--muted);margin-top:3px}
.ext{font-size:11px;color:var(--muted);border:1px solid var(--line);padding:1px 6px;border-radius:10px;margin-left:6px;white-space:nowrap}
.ext:hover{color:var(--yel);border-color:var(--yel);text-decoration:none}
.badge{display:inline-block;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;padding:1px 7px;border-radius:10px;
background:var(--panel2);color:var(--muted);border:1px solid var(--line);vertical-align:middle}
.badge.in{color:var(--teal);border-color:#245e59}.badge.want{color:#111;background:var(--yel);border-color:var(--yel)}
.badge.watched{color:#111;background:var(--teal);border-color:var(--teal)}.badge.tv{color:var(--pink);border-color:#6d2c4b}
.hero{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}
.hero .poster{width:240px;flex:0 0 240px}
.hero .info{flex:1 1 420px;min-width:280px}
.poster img,.thumb img{width:100%;border-radius:8px;display:block;box-shadow:0 6px 22px rgba(0,0,0,.6)}
.noimg{background:repeating-linear-gradient(45deg,#1b1820,#1b1820 10px,#211d28 10px,#211d28 20px);border-radius:8px;
display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:12px;padding:8px;aspect-ratio:2/3}
.backdrop{height:220px;margin:-22px -18px 18px;background-size:cover;background-position:center 25%;
mask-image:linear-gradient(180deg,#000 40%,transparent);-webkit-mask-image:linear-gradient(180deg,#000 40%,transparent);opacity:.55}
.facts{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--muted);font-size:14px;margin:4px 0 10px}
.facts b{color:var(--ink);font-weight:600}
.tagline{font-style:italic;color:var(--pink);font-size:18px;margin:4px 0 8px}
.pills a{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:2px 10px;margin:0 6px 6px 0;font-size:13px;color:var(--ink)}
.pills a:hover{border-color:var(--yel);color:var(--yel);text-decoration:none}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:14px}
.tile{font-size:13px;line-height:1.3}
.tile .thumb{margin-bottom:6px}
.tile .t{font-weight:700;color:var(--ink)}
.tile a:hover .t{color:var(--yel)}
.tile .s{color:var(--muted);font-size:12px}
table.list{width:100%;border-collapse:collapse;font-size:14.5px}
table.list td,table.list th{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:middle;text-align:left}
table.list th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
table.list tr:hover td{background:#16141a}
table.list img{width:38px;border-radius:3px;display:block}
table.list .mini{width:38px;height:57px;padding:0;font-size:9px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 14px}
.tabs a{padding:5px 12px;border-radius:16px;background:var(--panel2);border:1px solid var(--line);color:var(--ink);font-size:13px;font-weight:700;text-transform:uppercase}
.tabs a.on{background:var(--red);border-color:var(--red);color:#fff}
.tabs a:hover{text-decoration:none;border-color:var(--yel)}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
.actions form{display:inline}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}
.progress{height:10px;background:#000;border-radius:6px;overflow:hidden;border:1px solid var(--line);margin:6px 0}
.progress div{height:100%;background:linear-gradient(90deg,var(--red),var(--yel))}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 6px}
.stats div{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-size:13px;color:var(--muted)}
.stats b{color:var(--yel);font-size:18px;margin-right:4px}
.toc a{margin-right:12px;font-size:13px;white-space:nowrap}
.bio{white-space:pre-line;max-height:16em;overflow:auto;padding-right:6px}
.crewcols{columns:3 260px;column-gap:28px}
.crewcols div{break-inside:avoid;margin-bottom:12px}
footer{max-width:1240px;margin:0 auto;padding:20px 18px 40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
code{background:#000;padding:1px 6px;border-radius:4px;color:var(--teal)}
ol.steps li{margin-bottom:6px}
"""

FAVICON = ("data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22>"
           "<text y=%22.9em%22 font-size=%2290%22>📼</text></svg>")


def page(title, body, warning=None, status=200, flash_kind="warn", head_extra="", description=None, image=None):
    flash = ('<div class="flash %s">%s</div>' % (flash_kind, e(warning))) if warning else ""
    fake = ' <span class="badge tv">FAKE DATA MODE</span>' if CONFIG["fake"] else ""
    meta = ""
    if description:
        d = e(clip(description))
        meta = ("<meta name='description' content='%s'>"
                "<meta property='og:type' content='website'>"
                "<meta property='og:site_name' content='Schlock Archive'>"
                "<meta property='og:title' content='%s'><meta property='og:description' content='%s'>"
                % (d, e(title), d))
        if image:
            meta += "<meta property='og:image' content='%s'>" % e(image)
    head_extra = meta + head_extra
    if CONFIG.get("static"):
        nav = "<nav><a href='/'>Directors</a></nav>"
        search = ""
        footer_note = ("A static archive of schlock directors and their films, built from "
                       "<a href='https://www.themoviedb.org/' target='_blank' rel='noopener'>TMDB</a> data. "
                       "This product uses the TMDB API but is not endorsed or certified by TMDB.")
    else:
        nav = ("<nav><a href='/'>Directors</a><a href='/library'>Library</a><a href='/watchlist'>Watchlist</a>"
               "<a href='/settings'>Settings</a></nav>")
        search = ("<form class='search' action='/search' method='get'><input type='search' name='q' "
                  "placeholder='Search people &amp; films…'><button>Go</button></form>")
        footer_note = ("Data and images from <a href='https://www.themoviedb.org/' target='_blank' "
                       "rel='noopener'>TMDB</a>. This product uses the TMDB API but is not endorsed or certified by "
                       "TMDB. Everything you browse is saved in a local SQLite file.")
    doc = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>%s"
           "<meta name='viewport' content='width=device-width,initial-scale=1'><title>%s · Schlock Archive</title>"
           "<link rel='icon' href='%s'><style>%s</style></head><body>"
           "<header class='top'><div class='bar'><a class='brand' href='/'>Schlock<span>Archive</span></a>%s"
           "%s%s</div></header>"
           "<main>%s%s</main><footer>%s</footer></body></html>"
           % (head_extra, e(title), FAVICON, CSS, fake, nav, search, flash, body, footer_note))
    return status, doc


def poster_html(path, alt, size="w185", cls="thumb"):
    u = img_url(path, size)
    if u:
        return "<div class='%s'><img loading='lazy' src='%s' alt='%s'></div>" % (cls, e(u), e(alt))
    return "<div class='%s'><div class='noimg'>%s</div></div>" % (cls, e(alt))


def title_link(media_type, tid):
    return "/%s/%d" % ("tv" if media_type == "tv" else "movie", int(tid))


def tmdb_ext(kind, i):
    return ("<a class='ext' href='https://www.themoviedb.org/%s/%d' target='_blank' rel='noopener'>TMDB ↗</a>"
            % (kind, int(i)))


def status_badge(status):
    if status == "want":
        return " <span class='badge want'>Want</span>"
    if status == "watched":
        return " <span class='badge watched'>Watched</span>"
    return ""


def title_tile(r, sub=""):
    mt = col(r, "media_type", "movie")
    t = col(r, "title") or "Untitled"
    badges = " <span class='badge tv'>TV</span>" if mt == "tv" else ""
    badges += status_badge(col(r, "status"))
    return ("<div class='tile'><a href='%s'>%s<div class='t'>%s</div></a><div class='s'>%s%s%s</div></div>"
            % (title_link(mt, r["id"]), poster_html(col(r, "poster_path"), t), e(t), e(year_of(col(r, "release_date"))),
               (" · " + e(sub)) if sub else "", badges))


def person_tile(r, sub=""):
    n = col(r, "name") or "Unknown"
    return ("<div class='tile'><a href='/person/%d'>%s<div class='t'>%s</div></a><div class='s'>%s</div></div>"
            % (int(r["id"]), poster_html(col(r, "profile_path"), n), e(n), e(sub)))


ERROR_TITLES = {"auth": "TMDB didn't accept your key", "notfound": "Not found on TMDB",
                "ratelimit": "Whoa there, TMDB says slow down", "network": "Can't reach TMDB",
                "ssl": "HTTPS certificate problem", "nokey": "Set up your TMDB key first", "other": "TMDB hiccup"}


def error_page(err, retry_url=None):
    title = ERROR_TITLES.get(err.kind, "Problem")
    status = {"notfound": 404, "nokey": 200}.get(err.kind, 502)
    body = "<h1>%s</h1><p>%s</p><div class='actions'>" % (e(title), e(err.message))
    if retry_url:
        body += "<a class='btn' href='%s'>Try again</a>" % e(retry_url)
    if err.kind in ("auth", "nokey"):
        body += "<a class='btn alt' href='/setup'>Set up API key</a>"
    body += "<a class='btn alt' href='/'>Back to directors</a></div>"
    return page(title, body, status=status)

# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------

def render_home(conn, flash=None):
    rows = conn.execute(
        "SELECT s.*, p.fetched_at, (SELECT COUNT(DISTINCT c.title_id) FROM credits c WHERE c.person_id=s.tmdb_id "
        "AND c.job='Director') AS ndir FROM seed_directors s LEFT JOIN people p ON p.id=s.tmdb_id "
        "ORDER BY s.category, s.name COLLATE NOCASE").fetchall()
    cats = {}
    for r in rows:
        cats.setdefault(r["category"], []).append(r)
    st = conn.execute("SELECT (SELECT COUNT(*) FROM people WHERE fetched_at IS NOT NULL) p,"
                      " (SELECT COUNT(*) FROM movies WHERE fetched_at IS NOT NULL) m,"
                      " (SELECT COUNT(*) FROM user_data WHERE status='want') w,"
                      " (SELECT COUNT(*) FROM user_data WHERE status='watched') d").fetchone()
    o = ["<h1>Directors of Ill Repute</h1>",
         "<p class='muted'>%d directors to start from. Click anyone to pull their full TMDB record into your local "
         "archive. Every name and film on every page links onward, so the archive grows as you browse.</p>" % len(rows),
         "<div class='stats'><div><b>%d</b>people imported</div><div><b>%d</b>films imported</div>"
         "<div><b>%d</b>want to watch</div><div><b>%d</b>watched</div></div>" % (st["p"], st["m"], st["w"], st["d"])]
    if not has_key(conn) and not CONFIG.get("static"):
        o.append("<div class='flash warn'>No TMDB key yet. <a href='/setup'>Set one up</a> to start importing.</div>")
    keys = sorted(cats.keys())
    o.append("<p class='toc'>" + " ".join("<a href='#%s'>%s</a>" % (e(k), e(CATEGORY_LABELS.get(k, (k,))[0]))
                                         for k in keys) + "</p>")
    for k in keys:
        label, blurb = CATEGORY_LABELS.get(k, (k, ""))
        o.append("<h2 id='%s'>%s <span class='muted small'>(%d)</span></h2>" % (e(k), e(label), len(cats[k])))
        if blurb:
            o.append("<p class='muted small'>%s</p>" % e(blurb))
        o.append("<div class='grid'>")
        for r in cats[k]:
            badge = (" <span class='badge in'>%d directed</span>" % r["ndir"]) if r["fetched_at"] else ""
            o.append("<div class='card'><a class='name' href='/person/%d'>%s</a>%s%s"
                     "<div class='films'>%s</div><div class='notes'>%s</div></div>"
                     % (r["tmdb_id"], e(r["name"]), badge, tmdb_ext("person", r["tmdb_id"]),
                        e(r["example_films"]), e(r["notes"])))
        o.append("</div>")
    return page("Directors", "".join(o), flash, flash_kind="ok",
                description="A browsable archive of %d schlock and b-movie directors and their films, "
                            "drawn from TMDB." % len(rows))


def render_person(conn, pid, view=None, force=False):
    row, warning = ensure_person(conn, pid, force=force)
    seed = conn.execute("SELECT * FROM seed_directors WHERE tmdb_id=?", (pid,)).fetchone()
    creds = conn.execute(
        "SELECT c.*, m.title, m.release_date, m.poster_path, m.fetched_at AS mfetched, u.status FROM credits c "
        "JOIN movies m ON m.media_type=c.media_type AND m.id=c.title_id "
        "LEFT JOIN user_data u ON u.media_type=c.media_type AND u.movie_id=c.title_id "
        "WHERE c.person_id=?", (pid,)).fetchall()
    has_dir = any(c["job"] == "Director" for c in creds)
    if view not in ("directing", "all", "acting", "crew"):
        view = "directing" if has_dir else "all"
    if CONFIG.get("static"):
        view = "all"  # one static page shows the whole filmography (no ?view= querystrings)
    filters = {"directing": lambda c: c["job"] == "Director", "acting": lambda c: c["credit_type"] == "cast",
               "crew": lambda c: c["credit_type"] == "crew", "all": lambda c: True}
    grouped = {}
    for c in creds:
        if not filters[view](c):
            continue
        g = grouped.setdefault((c["media_type"], c["title_id"]), {"r": c, "roles": []})
        role = c["job"] if c["credit_type"] == "crew" else (("as " + c["character"]) if c["character"] else "Actor")
        if role and role not in g["roles"]:
            g["roles"].append(role)
    dated = sorted([g for g in grouped.values() if g["r"]["release_date"]], key=lambda g: g["r"]["release_date"], reverse=True)
    undated = [g for g in grouped.values() if not g["r"]["release_date"]]
    items = undated + dated   # upcoming / undated first, like TMDB

    name = row["name"] or (seed["name"] if seed else "Unknown")
    o = ["<div class='hero'><div class='poster'>%s</div><div class='info'>" %
         poster_html(row["profile_path"], name, "w342", "poster"), "<h1>%s</h1>" % e(name)]
    facts = []
    if row["known_for_department"]:
        facts.append("<span>Known for <b>%s</b></span>" % e(row["known_for_department"]))
    if row["birthday"]:
        facts.append("<span>Born <b>%s</b>%s</span>" % (e(row["birthday"]),
                     (" in " + e(row["place_of_birth"])) if row["place_of_birth"] else ""))
    if row["deathday"]:
        facts.append("<span>Died <b>%s</b></span>" % e(row["deathday"]))
    imdb = (" <a class='ext' target='_blank' rel='noopener' href='https://www.imdb.com/name/%s/'>IMDb ↗</a>"
            % e(row["imdb_id"])) if row["imdb_id"] else ""
    facts.append("<span>%s%s</span>" % (tmdb_ext("person", pid), imdb))
    o.append("<div class='facts'>%s</div>" % "".join(facts))
    if seed:
        o.append("<div class='panel small'><b>From your list</b> · %s<br>%s<br><span class='muted'>%s</span></div>"
                 % (e(CATEGORY_LABELS.get(seed["category"], (seed["category"],))[0]), e(seed["example_films"]),
                    e(seed["notes"])))
    try:
        aka = json.loads(row["also_known_as"] or "[]")
    except ValueError:
        aka = []
    if aka:
        o.append("<p class='small muted'>Also known as: %s</p>" % e(", ".join(aka[:8])))
    if row["biography"]:
        o.append("<div class='bio'>%s</div>" % e(row["biography"]))

    head_extra = ""
    if not CONFIG.get("static"):
        o.append("<div class='actions'><form method='post' action='/person/%d/refresh'>"
                 "<button class='alt'>↻ Refresh from TMDB</button></form>" % pid)
        job = latest_job(conn, pid)
        running = job is not None and job["status"] == "running"
        if not running:
            pending_dir = len(filmography_targets(conn, pid, "directing"))
            pending_all = len(filmography_targets(conn, pid, "all"))
            if pending_dir:
                o.append("<form method='post' action='/person/%d/import'><input type='hidden' name='scope' value='directing'>"
                         "<button>Import directed films (%d)</button></form>" % (pid, pending_dir))
            if pending_all > pending_dir:
                o.append("<form method='post' action='/person/%d/import'><input type='hidden' name='scope' value='all'>"
                         "<button class='alt'>Import every film credit (%d)</button></form>" % (pid, pending_all))
        o.append("<span class='small muted'>Saved %s</span></div>" % e(local_time(row["fetched_at"])))
        if job:
            if running:
                pct = int(100 * (job["done"] + job["failed"]) / job["total"]) if job["total"] else 100
                head_extra = "<meta http-equiv='refresh' content='3'>"
                o.append("<div class='panel'><b>Importing filmography…</b> %d of %d done%s"
                         "<div class='progress'><div style='width:%d%%'></div></div><span class='small muted'>This page "
                         "refreshes itself. You can keep browsing in another tab.</span></div>"
                         % (job["done"] + job["failed"], job["total"],
                            (" (%d failed)" % job["failed"]) if job["failed"] else "", pct))
            elif age_days(job["finished_at"]) < 0.02:  # finished in the last ~30 minutes
                o.append("<div class='flash %s'>Filmography import %s: %d of %d imported%s.</div>"
                         % ("ok" if not job["failed"] else "warn", e(job["status"]), job["done"], job["total"],
                            (", %d failed (%s)" % (job["failed"], e(job["last_error"]))) if job["failed"] else ""))
    o.append("</div></div>")

    def count(fn):
        return len({(c["media_type"], c["title_id"]) for c in creds if fn(c)})
    o.append("<h2>Filmography</h2><div class='tabs'>")
    for v, lab in (("directing", "Directed"), ("all", "All credits"), ("acting", "Acting"), ("crew", "Crew")):
        if CONFIG.get("static"):
            o.append("<span class='%s'>%s (%d)</span>" % ("on" if v == view else "", lab, count(filters[v])))
        else:
            o.append("<a class='%s' href='/person/%d?view=%s'>%s (%d)</a>"
                     % ("on" if v == view else "", pid, v, lab, count(filters[v])))
    o.append("</div>")
    if not items:
        o.append("<p class='muted'>No credits in this view.</p>")
    else:
        o.append("<table class='list'><tr><th></th><th>Year</th><th>Title</th><th>Role</th><th></th></tr>")
        for g in items:
            r = g["r"]
            u = img_url(r["poster_path"], "w92")
            thumb = ("<img loading='lazy' src='%s' alt=''>" % e(u)) if u else "<div class='noimg mini'></div>"
            badges = " <span class='badge tv'>TV</span>" if r["media_type"] == "tv" else ""
            badges += " <span class='badge in'>in archive</span>" if r["mfetched"] else ""
            badges += status_badge(r["status"])
            o.append("<tr><td>%s</td><td>%s</td><td><a href='%s'>%s</a>%s</td><td class='muted small'>%s</td>"
                     "<td>%s</td></tr>" % (thumb, e(year_of(r["release_date"]) or "TBA"),
                                           title_link(r["media_type"], r["title_id"]), e(r["title"] or "Untitled"),
                                           badges, e(", ".join(g["roles"])), tmdb_ext(r["media_type"], r["title_id"])))
        o.append("</table>")
    return page(name, "".join(o), warning, head_extra=head_extra,
                description=(clip(row["biography"]) or
                             "%s: filmography and credits in the Schlock Archive." % name),
                image=img_url(row["profile_path"], "w342"))


def render_title(conn, media_type, tid, force=False, flash=None):
    row, warning = ensure_title(conn, media_type, tid, force=force)
    ud = conn.execute("SELECT * FROM user_data WHERE media_type=? AND movie_id=?", (media_type, tid)).fetchone()
    genres = conn.execute("SELECT g.* FROM movie_genres mg JOIN genres g ON g.id=mg.genre_id WHERE mg.media_type=? "
                          "AND mg.movie_id=? ORDER BY g.name", (media_type, tid)).fetchall()
    kws = conn.execute("SELECT k.* FROM movie_keywords mk JOIN keywords k ON k.id=mk.keyword_id WHERE mk.media_type=? "
                       "AND mk.movie_id=? ORDER BY k.name", (media_type, tid)).fetchall()
    cast = conn.execute("SELECT c.*, p.name, p.profile_path FROM credits c JOIN people p ON p.id=c.person_id "
                        "WHERE c.media_type=? AND c.title_id=? AND c.credit_type='cast' ORDER BY COALESCE(c.ord,999)",
                        (media_type, tid)).fetchall()
    crew = conn.execute("SELECT c.*, p.name, p.profile_path FROM credits c JOIN people p ON p.id=c.person_id "
                        "WHERE c.media_type=? AND c.title_id=? AND c.credit_type='crew' "
                        "ORDER BY c.department, c.job, p.name", (media_type, tid)).fetchall()
    rel = {}
    for rt in ("recommendations", "similar"):
        rel[rt] = conn.execute(
            "SELECT m.*, u.status FROM related r JOIN movies m ON m.media_type=r.other_media_type AND m.id=r.other_id "
            "LEFT JOIN user_data u ON u.media_type=m.media_type AND u.movie_id=m.id "
            "WHERE r.media_type=? AND r.movie_id=? AND r.rel_type=? ORDER BY r.rank", (media_type, tid, rt)).fetchall()
    t = row["title"] or "Untitled"
    base = title_link(media_type, tid)
    o = []
    bd = img_url(row["backdrop_path"], "w1280")
    if bd:
        o.append("<div class='backdrop' style=\"background-image:url('%s')\"></div>" % e(bd))
    o.append("<div class='hero'><div class='poster'>%s</div><div class='info'>"
             % poster_html(row["poster_path"], t, "w342", "poster"))
    o.append("<h1>%s <span class='muted' style='font-size:.6em'>%s</span>%s</h1>" % (
        e(t), e(year_of(row["release_date"])), " <span class='badge tv'>TV</span>" if media_type == "tv" else ""))
    if row["original_title"] and row["original_title"] != row["title"]:
        o.append("<p class='muted small'>Original title: %s</p>" % e(row["original_title"]))
    if row["tagline"]:
        o.append("<div class='tagline'>“%s”</div>" % e(row["tagline"]))
    facts = []
    if row["release_date"]:
        facts.append("<span><b>%s</b></span>" % e(row["release_date"]))
    if row["certification"]:
        facts.append("<span>Rated <b>%s</b></span>" % e(row["certification"]))
    if row["runtime"]:
        facts.append("<span><b>%s</b> min</span>" % e(row["runtime"]))
    if row["vote_count"]:
        facts.append("<span>TMDB <b>%.1f</b> (%s votes)</span>" % (row["vote_average"] or 0, e(row["vote_count"])))
    if row["original_language"]:
        facts.append("<span>Language <b>%s</b></span>" % e(row["original_language"]))
    if row["budget"]:
        facts.append("<span>Budget <b>$%s</b></span>" % "{:,}".format(row["budget"]))
    if row["revenue"]:
        facts.append("<span>Gross <b>$%s</b></span>" % "{:,}".format(row["revenue"]))
    links = tmdb_ext(media_type, tid)
    if row["imdb_id"]:
        links += (" <a class='ext' target='_blank' rel='noopener' href='https://www.imdb.com/title/%s/'>IMDb ↗</a>"
                  % e(row["imdb_id"]))
    if row["trailer_key"]:
        links += (" <a class='ext' target='_blank' rel='noopener' href='https://www.youtube.com/watch?v=%s'>Trailer ↗</a>"
                  % e(row["trailer_key"]))
    links += (" <a class='ext' target='_blank' rel='noopener' href='https://www.justwatch.com/us/search?q=%s'>"
              "Where to watch ↗</a>" % q(t))
    facts.append("<span>%s</span>" % links)
    o.append("<div class='facts'>%s</div>" % "".join(facts))
    if genres:
        o.append("<div class='pills'>%s</div>" % "".join(
            "<a href='/genre/%d'>%s</a>" % (g["id"], e(g["name"])) for g in genres))
    directors, seen = [], set()
    for c in crew:
        if c["job"] in ("Director", "Co-Director") and c["person_id"] not in seen:
            seen.add(c["person_id"])
            directors.append(c)
    if directors:
        o.append("<p>Directed by %s</p>" % ", ".join(
            "<a href='/person/%d'><b>%s</b></a>" % (c["person_id"], e(c["name"])) for c in directors))
    writers, seen = [], set()
    for c in crew:
        if c["department"] == "Writing" and c["person_id"] not in seen:
            seen.add(c["person_id"])
            writers.append(c)
    if writers:
        o.append("<p class='small'>Written by %s</p>" % ", ".join(
            "<a href='/person/%d'>%s</a>" % (c["person_id"], e(c["name"])) for c in writers[:6]))
    if row["overview"]:
        o.append("<p>%s</p>" % e(row["overview"]))

    # personal panel
    status = ud["status"] if ud else None
    if CONFIG.get("static"):
        if ud and (status or ud["rating"] or ud["notes"]):
            bits = []
            if status:
                bits.append(status_badge(status).strip())
            if ud["rating"]:
                bits.append("Rated <b>%d/10</b>" % ud["rating"])
            head = (" · ".join(bits)) if bits else ""
            note = ("<p>%s</p>" % e(ud["notes"])) if ud["notes"] else ""
            o.append("<div class='panel'><b>Curator's note</b> %s%s</div>" % (head, note))
    else:
        o.append("<div class='panel'><b>Your notes</b><div class='actions'>")
        for val, lab in (("want", "★ Want to watch"), ("watched", "✓ Watched"), ("", "Clear")):
            cls = "on" if val and status == val else "alt"
            o.append("<form method='post' action='%s/user'><input type='hidden' name='action' value='status'>"
                     "<input type='hidden' name='status' value='%s'><button class='%s'>%s</button></form>"
                     % (base, val, cls, lab))
        opts = "".join("<option value='%d'%s>%d</option>" % (n, " selected" if ud and ud["rating"] == n else "", n)
                       for n in range(10, 0, -1))
        o.append("</div><form method='post' action='%s/user'><input type='hidden' name='action' value='notes'>"
                 "<label class='small muted'>My rating <select name='rating'><option value=''>none</option>%s</select></label>"
                 "<textarea name='notes' rows='3' style='width:100%%;margin:8px 0;display:block' placeholder='Notes: where you "
                 "found it, who to watch it with, how bad it really is…'>%s</textarea><button>Save notes</button></form></div>"
                 % (base, opts, e(ud["notes"] if ud else "")))
        o.append("<div class='actions'><form method='post' action='%s/refresh'><button class='alt'>↻ Refresh from TMDB"
                 "</button></form><span class='small muted'>Saved %s</span></div>" % (base, e(local_time(row["fetched_at"]))))
    o.append("</div></div>")

    if kws:
        o.append("<h3>Keywords</h3><div class='pills'>%s</div>" % "".join(
            "<a href='/keyword/%d'>%s</a>" % (k["id"], e(k["name"])) for k in kws))
    if cast:
        o.append("<h2>Cast</h2><div class='tiles'>%s</div>" % "".join(person_tile(
            {"id": c["person_id"], "name": c["name"], "profile_path": c["profile_path"]}, c["character"] or "")
            for c in cast))
    if crew:
        bydept = {}
        for c in crew:
            entry = bydept.setdefault(c["department"] or "Other", {}).setdefault(c["person_id"], [c["name"], []])
            if c["job"] not in entry[1]:
                entry[1].append(c["job"])
        o.append("<h2>Crew</h2><div class='crewcols'>")
        for dept in sorted(bydept, key=lambda d: (d != "Directing", d != "Writing", d)):
            o.append("<div><h3>%s</h3>%s</div>" % (e(dept), "<br>".join(
                "<a href='/person/%d'>%s</a> <span class='muted small'>%s</span>" % (p, e(v[0]), e(", ".join(v[1])))
                for p, v in bydept[dept].items())))
        o.append("</div>")
    for rt, lab in (("recommendations", "TMDB recommends"), ("similar", "Similar titles")):
        if rel[rt]:
            o.append("<h2>%s</h2><div class='tiles'>%s</div>" % (lab, "".join(title_tile(r) for r in rel[rt])))
    return page(t, "".join(o), warning or flash, flash_kind="warn" if warning else "ok",
                description=(clip(row["overview"]) or row["tagline"] or
                            ("%s%s" % (t, (" (%s)" % year_of(row["release_date"])) if row["release_date"] else ""))),
                image=img_url(row["poster_path"], "w342"))

def render_search(conn, qstr):
    qstr = (qstr or "").strip()
    o = ["<h1>Search</h1><form action='/search' method='get' class='actions'><input type='search' name='q' value='%s' "
         "style='width:340px' autofocus><button>Search</button></form>" % e(qstr)]
    if not qstr:
        return page("Search", "".join(o))
    like = "%" + qstr + "%"
    people = conn.execute("SELECT * FROM people WHERE name LIKE ? ORDER BY fetched_at IS NULL, popularity DESC LIMIT 60",
                          (like,)).fetchall()
    titles = conn.execute("SELECT m.*, u.status FROM movies m LEFT JOIN user_data u ON u.media_type=m.media_type AND "
                          "u.movie_id=m.id WHERE m.title LIKE ? OR m.original_title LIKE ? "
                          "ORDER BY m.fetched_at IS NULL, m.popularity DESC LIMIT 60", (like, like)).fetchall()
    o.append("<h2>In your archive</h2>")
    if people:
        o.append("<h3>People</h3><div class='tiles'>%s</div>" % "".join(
            person_tile(p, p["known_for_department"] or "") for p in people))
    if titles:
        o.append("<h3>Films &amp; shows</h3><div class='tiles'>%s</div>" % "".join(title_tile(t) for t in titles))
    if not people and not titles:
        o.append("<p class='muted'>Nothing local yet.</p>")
    o.append("<h2>On TMDB</h2>")
    if not has_key(conn):
        o.append("<p class='muted'><a href='/setup'>Add a TMDB key</a> to search TMDB too.</p>")
    else:
        try:
            res = tmdb_get("/search/multi", {"query": qstr, "include_adult": "false"}).get("results") or []
            ppl = [r for r in res if r.get("media_type") == "person" and r.get("id")]
            ttl = [r for r in res if r.get("media_type") in ("movie", "tv") and r.get("id")]
            if ppl:
                o.append("<h3>People</h3><div class='tiles'>%s</div>" % "".join(
                    person_tile(r, r.get("known_for_department") or "") for r in ppl))
            if ttl:
                o.append("<h3>Films &amp; shows</h3><div class='tiles'>%s</div>" % "".join(
                    title_tile({"media_type": r["media_type"], "id": r["id"], "title": r.get("title") or r.get("name"),
                                "release_date": r.get("release_date") or r.get("first_air_date"),
                                "poster_path": r.get("poster_path")}) for r in ttl))
            if not ppl and not ttl:
                o.append("<p class='muted'>TMDB found nothing.</p>")
        except TMDBError as err:
            o.append("<div class='flash err'>TMDB search failed: %s</div>" % e(err.message))
    return page("Search: " + qstr, "".join(o))


def render_watchlist(conn, which):
    if which not in ("want", "watched", "all"):
        which = "want"
    if which == "all":
        where, args = "u.status IN ('want','watched')", ()
    else:
        where, args = "u.status=?", (which,)
    rows = conn.execute("SELECT m.*, u.status, u.rating, u.notes, u.updated_at FROM user_data u JOIN movies m ON "
                        "m.media_type=u.media_type AND m.id=u.movie_id WHERE " + where +
                        " ORDER BY u.updated_at DESC", args).fetchall()
    o = ["<h1>Watchlist</h1><div class='tabs'>"]
    for v, lab in (("want", "Want to watch"), ("watched", "Watched"), ("all", "Both")):
        o.append("<a class='%s' href='/watchlist?status=%s'>%s</a>" % ("on" if v == which else "", v, lab))
    o.append("</div><p class='small muted'><a href='/export/watchlist.csv'>Export CSV</a> (includes ratings and notes)</p>")
    if not rows:
        o.append("<p class='muted'>Nothing here yet. Use the buttons on any film page.</p>")
    else:
        o.append("<table class='list'><tr><th></th><th>Title</th><th>Year</th><th>My rating</th><th>Notes</th>"
                 "<th>Status</th></tr>")
        for r in rows:
            u = img_url(r["poster_path"], "w92")
            o.append("<tr><td>%s</td><td><a href='%s'>%s</a></td><td>%s</td><td>%s</td><td class='small muted'>%s</td>"
                     "<td>%s</td></tr>" % (("<img src='%s' alt=''>" % e(u)) if u else "<div class='noimg mini'></div>",
                                           title_link(r["media_type"], r["id"]), e(r["title"]),
                                           e(year_of(r["release_date"])), e(r["rating"] or ""),
                                           e((r["notes"] or "")[:160]), status_badge(r["status"])))
        o.append("</table>")
    return page("Watchlist", "".join(o))


def render_library(conn, kind, sort):
    kind = "people" if kind == "people" else "films"
    o = ["<h1>Your archive</h1><div class='tabs'>"]
    for v, lab in (("films", "Films & shows"), ("people", "People")):
        o.append("<a class='%s' href='/library?kind=%s'>%s</a>" % ("on" if v == kind else "", v, e(lab)))
    o.append("</div>")
    if kind == "people":
        rows = conn.execute("SELECT p.*, (SELECT COUNT(DISTINCT title_id) FROM credits c WHERE c.person_id=p.id AND "
                            "c.job='Director') nd FROM people p WHERE fetched_at IS NOT NULL "
                            "ORDER BY name COLLATE NOCASE").fetchall()
        o.append("<p class='muted'>%d people imported.</p><div class='tiles'>%s</div>" % (len(rows), "".join(
            person_tile(r, ("%d directed" % r["nd"]) if r["nd"] else (r["known_for_department"] or "")) for r in rows)))
    else:
        orders = {"added": "m.fetched_at DESC", "title": "m.title COLLATE NOCASE", "year": "m.release_date DESC",
                  "rating": "m.vote_average DESC"}
        sort = sort if sort in orders else "added"
        rows = conn.execute("SELECT m.*, u.status FROM movies m LEFT JOIN user_data u ON u.media_type=m.media_type AND "
                            "u.movie_id=m.id WHERE m.fetched_at IS NOT NULL ORDER BY " + orders[sort] +
                            " LIMIT 3000").fetchall()
        o.append("<p class='muted'>%d titles imported. Sort: %s</p>" % (len(rows), " · ".join(
            ("<b>%s</b>" % s) if s == sort else "<a href='/library?kind=films&amp;sort=%s'>%s</a>" % (s, s)
            for s in orders)))
        o.append("<div class='tiles'>%s</div>" % "".join(title_tile(r) for r in rows))
    return page("Library", "".join(o))


def render_tag(conn, kind, tag_id, discover=False):
    if kind == "genre":
        name = conn.execute("SELECT name FROM genres WHERE id=?", (tag_id,)).fetchone()
        link_table, link_col = "movie_genres", "genre_id"
    else:
        name = conn.execute("SELECT name FROM keywords WHERE id=?", (tag_id,)).fetchone()
        link_table, link_col = "movie_keywords", "keyword_id"
    rows = conn.execute("SELECT m.*, u.status FROM %s g JOIN movies m ON m.media_type=g.media_type AND m.id=g.movie_id "
                        "LEFT JOIN user_data u ON u.media_type=m.media_type AND u.movie_id=m.id WHERE g.%s=? "
                        "ORDER BY m.release_date DESC" % (link_table, link_col), (tag_id,)).fetchall()
    label = name["name"] if name else "#%d" % tag_id
    o = ["<h1>%s: %s</h1>" % (kind.title(), e(label)),
         "<p class='muted'>%d titles in your archive. %s</p>" % (len(rows), tmdb_ext(kind, tag_id)),
         "<div class='tiles'>%s</div>" % "".join(title_tile(r) for r in rows)]
    if kind == "keyword" and not CONFIG.get("static"):
        if not discover:
            o.append("<div class='actions'><a class='btn' href='/keyword/%d?discover=1'>Find more films with this "
                     "keyword on TMDB</a></div>" % tag_id)
        else:
            try:
                res = tmdb_get("/discover/movie", {"with_keywords": str(tag_id), "sort_by": "vote_count.desc",
                                                   "include_adult": "false"}).get("results") or []
                o.append("<h2>More on TMDB</h2><div class='tiles'>%s</div>" % "".join(title_tile(
                    {"media_type": "movie", "id": r["id"], "title": r.get("title"), "release_date": r.get("release_date"),
                     "poster_path": r.get("poster_path")}) for r in res if r.get("id")))
            except TMDBError as err:
                o.append("<div class='flash err'>TMDB discover failed: %s</div>" % e(err.message))
    return page(label, "".join(o),
                description="%d films in the Schlock Archive tagged %s %s." % (len(rows), kind, label))


def render_setup(conn, error=None, ok=None):
    current = get_setting(conn, "tmdb_credential")
    o = ["<h1>Set up your TMDB key</h1>"]
    if ok:
        o.append("<div class='flash ok'>%s</div>" % e(ok))
    if error:
        o.append("<div class='flash err'>%s</div>" % e(error))
    if current:
        kind = "v4 Read Access Token" if detect_auth_type(current) == "bearer" else "v3 API key"
        o.append("<p>A %s ending in <code>…%s</code> is saved. Paste a new one below to replace it.</p>"
                 % (kind, e(current[-4:])))
    o.append("""<div class='panel'><p>Schlock Archive pulls everything from <a href="https://www.themoviedb.org/" target="_blank" rel="noopener">TMDB</a>. Their API is free for personal use; you just need a key.</p>
<ol class='steps'>
<li>Create a free account at <a href="https://www.themoviedb.org/signup" target="_blank" rel="noopener">themoviedb.org/signup</a> (or log in).</li>
<li>Go to <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener">Settings → API</a> and request an API key (pick “Developer”, describe it as a personal movie archive).</li>
<li>Copy either the <b>API Key</b> (32 characters) or the <b>API Read Access Token</b> (long, starts with <code>eyJ</code>). Either works.</li>
<li>Paste it below. It is stored only in the <code>schlock.db</code> file in this folder on your Mac.</li></ol>
<form method='post' action='/setup'><input type='password' name='credential' autocomplete='off' style='width:100%;max-width:640px' placeholder='Paste your TMDB API key or Read Access Token'>
<div class='actions'><button>Save &amp; test key</button></div></form></div>""")
    if current:
        o.append("<form method='post' action='/setup/remove'><button class='alt'>Remove saved key</button></form>")
    o.append("<p class='small'><a href='/'>Browse the director list without a key →</a></p>")
    return page("Setup", "".join(o))


def render_settings(conn, flash=None):
    cur = get_setting(conn, "tmdb_credential")
    st = conn.execute("SELECT (SELECT COUNT(*) FROM people WHERE fetched_at IS NOT NULL) a,(SELECT COUNT(*) FROM people) b,"
                      "(SELECT COUNT(*) FROM movies WHERE fetched_at IS NOT NULL) c,(SELECT COUNT(*) FROM movies) d,"
                      "(SELECT COUNT(*) FROM credits) cr,(SELECT COUNT(*) FROM keywords) k").fetchone()
    size = os.path.getsize(CONFIG["db"]) if os.path.exists(CONFIG["db"]) else 0
    keydesc = (("Saved (%s ending …%s)" % ("v4 token" if detect_auth_type(cur) == "bearer" else "v3 key", e(cur[-4:])))
               if cur else "Not set.")
    o = ["<h1>Settings</h1>",
         "<div class='panel'><h3>TMDB key</h3><p>%s</p><a class='btn' href='/setup'>Change key</a></div>" % keydesc,
         "<div class='panel'><h3>Freshness</h3><form method='post' action='/settings'>Re-fetch a page from TMDB when "
         "it's older than <input type='text' name='stale_days' value='%s' style='width:70px'> days "
         "<button>Save</button></form></div>" % e(get_setting(conn, "stale_days", STALE_DAYS_DEFAULT)),
         "<div class='panel'><h3>Database</h3><p class='small'>File: <code>%s</code> (%.1f MB)<br>People: %d imported "
         "/ %d known · Titles: %d imported / %d known · Credits: %d · Keywords: %d</p><p class='small muted'>Back it up "
         "by copying that file. The full raw TMDB JSON is kept in the <code>raw_json</code> column of "
         "<code>people</code> and <code>movies</code>.</p><a class='btn alt' href='/export/watchlist.csv'>Export "
         "watchlist CSV</a></div>" % (e(CONFIG["db"]), size / 1e6, st["a"], st["b"], st["c"], st["d"], st["cr"], st["k"])]
    return page("Settings", "".join(o), flash, flash_kind="ok")

# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SchlockArchive/1.0"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        # Our own paths never contain the TMDB key.
        if CONFIG.get("quiet"):
            return
        sys.stderr.write("  %s %s\n" % (self.command, self.path.split("?")[0]))

    def send_html(self, status, doc):
        b = doc.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, loc):
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 1000000)).decode("utf-8", "replace") if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def origin_ok(self):
        """Refuse POSTs that come from other websites (only this local app may change data)."""
        o = self.headers.get("Origin") or self.headers.get("Referer")
        if not o or o == "null":
            return True
        return urllib.parse.urlparse(o).hostname in ("localhost", "127.0.0.1", "::1")

    def handle_safe(self, fn):
        conn = db()
        try:
            fn(conn)
        except TMDBError as err:
            self.send_html(*error_page(err, self.path if self.command == "GET" else None))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_html(*page("Something broke", "<h1>Something broke</h1><p>An unexpected error happened. "
                                     "Details are in the Terminal window.</p><a class='btn' href='/'>Home</a>",
                                     status=500))
            except Exception:
                pass
        finally:
            conn.close()

    def do_GET(self):
        self.handle_safe(self._get)

    def do_POST(self):
        if not self.origin_ok():
            self.send_html(*page("Blocked", "<h1>Blocked</h1><p>Cross-site request refused.</p>", status=403))
            return
        self.handle_safe(self._post)

    def _imported(self, conn, sql, args):
        r = conn.execute(sql, args).fetchone()
        return bool(r and r["fetched_at"])

    def _get(self, conn):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/":
            if not has_key(conn) and not get_setting(conn, "setup_seen"):
                set_setting(conn, "setup_seen", "1")
                return self.redirect("/setup")
            return self.send_html(*render_home(conn, "Key saved. You're ready to go." if qs.get("keysaved") else None))
        if path == "/setup":
            return self.send_html(*render_setup(conn))
        if path == "/settings":
            return self.send_html(*render_settings(conn))
        if path == "/search":
            return self.send_html(*render_search(conn, qs.get("q", "")))
        if path == "/watchlist":
            return self.send_html(*render_watchlist(conn, qs.get("status", "want")))
        if path == "/library":
            return self.send_html(*render_library(conn, qs.get("kind", "films"), qs.get("sort", "added")))
        if path == "/export/watchlist.csv":
            return self.export_csv(conn)
        m = re.match(r"^/person/(\d+)$", path)
        if m:
            pid = int(m.group(1))
            if not has_key(conn) and not self._imported(conn, "SELECT fetched_at FROM people WHERE id=?", (pid,)):
                return self.redirect("/setup")
            return self.send_html(*render_person(conn, pid, qs.get("view")))
        m = re.match(r"^/(movie|tv)/(\d+)$", path)
        if m:
            mt, tid = m.group(1), int(m.group(2))
            if not has_key(conn) and not self._imported(
                    conn, "SELECT fetched_at FROM movies WHERE media_type=? AND id=?", (mt, tid)):
                return self.redirect("/setup")
            flash = {"saved": "Saved.", "refreshed": "Refreshed from TMDB."}.get(qs.get("msg"))
            return self.send_html(*render_title(conn, mt, tid, flash=flash))
        m = re.match(r"^/(genre|keyword)/(\d+)$", path)
        if m:
            return self.send_html(*render_tag(conn, m.group(1), int(m.group(2)), qs.get("discover") == "1"))
        self.send_html(*page("Not found", "<h1>404: this tape is missing</h1><p><a href='/'>Back to the shelves</a></p>",
                             status=404))

    def _post(self, conn):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        f = self.form()
        if path == "/setup":
            cred = (f.get("credential") or "").strip().strip('"').strip("'")
            if cred.lower().startswith("bearer "):
                cred = cred[7:].strip()
            if not cred:
                return self.send_html(*render_setup(conn, error="Paste a key first."))
            if not re.match(r"^[A-Za-z0-9._\-]+$", cred):
                return self.send_html(*render_setup(conn, error="That doesn't look like a TMDB key (unexpected characters)."))
            try:
                tmdb_get("/authentication", credential=cred)
            except TMDBError as err:
                if err.kind == "auth":
                    return self.send_html(*render_setup(
                        conn, error="TMDB rejected that key. Double-check you copied the whole thing."))
                set_setting(conn, "tmdb_credential", cred)
                return self.send_html(*render_setup(conn, ok="Saved, but couldn't verify it right now: " + err.message))
            set_setting(conn, "tmdb_credential", cred)
            return self.redirect("/?keysaved=1")
        if path == "/setup/remove":
            set_setting(conn, "tmdb_credential", None)
            return self.send_html(*render_setup(conn, ok="Key removed."))
        if path == "/settings":
            try:
                v = float(f.get("stale_days", ""))
                if v <= 0:
                    raise ValueError
                set_setting(conn, "stale_days", str(int(v)) if v == int(v) else str(v))
                return self.send_html(*render_settings(conn, "Saved."))
            except ValueError:
                return self.send_html(*render_settings(conn, "Enter a positive number of days."))
        m = re.match(r"^/person/(\d+)/(refresh|import)$", path)
        if m:
            pid = int(m.group(1))
            if m.group(2) == "refresh":
                row, warning = ensure_person(conn, pid, force=True)
                if warning:
                    return self.send_html(*render_person(conn, pid))
            else:
                ensure_person(conn, pid)
                start_filmography_import(pid, "all" if f.get("scope") == "all" else "directing")
            return self.redirect("/person/%d" % pid)
        m = re.match(r"^/(movie|tv)/(\d+)/(refresh|user)$", path)
        if m:
            mt, tid, act = m.group(1), int(m.group(2)), m.group(3)
            if act == "refresh":
                row, warning = ensure_title(conn, mt, tid, force=True)
                if warning:
                    return self.send_html(*render_title(conn, mt, tid))
                return self.redirect("/%s/%d?msg=refreshed" % (mt, tid))
            ud = conn.execute("SELECT * FROM user_data WHERE media_type=? AND movie_id=?", (mt, tid)).fetchone()
            status = ud["status"] if ud else None
            rating = ud["rating"] if ud else None
            notes = ud["notes"] if ud else None
            if f.get("action") == "status":
                status = f.get("status") if f.get("status") in ("want", "watched") else None
            else:
                try:
                    rating = int(f.get("rating")) if f.get("rating") else None
                    if rating is not None and not 1 <= rating <= 10:
                        rating = None
                except ValueError:
                    rating = None
                notes = f.get("notes", "")
            conn.execute("INSERT INTO user_data (media_type,movie_id,status,rating,notes,updated_at) VALUES (?,?,?,?,?,?) "
                         "ON CONFLICT(media_type,movie_id) DO UPDATE SET status=excluded.status, rating=excluded.rating,"
                         " notes=excluded.notes, updated_at=excluded.updated_at", (mt, tid, status, rating, notes, now_iso()))
            conn.commit()
            return self.redirect("/%s/%d?msg=saved" % (mt, tid))
        self.send_html(*page("Not found", "<h1>404</h1>", status=404))

    def export_csv(self, conn):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["media_type", "tmdb_id", "title", "year", "status", "my_rating", "notes", "tmdb_url", "imdb_id"])
        for r in conn.execute("SELECT m.media_type, m.id, m.title, m.release_date, m.imdb_id, u.status, u.rating, u.notes "
                              "FROM user_data u JOIN movies m ON m.media_type=u.media_type AND m.id=u.movie_id "
                              "WHERE COALESCE(u.status,'')!='' OR COALESCE(u.notes,'')!='' OR u.rating IS NOT NULL "
                              "ORDER BY u.updated_at DESC"):
            w.writerow([r["media_type"], r["id"], r["title"], year_of(r["release_date"]), r["status"] or "",
                        r["rating"] or "", r["notes"] or "",
                        "https://www.themoviedb.org/%s/%d" % (r["media_type"], r["id"]), r["imdb_id"] or ""])
        b = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=schlock-watchlist.csv")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


# ----------------------------------------------------------------------------
# Static site export
# ----------------------------------------------------------------------------

# Internal links the exporter knows how to turn into files. Matches an optional
# ?query so stale tab/discover links collapse to their base page.
_INTERNAL_HREF = re.compile(r"href=(?P<q>['\"])(?P<path>/(?:person|movie|tv|genre|keyword)/\d+)(?:\?[^'\"]*)?(?P=q)")


def _url_to_path(url):
    """Map a route to the file that serves it: '/' -> index.html, '/person/5' -> person/5/index.html."""
    if url == "/":
        return "index.html"
    return os.path.join(url.strip("/"), "index.html")


def export_targets(conn):
    """Every page worth writing: home, plus each fetched person, title, and referenced genre/keyword."""
    urls = ["/"]
    urls += ["/person/%d" % r["id"] for r in conn.execute(
        "SELECT id FROM people WHERE fetched_at IS NOT NULL ORDER BY id")]
    urls += ["/%s/%d" % (r["media_type"], r["id"]) for r in conn.execute(
        "SELECT media_type, id FROM movies WHERE fetched_at IS NOT NULL ORDER BY media_type, id")]
    urls += ["/genre/%d" % r["id"] for r in conn.execute(
        "SELECT DISTINCT g.genre_id id FROM movie_genres g JOIN movies m "
        "ON m.media_type=g.media_type AND m.id=g.movie_id WHERE m.fetched_at IS NOT NULL ORDER BY g.genre_id")]
    urls += ["/keyword/%d" % r["id"] for r in conn.execute(
        "SELECT DISTINCT k.keyword_id id FROM movie_keywords k JOIN movies m "
        "ON m.media_type=k.media_type AND m.id=k.movie_id WHERE m.fetched_at IS NOT NULL ORDER BY k.keyword_id")]
    return urls


def render_route(conn, url):
    """Render a single route to its HTML body using the live page renderers (static mode)."""
    if url == "/":
        return render_home(conn)[1]
    m = re.match(r"^/person/(\d+)$", url)
    if m:
        return render_person(conn, int(m.group(1)))[1]
    m = re.match(r"^/(movie|tv)/(\d+)$", url)
    if m:
        return render_title(conn, m.group(1), int(m.group(2)))[1]
    m = re.match(r"^/(genre|keyword)/(\d+)$", url)
    if m:
        return render_tag(conn, m.group(1), int(m.group(2)))[1]
    raise ValueError("no static route for %s" % url)


def build_slug_url(conn):
    """Return slug_url(path): '/person/98132' -> '/person/98132-jim-wynorski' for readable, SEO URLs."""
    people = {r["id"]: slugify(r["name"]) for r in conn.execute("SELECT id, name FROM people")}
    titles = {(r["media_type"], r["id"]): slugify(r["title"])
              for r in conn.execute("SELECT media_type, id, title FROM movies")}
    genres = {r["id"]: slugify(r["name"]) for r in conn.execute("SELECT id, name FROM genres")}
    keywords = {r["id"]: slugify(r["name"]) for r in conn.execute("SELECT id, name FROM keywords")}

    def slug_url(path):
        m = re.match(r"^/(person|movie|tv|genre|keyword)/(\d+)$", path)
        if not m:
            return path
        kind, i = m.group(1), int(m.group(2))
        s = (people.get(i) if kind == "person" else
             titles.get((kind, i)) if kind in ("movie", "tv") else
             genres.get(i) if kind == "genre" else keywords.get(i))
        return "%s-%s" % (path, s) if s else path
    return slug_url


def rewrite_links(html_text, existing, slug_url):
    """Slug internal links whose target was exported (dropping any ?query); de-link the rest.

    A de-linked anchor keeps its text but loses its href, so the published site has no broken
    internal links (e.g. cast members whose pages were never imported)."""
    def repl(m):
        q, path = m.group("q"), m.group("path")
        if path in existing:
            return "href=%s%s%s" % (q, slug_url(path), q)
        return "data-unlinked=%s%s%s" % (q, path, q)
    return _INTERNAL_HREF.sub(repl, html_text)


_IMG_URL = re.compile(r"https://image\.tmdb\.org/t/p/[A-Za-z0-9/_.\-]+")


def _download_image(url):
    """Fetch an image from TMDB's public CDN (no API key needed). Returns bytes, or None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with _urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def localize_images(html_text, out_dir, cache, base_url=None, fetch=None):
    """Download every TMDB image referenced in html_text into out_dir/img/ and rewrite the HTML.

    cache maps a remote URL to its local path (or None if the download failed) so an image shared
    across pages is fetched once. A failed download is left pointing at the CDN so the page still
    works online."""
    fetch = fetch or _download_image
    for url in set(_IMG_URL.findall(html_text)):
        if url not in cache:
            rel = os.path.join("img", "t", "p", url.split("/t/p/", 1)[1])
            dest = os.path.join(out_dir, rel)
            ok = os.path.exists(dest)
            if not ok:
                data = fetch(url)
                if data:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as fh:
                        fh.write(data)
                    ok = True
            cache[url] = ("/" + rel.replace(os.sep, "/")) if ok else None
        local = cache[url]
        if local:
            html_text = html_text.replace(url, (base_url.rstrip("/") + local) if base_url else local)
    return html_text


def _write_sitemap(out_dir, base_url, locs):
    base = base_url.rstrip("/")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    lines += ["<url><loc>%s</loc></url>" % e(base + loc) for loc in locs]
    lines.append("</urlset>")
    with open(os.path.join(out_dir, "sitemap.xml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    with open(os.path.join(out_dir, "robots.txt"), "w", encoding="utf-8") as fh:
        fh.write("User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % base)


def export_site(out_dir, limit=None, base_url=None, images=False):
    """Write a static HTML copy of the current archive to out_dir. Returns stats.

    base_url (e.g. 'https://schlock.example') enables canonical links, og:url, sitemap.xml and
    robots.txt; without it the build is still complete but has no absolute-URL metadata.
    images=True downloads every referenced TMDB image into out_dir/img and rewrites the pages to
    serve them locally (slower; needs network)."""
    out_dir = os.path.abspath(out_dir)
    quiet = CONFIG.get("quiet")
    prev = CONFIG.get("static", False)
    CONFIG["static"] = True
    conn = db()
    img_cache = {}
    try:
        urls = export_targets(conn)
        if limit is not None:
            people = [u for u in urls if u.startswith("/person/")][:limit]
            rest = [u for u in urls if not u.startswith("/person/") and u != "/"]
            urls = ["/"] + people + rest
        existing = set(urls)
        slug_url = build_slug_url(conn)
        slugged = {u: slug_url(u) for u in urls}    # bare route -> slugged route
        locs = []                                   # slugged, trailing-slashed paths for the sitemap
        pages = errors = 0
        for url in urls:
            try:
                body = rewrite_links(render_route(conn, url), existing, slug_url)
                if images:
                    body = localize_images(body, out_dir, img_cache, base_url)
            except Exception as err:  # one bad record must not abort the whole build
                errors += 1
                if not quiet:
                    print("  skip %s (%s: %s)" % (url, type(err).__name__, err))
                continue
            loc = "/" if url == "/" else slugged[url] + "/"
            if base_url:
                canon = e(base_url.rstrip("/") + loc)
                body = body.replace(
                    "</head>",
                    "<link rel='canonical' href='%s'><meta property='og:url' content='%s'></head>" % (canon, canon),
                    1)
            dest = os.path.join(out_dir, _url_to_path(slugged[url]))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(body)
            locs.append(loc)
            pages += 1
            if not quiet and pages % 250 == 0:
                print("  ...%d pages" % pages, flush=True)
        if base_url:
            _write_sitemap(out_dir, base_url, locs)
    finally:
        conn.close()
        CONFIG["static"] = prev
    n_images = len([v for v in img_cache.values() if v])
    if not quiet:
        extra = (" (%d skipped)" % errors) if errors else ""
        extra += "  + sitemap.xml, robots.txt" if base_url else ""
        extra += "  + %d images" % n_images if images else ""
        print("  Exported %d pages to %s%s" % (pages, out_dir, extra))
    return {"pages": pages, "errors": errors, "out_dir": out_dir, "base_url": base_url, "images": n_images}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_server(port, tries=10):
    last = None
    for p in range(port, port + tries):
        try:
            return Server(("127.0.0.1", p), Handler), p
        except OSError as err:
            last = err
    raise SystemExit("Couldn't open a port between %d and %d (%s)." % (port, port + tries - 1, last))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Schlock Archive: local TMDB-powered b-movie archive")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--db", default=None, help="SQLite file (default: schlock.db next to app.py)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    ap.add_argument("--fake", action="store_true",
                    help="demo/test mode with synthetic TMDB data (uses schlock-fake.db, never contacts TMDB)")
    ap.add_argument("--export", metavar="DIR",
                    help="write a static HTML copy of the current archive to DIR and exit (no server, no TMDB)")
    ap.add_argument("--limit", type=int, default=None,
                    help="with --export, cap how many person pages to write (for quick test builds)")
    ap.add_argument("--base-url", default=None,
                    help="with --export, the public site URL (e.g. https://schlock.example); enables "
                         "canonical links, sitemap.xml and robots.txt")
    ap.add_argument("--images", action="store_true",
                    help="with --export, download TMDB images into the build and serve them locally "
                         "(slower, needs network)")
    a = ap.parse_args(argv)
    CONFIG["fake"] = a.fake
    CONFIG["db"] = os.path.abspath(a.db) if a.db else (os.path.join(APP_DIR, "schlock-fake.db") if a.fake else DEFAULT_DB)
    init_db()
    if a.export:
        print("\n  Exporting static site from %s" % CONFIG["db"], flush=True)
        stats = export_site(a.export, limit=a.limit, base_url=a.base_url, images=a.images)
        print("  Done: %d pages written.\n" % stats["pages"])
        return
    srv, port = make_server(a.port)
    url = "http://localhost:%d/" % port
    print("\n  Schlock Archive is running at %s" % url, flush=True)
    print("  Database: %s%s" % (CONFIG["db"], "  (FAKE DATA MODE)" if a.fake else ""))
    print("  Leave this window open while you browse. Press Ctrl+C (or close the window) to stop.\n")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped. Be kind, rewind.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
