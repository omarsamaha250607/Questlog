# Questlog

Single-file app (`app/questlog.html`) with optional cloud sync through Supabase.

- `app/questlog.html` is the shipped file. It contains the bundled `@supabase/supabase-js` 2.45.4 (MIT licence text kept inside) and a Content-Security-Policy that only allows talking to the project's own Supabase address.
- `supabase/` holds the database scripts. Run `01_schema.sql`, `03_update_restore_points.sql` and `04_account_deletion.sql` once each (then `04b_verify_deletion_setup.sql`, which only reads) in the Supabase SQL editor (all are safe to re-run). `02_access_test.html` is a manual check page.
- `tests/` holds the automated checks. `tools/build_test_copy.py` makes the test copy (no bundled library, so a fake one can be used).
- `.github/workflows/tests.yml` runs everything on each push using free GitHub runners.

Run locally: `pip install -r requirements.txt && npm install && python -m playwright install chromium && AXE_PATH=../node_modules/axe-core/axe.min.js bash tests/run_all.sh` (needs a PostgreSQL 16; set PGHOST/PGPORT/PGUSER/PGPASSWORD).

The publishable Supabase key inside the app is meant to be public; security comes from the row-level rules in `01_schema.sql`. Never put a secret / service-role key in this repository.

## Publishing (GitHub Pages)
`.github/workflows/pages.yml` publishes only `site/` (built by `tools/build_site.py`: the app plus a do-not-index tag, `robots.txt` and `.nojekyll`). It runs only when started by hand from the Actions tab, and only after Pages is set to "GitHub Actions" in the repository settings. After the first publish the address is `https://<your-github-name>.github.io/<repository-name>/`.
