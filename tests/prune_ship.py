import sys, os, pathlib
SHIP = pathlib.Path(__file__).resolve().parent.parent / "app" / "questlog.html"
assert SHIP.exists(), SHIP
SHIP_URL = SHIP.as_uri()
from playwright.sync_api import sync_playwright
res=[]
def t(n,c,d=""): res.append(bool(c)); print(("PASS " if c else "FAIL ")+n+((" :: "+str(d)) if d and not c else ""))
with sync_playwright() as pw:
    b=pw.chromium.launch(); c=b.new_context(); p=c.new_page(); p.goto("http://127.0.0.1:8765/test_sb.html"); p.wait_for_timeout(500)
    # small save is byte-identical
    p.evaluate("S.onb=1;S.quests.push({id:uid(),t:'Run',stat:'Health',xp:25,streak:2,last:'',per:5});save()")
    same=p.evaluate("localStorage.getItem('questlog')===JSON.stringify(S)"); t("normal-size save is stored unchanged",same)
    # heavy: 3000 days history, journeys with 600 logs, 300 reviews
    p.evaluate("""(()=>{S.epics.push({id:uid(),t:'Big',stat:'Learning',target:99999,cur:10,unit:'p',start:today,due:addDays(today,30),ms:[],logs:Array.from({length:3000},(_,i)=>({d:addDays(today,-i),n:1,note:'x'.repeat(30)})),paidSlots:[],finishedAwarded:false});
      for(let i=0;i<3000;i++)S.hist[addDays(today,-i)]={xp:10,q:3,Health:5,Learning:5,Career:0,Growth:0};
      for(let i=0;i<300;i++)S.reviews['2020-W'+String(i).padStart(3,'0')]={x:'y'.repeat(200)};
      window.__pre={q:S.quests.length,e:S.epics.length,xp:S.xp,gold:S.gold,ach:Object.keys(S.ach).length,streak:S.quests[0].streak};save()})()""")
    n=p.evaluate("localStorage.getItem('questlog').length"); t("heavy save is brought under the 300,000-character account limit",n<300000,n)
    t("full copy kept on this device once", p.evaluate("!!localStorage.getItem('questlog_backup_before_prune')"))
    ok=p.evaluate("S.quests.length===__pre.q&&S.epics.length===__pre.e&&S.xp===__pre.xp&&S.gold===__pre.gold&&Object.keys(S.ach).length===__pre.ach&&S.quests[0].streak===__pre.streak")
    t("quests, journeys, XP, gold, achievements and streaks untouched",ok)
    t("recent history kept", p.evaluate("!!S.hist[today]&&!!S.hist[addDays(today,-100)]"))
    t("one-time notice shown", "trimmed" in p.evaluate("document.getElementById('toast').innerText"))
    b.close()
    # ship build
    b=pw.chromium.launch(); c=b.new_context(); p=c.new_page(); viol=[];reqs=[]
    p.on("console",lambda m:viol.append(m.text) if "Content Security Policy" in m.text or "violates" in m.text else None)
    p.on("request",lambda r:reqs.append(r.url) if not r.url.startswith(("data:","blob:","file:")) else None)
    p.route("https://ispuunotdlmpduscynsf.supabase.co/**", lambda r: r.fulfill(status=400, content_type="application/json", headers={"access-control-allow-origin":"*"}, body='{"error":"invalid_grant","error_description":"Invalid login credentials"}'))  # tests never contact the real project
    p.goto(SHIP_URL); p.wait_for_timeout(1500)
    t("ship build: bundled library present",p.evaluate("typeof window.supabase?.createClient==='function'"))
    t("ship build: app renders",p.evaluate("typeof S==='object'&&document.querySelectorAll('nav button, .tab').length>0 || document.body.innerText.length>50"))
    t("ship build: no CSP violations on load",not viol,viol[:2])
    t("ship build: only requests are to the project's own address",all("ispuunotdlmpduscynsf.supabase.co" in u for u in reqs),reqs[:3])
    p.evaluate("try{fetch('https://example.com/x').catch(()=>0)}catch(e){}"); p.wait_for_timeout(500)
    t("ship build: a request to any other site is blocked by the policy",any("Content Security Policy" in v or "violates" in v for v in viol),viol[:2])
    p.evaluate("(()=>{const s=document.createElement('script');s.textContent='setTimeout(()=>{try{eval(String(1));window.__ev=1}catch(e){window.__ev=0}},0)';document.head.appendChild(s)})()"); p.wait_for_timeout(300)
    ev=p.evaluate("window.__ev")
    t("ship build: the policy refuses eval in page scripts",ev==0,ev)
    r=p.evaluate("sbClient().auth.signInWithPassword({email:'nobody@example.com',password:'abcdef'}).then(x=>({err:!!x.error,msg:(x.error&&x.error.message||'').slice(0,80)}),e=>({thrown:String(e).slice(0,80)}))")
    t("ship build: sign-in with the real library fails gracefully (no crash) when the server cannot be reached or rejects",('err' in r and r['err']) ,r)
    html=open(SHIP,encoding="utf-8").read()
    t("shipped app only calls the deletion function with a password", html.count('rpc("delete_my_account"')==1 and 'rpc("delete_my_account",{p_password:' in html and 'rpc("delete_my_account")' not in html)
    t("shipped app no longer re-signs in client-side to 'check' the password before deleting", 'signInWithPassword({email:CLOUD.name' not in html)
    b.close()
print(f"\n{sum(res)}/{len(res)} passed"); sys.exit(0 if all(res) else 1)
