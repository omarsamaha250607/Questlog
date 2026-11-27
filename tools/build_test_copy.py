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
# The shipped file may carry the real public Turnstile site key. Test copies never use it:
# the default copy has the bot check OFF (empty key); a second copy has it ON with a fake key.
import re as _re
_pat = _re.compile(r'CAPTCHA_SITEKEY="[^"]*"')
assert len(_pat.findall(s)) == 1
s_off = _pat.sub('CAPTCHA_SITEKEY=""', s)
s_on = _pat.sub('CAPTCHA_SITEKEY="TEST-SITE-KEY"', s)
(root / "tests" / "test_sb.html").write_text(s_off, encoding="utf-8")
(root / "tests" / "test_sb_cap.html").write_text(s_on, encoding="utf-8")
