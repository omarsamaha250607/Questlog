"""Builds the folder that GitHub Pages publishes: site/index.html (the app, unchanged except a "do not index" tag),
robots.txt and .nojekyll. Only these three files are ever published. Tests, SQL and docs are not."""
import pathlib
root = pathlib.Path(__file__).resolve().parent.parent
src = (root / "app" / "questlog.html").read_text(encoding="utf-8")
assert src.count("<head>") == 1 and "Content-Security-Policy" in src and "sb_publishable_" in src
for bad in ("service_role", "sb_secret_", "BEGIN PRIVATE KEY"):
    assert bad not in src, "refusing to publish: found " + bad
out = root / "site"; out.mkdir(exist_ok=True)
(out / "index.html").write_text(src.replace("<head>", '<head>\n<meta name="robots" content="noindex, nofollow">', 1), encoding="utf-8")
(out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
(out / ".nojekyll").write_text("", encoding="utf-8")
print("built site/:", sorted(p.name for p in out.iterdir()))
