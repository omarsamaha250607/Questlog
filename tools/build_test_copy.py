"""Makes tests/test_sb.html from app/questlog.html: removes the bundled sign-in library (the tests install a fake one)
and lets the test browser run its wait helpers (adds 'unsafe-eval' to the policy). The shipped file is never changed."""
import pathlib, re, sys
root = pathlib.Path(__file__).resolve().parent.parent
s = (root / "app" / "questlog.html").read_text(encoding="utf-8")
a, b = s.index("<!--SUPABASE-BUNDLE-START-->"), s.index("<!--SUPABASE-BUNDLE-END-->")
s = s[:a] + "<!--SUPABASE-BUNDLE-START--><!--SUPABASE-BUNDLE-END-->" + s[b + len("<!--SUPABASE-BUNDLE-END-->"):]
assert "script-src 'unsafe-inline'" in s
s = s.replace("script-src 'unsafe-inline'", "script-src 'unsafe-inline' 'unsafe-eval'", 1)
(root / "tests" / "test_sb.html").write_text(s, encoding="utf-8"); print("wrote tests/test_sb.html", len(s))
