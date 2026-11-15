"""End-to-end tests for Schlock Archive using the built-in fake TMDB (no network, no key).
Run:  python3 -m unittest discover -s tests -v     (from the app folder)
"""
import http.client
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import app  # noqa: E402

V3_KEY = "0123456789abcdef0123456789abcdef"
V4_TOKEN = "eyJhbGciOiJIUzI1NiJ9." + "a" * 80 + ".sig_" + "b" * 30


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        app.CONFIG["db"] = os.path.join(cls.tmp, "test.db")
        app.CONFIG["fake"] = True
        app.CONFIG["quiet"] = True
        app.init_db()
        cls.srv, cls.port = app.make_server(18787)
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        loc = r.getheader("Location")
        c.close()
        return r.status, data, loc

    def get(self, path):
        return self.req("GET", path)

    def post(self, path, **fields):
        import urllib.parse
        return self.req("POST", path, urllib.parse.urlencode(fields))

    def sql(self, q, args=()):
        c = sqlite3.connect(app.CONFIG["db"])
        try:
            return c.execute(q, args).fetchall()
        finally:
            c.close()

    def assertPageOK(self, path):
        s, body, loc = self.get(path)
        self.assertEqual(s, 200, "%s -> %s %s" % (path, s, loc))
        self.assertNotIn("Something broke", body)
        self.assertIn("</html>", body)
        return body


class T01SetupFlow(Base):
    def test_a_seed(self):
        self.assertEqual(self.sql("SELECT COUNT(*) FROM seed_directors")[0][0], 193)
        self.assertEqual(len(self.sql("SELECT DISTINCT category FROM seed_directors")), 8)

    def test_b_first_run_redirects_to_setup(self):
        s, body, loc = self.get("/")
        self.assertEqual((s, loc), (303, "/setup"))
        body = self.assertPageOK("/setup")
        self.assertIn("themoviedb.org/settings/api", body)
        self.assertIn("name='credential'", body)
        # second visit to home shows list (with a no-key banner)
        body = self.assertPageOK("/")
        self.assertIn("No TMDB key yet", body)

    def test_c_person_without_key_redirects(self):
        s, _, loc = self.get("/person/51572")
        self.assertEqual((s, loc), (303, "/setup"))

    def test_d_bad_key_rejected(self):
        s, body, _ = self.post("/setup", credential="badkey_value")
        self.assertEqual(s, 200)
        self.assertIn("TMDB rejected that key", body)
        self.assertFalse(self.sql("SELECT value FROM settings WHERE key='tmdb_credential'"))
        s, body, _ = self.post("/setup", credential="<script>")
        self.assertIn("doesn&#x27;t look like", body)

    def test_e_good_key_saved_and_masked(self):
        s, _, loc = self.post("/setup", credential="  %s  " % V3_KEY)
        self.assertEqual((s, loc), (303, "/?keysaved=1"))
        self.assertEqual(self.sql("SELECT value FROM settings WHERE key='tmdb_credential'")[0][0], V3_KEY)
        body = self.assertPageOK("/settings")
        self.assertIn("v3 key ending …cdef", body)
        self.assertNotIn(V3_KEY, body)
        self.assertNotIn(V3_KEY, self.assertPageOK("/setup"))

    def test_f_auth_type_detection(self):
        self.assertEqual(app.detect_auth_type(V3_KEY), "api_key")
        self.assertEqual(app.detect_auth_type(V4_TOKEN), "bearer")


class T02Browse(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        c = app.db()
        app.set_setting(c, "tmdb_credential", V3_KEY)
        c.close()

    def test_a_home_lists_directors_with_local_and_tmdb_links(self):
        body = self.assertPageOK("/")
        local = set(re.findall(r"href='/person/(\d+)'", body))
        self.assertEqual(len(local), 193)
        self.assertIn("https://www.themoviedb.org/person/51572", body)
        self.assertIn("Video-store".lower()[:5], body.lower())
        self.assertIn("Braddock: Missing in Action III", body)
        for label, _ in app.CATEGORY_LABELS.values():
            self.assertIn(app.e(label), body)

    def test_b_person_import_and_page(self):
        before = len(app.FakeTMDB.calls)
        body = self.assertPageOK("/person/51572")
        self.assertIn("Aaron Norris", body)
        self.assertEqual(len(app.FakeTMDB.calls), before + 1)
        row = self.sql("SELECT name, fetched_at, raw_json FROM people WHERE id=51572")[0]
        self.assertTrue(row[1])
        self.assertIn("combined_credits", json.loads(row[2]))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM credits WHERE person_id=51572")[0][0], 7)
        # default view = directing; links are local
        for mid in (515721, 515722, 515723, 515724):
            self.assertIn("href='/movie/%d'" % mid, body)
        self.assertNotIn("Cameo Movie", body)
        allv = self.assertPageOK("/person/51572?view=all")
        self.assertIn("Cameo Movie", allv)
        self.assertIn("href='/tv/515729'", allv)
        self.assertIn("Writer", allv)
        acting = self.assertPageOK("/person/51572?view=acting")
        self.assertIn("as Himself", acting)
        self.assertNotIn("/movie/515722'", acting)
        # cached: second visit does not call TMDB
        before = len(app.FakeTMDB.calls)
        self.assertPageOK("/person/51572")
        self.assertEqual(len(app.FakeTMDB.calls), before)

    def test_c_movie_import_and_page(self):
        body = self.assertPageOK("/movie/515721")
        r = self.sql("SELECT title, runtime, certification, trailer_key, imdb_id, raw_json FROM movies "
                     "WHERE media_type='movie' AND id=515721")[0]
        self.assertEqual(r[:5], ("Fake Movie 515721", 88, "R", "dQw4w9WgXcQ", "tt0515721"))
        raw = json.loads(r[5])
        for k in ("credits", "similar", "recommendations", "keywords", "external_ids", "release_dates", "videos"):
            self.assertIn(k, raw)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM movie_genres WHERE movie_id=515721")[0][0], 2)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM movie_keywords WHERE movie_id=515721")[0][0], 2)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM related WHERE movie_id=515721")[0][0], 3)
        for link in ("/person/51572'", "/person/516721'", "/person/517721'", "/person/522721'",
                     "/movie/515722'", "/movie/515723'", "/movie/515724'", "/genre/27'", "/keyword/9663'"):
            self.assertIn("href='" + link, body)
        self.assertIn("Actor &lt;517721&gt; &amp; Co", body)   # escaping
        self.assertNotIn("Actor <517721>", body)
        self.assertIn("Directed by", body)

    def test_d_tv_page(self):
        body = self.assertPageOK("/tv/515729")
        self.assertIn("Fake Show 515729", body)
        self.assertIn("href='/tv/515732'", body)  # recommendation is tv
        self.assertEqual(self.sql("SELECT runtime FROM movies WHERE media_type='tv' AND id=515729")[0][0], 44)

    def test_e_crawl_every_local_link(self):
        """Follow every local person/movie/tv link breadth-first; every page must render."""
        seen, queue, n = set(), ["/person/30678"], 0
        while queue and n < 80:
            p = queue.pop(0)
            if p in seen:
                continue
            seen.add(p)
            body = self.assertPageOK(p)
            n += 1
            for link in re.findall(r"href='(/(?:person|movie|tv|genre|keyword)/\d+)'", body):
                if link not in seen:
                    queue.append(link)
        self.assertGreaterEqual(n, 80)
        self.assertGreater(self.sql("SELECT COUNT(*) FROM people WHERE fetched_at IS NOT NULL")[0][0], 10)
        self.assertGreater(self.sql("SELECT COUNT(*) FROM movies WHERE fetched_at IS NOT NULL")[0][0], 10)

    def test_f_refresh(self):
        self.assertPageOK("/movie/600001")
        self.sql("SELECT 1")
        c = sqlite3.connect(app.CONFIG["db"])
        c.execute("UPDATE movies SET fetched_at='2000-01-01T00:00:00Z', title='OLD' WHERE id=600001")
        c.commit(); c.close()
        s, _, loc = self.post("/movie/600001/refresh")
        self.assertEqual((s, loc), (303, "/movie/600001?msg=refreshed"))
        self.assertEqual(self.sql("SELECT title FROM movies WHERE id=600001")[0][0], "Fake Movie 600001")
        self.assertIn("Refreshed from TMDB", self.assertPageOK(loc))
        s, _, loc = self.post("/person/51572/refresh")
        self.assertEqual((s, loc), (303, "/person/51572"))

    def test_g_stale_auto_refetch(self):
        self.assertPageOK("/movie/600010")
        c = sqlite3.connect(app.CONFIG["db"])
        c.execute("UPDATE movies SET fetched_at='2000-01-01T00:00:00Z' WHERE id=600010")
        c.commit(); c.close()
        before = len(app.FakeTMDB.calls)
        self.assertPageOK("/movie/600010")
        self.assertEqual(len(app.FakeTMDB.calls), before + 1)

    def test_h_watchlist_toggles_and_notes(self):
        self.assertPageOK("/movie/515722")
        s, _, loc = self.post("/movie/515722/user", action="status", status="want")
        self.assertEqual(s, 303)
        self.assertIn("Fake Movie 515722", self.assertPageOK("/watchlist?status=want"))
        self.post("/movie/515722/user", action="notes", rating="3", notes="So bad it's <great>")
        self.post("/movie/515722/user", action="status", status="watched")
        self.assertNotIn("Fake Movie 515722", self.assertPageOK("/watchlist?status=want"))
        body = self.assertPageOK("/watchlist?status=watched")
        self.assertIn("Fake Movie 515722", body)
        self.assertIn("So bad it&#x27;s &lt;great&gt;", body)
        self.assertEqual(self.sql("SELECT status, rating, notes FROM user_data WHERE movie_id=515722")[0],
                         ("watched", 3, "So bad it's <great>"))
        page = self.assertPageOK("/movie/515722")
        self.assertIn("value='3' selected", page)
        # badge shows on director page
        self.assertIn("badge watched", self.assertPageOK("/person/51572"))
        s, csvdata, _ = self.get("/export/watchlist.csv")
        self.assertIn("Fake Movie 515722", csvdata)
        self.post("/movie/515722/user", action="status", status="")
        self.assertIsNone(self.sql("SELECT status FROM user_data WHERE movie_id=515722")[0][0])
        self.assertEqual(self.sql("SELECT rating FROM user_data WHERE movie_id=515722")[0][0], 3)  # kept

    def test_i_filmography_import(self):
        pid = 63937
        self.assertPageOK("/person/%d" % pid)
        body = self.assertPageOK("/person/%d" % pid)
        self.assertIn("Import directed films (4)", body)
        s, _, loc = self.post("/person/%d/import" % pid, scope="directing")
        self.assertEqual(s, 303)
        for _ in range(100):
            st = self.sql("SELECT status, done, total FROM import_jobs WHERE person_id=? ORDER BY id DESC", (pid,))[0]
            if st[0] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(st, ("finished", 4, 4))
        n = self.sql("SELECT COUNT(*) FROM movies WHERE id BETWEEN ? AND ? AND fetched_at IS NOT NULL",
                     (pid * 10 + 1, pid * 10 + 4))[0][0]
        self.assertEqual(n, 4)
        body = self.assertPageOK("/person/%d" % pid)
        self.assertIn("4 of 4 imported", body)
        self.assertNotIn("Import directed films", body)
        self.assertIn("Import every film credit (1)", body)

    def test_j_errors_are_friendly(self):
        s, body, _ = self.get("/person/404404")
        self.assertEqual(s, 404)
        self.assertIn("Not found on TMDB", body)
        s, body, _ = self.get("/movie/429429")
        self.assertEqual(s, 502)
        self.assertIn("slow down", body)
        self.assertIn("Try again", body)
        s, body, _ = self.get("/movie/500500")
        self.assertEqual(s, 502)
        self.assertIn("Can&#x27;t reach TMDB", body)
        s, body, _ = self.get("/nope")
        self.assertEqual(s, 404)

    def test_k_stale_cache_with_tmdb_down_shows_saved_copy(self):
        self.assertPageOK("/movie/600020")
        c = sqlite3.connect(app.CONFIG["db"])
        c.execute("UPDATE movies SET fetched_at='2000-01-01T00:00:00Z' WHERE id=600020")
        c.commit(); c.close()
        orig = app.tmdb_get

        def down(*a, **k):
            raise app.TMDBError("network", "Couldn't reach TMDB (URLError). Are you online?")
        app.tmdb_get = down
        try:
            body = self.assertPageOK("/movie/600020")
            self.assertIn("Showing your saved copy", body)
            body = self.assertPageOK("/search?q=Fake")
            self.assertIn("TMDB search failed", body)
        finally:
            app.tmdb_get = orig

    def test_l_search(self):
        self.assertPageOK("/person/51572")
        body = self.assertPageOK("/search?q=Norris")
        self.assertIn("href='/person/51572'", body)
        self.assertIn("href='/person/777001'", body)
        self.assertIn("href='/movie/888001'", body)
        self.assertIn("href='/tv/999001'", body)
        self.assertPageOK("/person/777001")
        self.assertPageOK("/search?q=")

    def test_m_tags_library_settings(self):
        self.assertPageOK("/movie/515721")
        self.assertIn("Fake Movie 515721", self.assertPageOK("/genre/27"))
        body = self.assertPageOK("/keyword/9663?discover=1")
        self.assertIn("href='/movie/880001'", body)
        self.assertIn("Fake Movie", self.assertPageOK("/library"))
        self.assertIn("Aaron Norris", self.assertPageOK("/library?kind=people"))
        for srt in ("title", "year", "rating", "added"):
            self.assertPageOK("/library?kind=films&sort=" + srt)
        s, body, _ = self.post("/settings", stale_days="7")
        self.assertIn("Saved.", body)
        self.assertEqual(self.sql("SELECT value FROM settings WHERE key='stale_days'")[0][0], "7")

    def test_n_cross_site_post_blocked(self):
        s, _, _ = self.req("POST", "/setup/remove", "", {"Origin": "https://evil.example"})
        self.assertEqual(s, 403)
        self.assertTrue(self.sql("SELECT value FROM settings WHERE key='tmdb_credential'"))
        s, _, _ = self.req("POST", "/movie/515721/user", "action=status&status=want",
                           {"Origin": "http://localhost:%d" % self.port})
        self.assertEqual(s, 303)


class T03RealClient(unittest.TestCase):
    """Exercise the real HTTP client against a mocked urlopen (no network)."""

    def setUp(self):
        self.orig = app.urllib.request.urlopen
        self.seen = []
        app.MIN_INTERVAL = 0

    def tearDown(self):
        app.urllib.request.urlopen = self.orig

    def mock(self, responses):
        seq = list(responses)

        def fake(req, timeout=None, context=None):
            self.seen.append(req)
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return io.BytesIO(json.dumps(r).encode())
        app.urllib.request.urlopen = fake

    def http_err(self, code, headers=None):
        import email.message
        m = email.message.Message()
        for k, v in (headers or {}).items():
            m[k] = v
        return urllib.error.HTTPError("https://x/", code, "err", m, io.BytesIO(b"{}"))

    def test_v3_key_as_query_param(self):
        self.mock([{"id": 1}])
        self.assertEqual(app.real_tmdb_get("/movie/1", {"a": "b"}, credential=V3_KEY), {"id": 1})
        url = self.seen[0].full_url
        self.assertIn("api_key=" + V3_KEY, url)
        self.assertIsNone(self.seen[0].get_header("Authorization"))

    def test_v4_token_as_bearer(self):
        self.mock([{"id": 1}])
        app.real_tmdb_get("/movie/1", credential=V4_TOKEN)
        self.assertNotIn("api_key", self.seen[0].full_url)
        self.assertEqual(self.seen[0].get_header("Authorization"), "Bearer " + V4_TOKEN)

    def test_401_404(self):
        self.mock([self.http_err(401)])
        with self.assertRaises(app.TMDBError) as cm:
            app.real_tmdb_get("/movie/1", credential=V3_KEY)
        self.assertEqual(cm.exception.kind, "auth")
        self.assertNotIn(V3_KEY, cm.exception.message)
        self.mock([self.http_err(404)])
        with self.assertRaises(app.TMDBError) as cm:
            app.real_tmdb_get("/movie/1", credential=V3_KEY)
        self.assertEqual(cm.exception.kind, "notfound")

    def test_429_retry_then_success(self):
        self.mock([self.http_err(429, {"Retry-After": "1"}), {"ok": True}])
        t = time.time()
        self.assertEqual(app.real_tmdb_get("/movie/1", credential=V3_KEY), {"ok": True})
        self.assertGreaterEqual(time.time() - t, 0.9)

    def test_network_error(self):
        self.mock([urllib.error.URLError(OSError("down")), urllib.error.URLError(OSError("down"))])
        with self.assertRaises(app.TMDBError) as cm:
            app.real_tmdb_get("/movie/1", credential=V3_KEY)
        self.assertEqual(cm.exception.kind, "network")
        self.assertNotIn(V3_KEY, cm.exception.message)


class T04StaticExport(unittest.TestCase):
    """Export the archive to static HTML and verify the published site is clean."""

    def setUp(self):
        self.saved = dict(app.CONFIG)
        self.tmp = tempfile.mkdtemp()
        app.CONFIG["db"] = os.path.join(self.tmp, "exp.db")
        app.CONFIG["fake"] = True
        app.CONFIG["quiet"] = True
        app.CONFIG["static"] = False
        app.init_db()
        c = app.db()
        app.set_setting(c, "tmdb_credential", V3_KEY)
        app.ensure_person(c, 51572)          # pulls credits + stub titles
        app.ensure_title(c, "movie", 515721)  # fetch one of the stub films
        app.ensure_title(c, "tv", 515729)     # and one tv credit
        c.close()
        self.build = os.path.join(self.tmp, "site")

    def tearDown(self):
        app.CONFIG.clear()
        app.CONFIG.update(self.saved)

    def read(self, *parts):
        with open(os.path.join(self.build, *parts), encoding="utf-8") as f:
            return f.read()

    def assertClosed(self, rel, body):
        for path in re.findall(r"href='(/(?:person|movie|tv|genre|keyword)/[0-9][A-Za-z0-9-]*)'", body):
            f = os.path.join(self.build, path.lstrip("/"), "index.html")
            self.assertTrue(os.path.exists(f), "dead internal link %s in %s" % (path, rel))

    def test_export_produces_a_clean_static_site(self):
        stats = app.export_site(self.build, base_url="https://schlock.test")
        self.assertGreater(stats["pages"], 3)

        # pages live at slugged, human-readable paths (not bare ids)
        self.assertTrue(os.path.exists(os.path.join(self.build, "index.html")))
        self.assertTrue(os.path.exists(
            os.path.join(self.build, "person", "51572-aaron-norris", "index.html")))
        self.assertTrue(os.path.exists(
            os.path.join(self.build, "movie", "515721-fake-movie-515721", "index.html")))
        self.assertFalse(os.path.exists(os.path.join(self.build, "person", "51572", "index.html")))

        home = self.read("index.html")
        person = self.read("person", "51572-aaron-norris", "index.html")
        movie = self.read("movie", "515721-fake-movie-515721", "index.html")

        self.assertIn("Aaron Norris", home)
        self.assertIn("href='/person/51572-aaron-norris'", home)

        # no interactive chrome survives the export
        for name, doc in (("home", home), ("person", person), ("movie", movie)):
            self.assertNotIn("<form", doc, "form leaked into %s" % name)
            self.assertNotIn("method='post'", doc, "post leaked into %s" % name)
            self.assertNotIn("Refresh from TMDB", doc)
        self.assertNotIn("Import directed films", person)
        self.assertNotIn("?view=", person)
        self.assertNotIn("?discover=", movie)

        # fetched director stays linked (slugged); an un-imported stub cast member is de-linked
        self.assertIn("href='/person/51572-aaron-norris'", movie)
        self.assertNotIn("href='/person/516721", movie)

        # SEO: per-page description + canonical, plus sitemap and robots
        for doc in (home, person, movie):
            self.assertIn("<meta name='description'", doc)
            self.assertIn("<link rel='canonical' href='https://schlock.test/", doc)
        sitemap = self.read("sitemap.xml")
        self.assertIn("https://schlock.test/person/51572-aaron-norris/", sitemap)
        robots = self.read("robots.txt")
        self.assertIn("Sitemap: https://schlock.test/sitemap.xml", robots)

        # every internal link that remains points at a page we actually wrote
        self.assertClosed("index.html", home)
        self.assertClosed("person/51572-aaron-norris/index.html", person)
        self.assertClosed("movie/515721-fake-movie-515721/index.html", movie)


if __name__ == "__main__":
    unittest.main(verbosity=2)
