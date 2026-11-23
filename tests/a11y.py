import json, sys, datetime
from playwright.sync_api import sync_playwright
import os
AXE=open(os.environ.get('AXE_PATH','node_modules/axe-core/axe.min.js')).read()
URL="http://127.0.0.1:8765/test_sb.html"
today=datetime.date.today(); mon=today-datetime.timedelta(days=today.weekday())
seed={"v":4}
out={}
with sync_playwright() as pw:
    br=pw.chromium.launch()
    for width,scheme in ((390,"light"),(390,"dark"),(1280,"light")):
        c=br.new_context(viewport={"width":width,"height":844},color_scheme=scheme)
        c.route("**/supabase.min.js",lambda r:r.fulfill(body="",content_type="application/javascript"))
        c.add_init_script("window.supabase={createClient:()=>({auth:{getSession:async()=>({data:{session:null}}),onAuthStateChange:()=>({data:{subscription:{unsubscribe(){}}}}),signOut:async()=>({})},from:()=>({}),rpc:async()=>({}),channel:()=>({on(){return this},subscribe(){return this}}),removeChannel(){}})};")
        p=c.new_page(); p.goto(URL); p.evaluate("S.onb=1;S.quests.push({id:uid(),t:'Run',stat:'Health',xp:25,streak:2,last:'',per:5});S.epics.push({id:uid(),t:'Book',stat:'Learning',target:10,cur:3,unit:'pages',start:today,due:addDays(today,20),ms:[{t:'a',d:false,paid:false},{t:'b',d:false,paid:false}],logs:[],paidSlots:[false,false],finishedAwarded:false});revisePlan();save();render()")
        views=[("home","go('home')"),("plan","go('plan')"),("journeys","go('plan','journeys')"),("compete","go('compete')"),("me","go('me')"),("sheet-quick","go('home');quick()"),("sheet-addquest","go('home');openAdd('quest')"),("sheet-wizard","go('home');wiz(1)"),("sheet-logprogress","go('plan','journeys');logSheet(S.epics[0].id)")]
        for name,js in views:
            try:
                p.evaluate("try{closeSheet()}catch(e){}"); p.evaluate(js); p.wait_for_timeout(500)
                p.evaluate(AXE if not p.evaluate("!!window.axe") else "0")
                r=p.evaluate("axe.run(document,{resultTypes:['violations']}).then(r=>r.violations.map(v=>({id:v.id,impact:v.impact,n:v.nodes.length,help:v.help,ex:v.nodes[0].target.join(' ').slice(0,90)})))")
                out[f"{width}-{scheme}-{name}"]=r
            except Exception as e: out[f"{width}-{scheme}-{name}"]=[{"id":"ERROR","impact":"-","n":0,"help":str(e)[:120],"ex":""}]
        c.close()
    br.close()
agg={}
for k,v in out.items():
    for x in v: agg.setdefault((x["id"],x["impact"],x["help"]),[]).append((k,x["n"],x["ex"]))
print("views checked:",len(out),"; views with violations:",sum(1 for v in out.values() if v))
for (i,imp,h),lst in sorted(agg.items(),key=lambda z:str(z[0][1])):
    print(f"{imp} {i}: {h} — {len(lst)} views, e.g. {lst[0][0]} ({lst[0][1]} nodes) {lst[0][2]}"); print("   views:", ", ".join(x[0] for x in lst))
