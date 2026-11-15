# Schlock Archive 📼

A little web app that runs on your Mac and builds a personal, searchable archive of
b-movie and schlock directors and their films, pulled from TMDB and saved in a local
SQLite database.

It starts with your list of 193 directors. Click a name and the app pulls that
person's full TMDB record (bio plus every credit) into the database and builds a page
for them. Every film, cast member, crew member, and similar or recommended title on
every page links to its own local page, which imports itself the first time you click
it. So the archive grows as you browse.

No installs: it uses only the Python 3 that comes with macOS (or one from python.org).

## Starting it

**Easiest:** double-click **`Start Schlock Archive.command`** in this folder.
A Terminal window opens and your browser goes to <http://localhost:8787>.
Leave the Terminal window open while you browse; close it (or press Ctrl+C) to stop.

> The first time, macOS may say it "can't be opened because it is from an unidentified
> developer." Right-click (or Control-click) the file, choose **Open**, then **Open** again.
> If it says Python is missing, install it from <https://www.python.org/downloads/macos/>
> or accept the prompt to install the Command Line Tools.

**From Terminal:**

```bash
cd /path/to/schlock-archive
python3 app.py
```

Options: `--port 9000` to use another port (if 8787 is busy it tries the next few
automatically), `--no-browser` to skip opening a browser, `--db other.db` for a separate
database, and `--fake` for a demo with made-up data (see Testing).

## One-time setup: a free TMDB API key

The first time you open it, you'll land on a setup page:

1. Make a free account at <https://www.themoviedb.org/signup>.
2. Go to **Settings → API** (<https://www.themoviedb.org/settings/api>) and request an API key
   (pick "Developer" and describe it as a personal movie archive).
3. Copy **either** the short **API Key** (v3) **or** the long **API Read Access Token**
   (v4, starts with `eyJ`). Both work.
4. Paste it into the setup page. The app checks it with TMDB and saves it.

The key is stored only in `schlock.db` in this folder. It is never printed or logged.
To change or remove it, use **Settings**.

You can browse the director list without a key, but importing needs one.

## What you can do

- **Directors** (home): your 193 directors in their 8 categories, with example films and
  notes. Each name opens a local page; the small `TMDB ↗` tag opens TMDB itself.
- **Person pages**: photo, bio, birth and death dates, and the filmography with tabs for
  **Directed / All credits / Acting / Crew**. Films already in the archive are marked, and
  so are films you've flagged to watch or have seen.
  - **Import directed films** fetches every film they directed in the background (gently,
    a few per second) so the details are ready. **Import every film credit** does the same
    for all of their film credits. The page shows progress.
- **Film pages**: poster, tagline, rating, runtime, US certificate, genres, keywords, full
  cast and crew, TMDB recommendations, similar titles, and links to IMDb, the trailer,
  and JustWatch. TV credits work too.
- **Your notes**: **Want to watch** / **Watched** buttons, a 1–10 rating, and free-text
  notes on any film. They're saved in the database and gathered on the **Watchlist** page,
  which also exports a CSV.
- **Search** (top right): searches your archive first, then TMDB.
- **Keyword pages**: every film in your archive with that keyword, plus a button that asks
  TMDB for more films with it (good for finding more killer-robot movies, for example).
- **Library**: everything you've imported, sortable.
- **Refresh from TMDB** on every page. Pages also refresh themselves automatically once
  they're 30 days old (you can change that in Settings).

If TMDB is down, rate-limits you, or doesn't know an ID, you get a plain-English page with
a **Try again** button. Pages you've already imported still work offline.

## Files

| File | What it is |
|---|---|
| `app.py` | The whole app (Python standard library only). |
| `Start Schlock Archive.command` | Double-click launcher for macOS. |
| `data/directors.csv` | The 193-director seed list. Edit it to add directors (any row with a `tmdb_id`); the app picks up changes on its next start. |
| `schlock.db` | Your archive (created on first run). **Back up this file** to keep your notes and imports. |
| `tests/test_app.py` | Automated tests (use fake data; no key or internet needed). |

## For the curious: the database

SQLite tables: `people`, `movies` (films and TV, keyed by `media_type` + `id`),
`credits` (person ↔ title with job, department, character, and order), `genres`,
`movie_genres`, `keywords`, `movie_keywords`, `related` (similar and recommended titles),
`user_data` (your status, rating, and notes), `seed_directors`, `import_jobs`, and
`settings`. The full raw TMDB response is kept in `people.raw_json` and
`movies.raw_json`, so nothing is lost even where the tidy tables don't have a column for it.

Open it with any SQLite tool, e.g. `sqlite3 schlock.db` in Terminal or DB Browser for SQLite.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

To click around with made-up data and no key: `python3 app.py --fake`. Fake mode uses its
own database, `schlock-fake.db`, and never contacts TMDB. Any key you paste in fake mode
works, unless it contains the word "bad".

## Troubleshooting

- **"HTTPS certificate problem"**: some python.org installs ship without certificates. The
  app tries to use your Mac's built-in certificates automatically. If that still fails,
  open **Applications → Python 3.x** and double-click **Install Certificates.command**.
- **Port in use**: run `python3 app.py --port 8800`.
- **Start over**: quit the app and delete `schlock.db` (this also deletes your notes and saved key).

*This product uses the TMDB API but is not endorsed or certified by TMDB.*
