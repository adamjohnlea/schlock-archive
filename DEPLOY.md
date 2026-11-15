# Deploying the static site to GitHub Pages

The public site is built locally with `app.py --export` and published to a `gh-pages`
branch. The build runs on your Mac (not in GitHub Actions) because `schlock.db` holds
your archive and TMDB key and is deliberately kept out of git.

Target: a **project page** at `https://USERNAME.github.io/REPO/` (served under a subpath;
the exporter prefixes every link so this works). Images are **hotlinked from TMDB** by
default — add `--images` later to self-host them.

## One-time setup

1. **Create the GitHub repo** (public), e.g. `schlock-archive`:

   ```bash
   gh repo create schlock-archive --public --source . --remote origin --push
   ```

   or make it on github.com and then:

   ```bash
   git remote add origin https://github.com/USERNAME/schlock-archive.git
   git push -u origin main
   ```

2. **Set your site URL** in `deploy.sh` — change `BASE_URL` at the top to
   `https://USERNAME.github.io/REPO` (matching your username and repo name). This value
   drives the subpath prefix, canonical links, and the sitemap, so it must be exact.

3. **Deploy** (builds `schlock.db` → `build/` → pushes `gh-pages`):

   ```bash
   ./deploy.sh
   ```

4. **Turn on Pages:** repo **Settings → Pages → Build and deployment → Source:
   "Deploy from a branch" → Branch: `gh-pages` / `/ (root)` → Save.**
   The first build takes a minute or two, then the site is live at your `BASE_URL`.

## Updating the site

Import more in the app, then run `./deploy.sh` again. Each deploy force-pushes a fresh
`gh-pages` (the site is generated, so its branch keeps no history — your source history
lives on `main`).

## Options

- **Self-host images:** `./deploy.sh --images` downloads every referenced TMDB image into
  the build and serves them locally. Slower and larger, but removes the CDN dependency.
- **Custom domain:** point your domain at Pages, set `BASE_URL=https://yourdomain`, add a
  `CNAME` file to the build (tell me and I'll have the exporter write it), and the subpath
  prefix disappears automatically (root domain = no prefix).

## Notes

- `.nojekyll` and a styled `404.html` are generated into every build.
- `sitemap.xml` is published under your project path; submit its full URL to Google Search
  Console. (On a project page, `robots.txt` at the subpath isn't read by crawlers — that's
  a GitHub Pages limitation, not a build issue. A custom domain avoids it.)
