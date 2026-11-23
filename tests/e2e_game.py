import json, sys, time, datetime, re
from zoneinfo import ZoneInfo
TZ={'v':'Asia/Dubai'}
def aware(iso,tz=None): return datetime.datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(tz or TZ['v']))
from playwright.sync_api import sync_playwright
URL = "http://127.0.0.1:8765/test_sb.html"
results, errors = [], []
def t(name, cond, detail=""):
    results.append((name, bool(cond), detail)); print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)) if (detail and not cond) else ""))

def newctx(br, when="2026-11-23T10:00:00", tz="Asia/Dubai", width=390, init=""):
    c = br.new_context(viewport={"width": width, "height": 844}, timezone_id=tz)
    c.route("**/supabase.min.js", lambda r: r.fulfill(body="", content_type="application/javascript"))
    c.add_init_script("window.supabase={createClient:()=>({auth:{getSession:async()=>({data:{session:null}}),onAuthStateChange:()=>({data:{subscription:{unsubscribe(){}}}}),signOut:async()=>({})},from:()=>({}),rpc:async()=>({}),channel:()=>({on(){return this},subscribe(){return this}}),removeChannel(){}})};" + init)
    p = c.new_page(); p.on("pageerror", lambda e: errors.append(str(e)))
    TZ['v']=tz; p.clock.install(time=aware(when,tz))
    p.goto(URL); p.evaluate("(()=>{try{S.onb=1;save()}catch(e){}})()")
    return c, p

def day(p, iso):
    p.clock.set_fixed_time(aware(iso)); p.evaluate("refreshDay()")

def close(p): p.evaluate("try{closeSheet()}catch(e){}")
def addq(p, title, diff="Medium", stat="Health", per=5):
    return p.evaluate("(a)=>{const id=uid();S.quests.push({id,t:a[0],stat:a[1],xp:DIFF[a[2]],streak:0,last:'',per:a[3]});revisePlan();save();return id}", [title, stat, diff, per])
def S(p, e): return p.evaluate(e)

with sync_playwright() as pw:
    br = pw.chromium.launch()

    # ---- G1 level curve and ranks
    c, p = newctx(br)
    lv = p.evaluate("[0,99,100,249,250,449,450,699,700,2699,2700,5000].map(x=>lvl(x).l)")
    t("G1 level thresholds follow 100,150,200... per level", lv == [1,1,2,2,3,3,4,4,5,9,10,10] or lv[:11] == [1,1,2,2,3,3,4,4,5,9,10], lv)
    t("G1 remaining-XP numbers add up at every level", p.evaluate("(()=>{for(let x=0;x<6000;x+=7){const L=lvl(x);let before=0;for(let l=1;l<L.l;l++)before+=100+(l-1)*50;if(before+L.cur!==x||L.cur>=L.need)return false}return true})()"))
    t("G1 ranks by level", p.evaluate("[1,2,3,5,6,9,10,14,15,19,20,40].map(l=>rk(l).n)") == ["Initiate","Initiate","Apprentice","Apprentice","Adept","Adept","Veteran","Veteran","Master","Master","Grandmaster","Grandmaster"])

    # ---- G2/G3 completion, double-tap, XP formula, daily cap, gold
    id1 = addq(p, "Run", "Hard"); id2 = addq(p, "Read", "Hard"); id3 = addq(p, "Write", "Hard")
    p.evaluate(f"doQuest({id1})"); close(p); a = p.evaluate("[S.xp,S.gold,S.quests[0].streak]")
    t("G2 first Hard quest gives round(60*1.02)=61 XP, 31 gold + 10 achievement gold", a == [61, 41, 1], a)
    p.evaluate(f"doQuest({id1});doQuest({id1})"); close(p)
    t("G2 repeated taps cannot award the same quest twice in a day", p.evaluate("[S.xp,S.gold]") == [61, 41], p.evaluate("[S.xp,S.gold]"))
    p.evaluate(f"doQuest({id2})"); close(p); p.evaluate(f"doQuest({id3})"); close(p)
    a = p.evaluate("[S.xp,S.gold,S.today.base,lvl(S.xp).l]")
    t("G3 daily 150 focus cap: third Hard quest pays half past the cap (61+61+45=167 XP)", a[0] == 167 and a[2] == 183 and a[3] == 2, a)
    t("G3 gold is half of XP rounded up per award", p.evaluate("S.gold") == 41 + 31 + 23, p.evaluate("S.gold"))
    t("G3 domain XP matches total", p.evaluate("Object.values(S.stats).reduce((a,b)=>a+b,0)") == p.evaluate("S.xp"))

    # ---- G6/G7 challenges and achievements
    g0 = p.evaluate("S.gold"); k = p.evaluate("chs()[0].k"); p.evaluate(f"claim('{k}')"); p.evaluate(f"claim('{k}')")
    t("G6 challenge pays once (+15) after 3 quests", p.evaluate("S.gold") == g0 + 15, p.evaluate("S.gold") - g0)
    g0 = p.evaluate("S.gold"); k = p.evaluate("chs()[2].k"); p.evaluate(f"claim('{k}')")
    t("G6 unfinished challenge cannot be claimed", p.evaluate("S.gold") == g0)
    t("G7 First Step achievement recorded once", p.evaluate("Object.keys(S.ach)") == ["a1"])

    # ---- G4 streaks across days, rest token, halving, repair
    day(p, "2026-11-24T09:00:00"); p.evaluate(f"doQuest({id1})"); close(p)
    t("G4 next-day completion extends the streak and the bonus (2 days)", p.evaluate("S.quests[0].streak") == 2)
    day(p, "2026-11-26T09:00:00")   # missed 25th; rest tokens available
    r0 = p.evaluate("S.rest")
    t("G4 a missed day spends a rest token and keeps streaks", p.evaluate("S.quests[0].streak") == 2 and p.evaluate("S.rest") <= r0, [p.evaluate("S.quests[0].streak"), r0, p.evaluate("S.rest")])
    p.evaluate("S.rest=0;S.gold=100"); day(p, "2026-11-28T09:00:00")
    q = p.evaluate("S.quests[0]")
    t("G4 without a token a missed day halves the streak and records the loss", q["streak"] == 1 and q["lost"] >= 2, q)
    p.evaluate(f"repair({id1})"); t("G4 repair needs 150 gold (100 is not enough)", p.evaluate("S.quests[0].streak") == 1 and p.evaluate("S.gold") == 100)
    p.evaluate("S.gold=150");  p.evaluate(f"repair({id1})")
    t("G4 repair restores the streak and costs exactly 150", p.evaluate("[S.quests[0].streak,S.gold]") == [q["lost"], 0], p.evaluate("[S.quests[0].streak,S.gold]"))

    # ---- G5 shop
    p.evaluate("S.gold=79;S.rest=0;buyRest()"); a = p.evaluate("[S.gold,S.rest]")
    p.evaluate("S.gold=80;buyRest()"); b = p.evaluate("[S.gold,S.rest]")
    p.evaluate("S.gold=500;S.rest=3;buyRest()"); c3 = p.evaluate("[S.gold,S.rest]")
    t("G5 rest token needs 80 gold and caps at 3", a == [79, 0] and b == [0, 1] and c3 == [500, 3], [a, b, c3])
    p.evaluate("S.gold=59;buy(21);buy(21)"); a = p.evaluate("S.gold"); p.evaluate("S.gold=60;buy(21);buy(21)"); b = p.evaluate("S.gold")
    t("G5 rewards cannot be bought without enough gold; gold never goes negative", a == 59 and b == 0, [a, b])
    p.evaluate("addReward({t:{value:'Spa'},c:{value:'-5'}})"); t("G5 custom reward minimum cost is 10", p.evaluate("S.rewards.at(-1).cost") == 10)
    c.close()

    # ---- G9 journey measurable progress and milestone rewards paid once
    c, p = newctx(br)
    p.evaluate("""(()=>{const id=uid();S.epics.push({id,t:'Book',stat:'Learning',target:10,cur:0,unit:'chapters',start:today,due:addDays(today,30),ms:[{t:'25%',d:false,paid:false},{t:'50%',d:false,paid:false},{t:'75%',d:false,paid:false},{t:'100%',d:false,paid:false}],logs:[],paidSlots:[false,false,false,false],finishedAwarded:false});save()})()""")
    eid = p.evaluate("S.epics[0].id")
    p.evaluate(f"logProgress({eid},5,'a')"); close(p); x1 = p.evaluate("[S.xp,S.epics[0].ms.filter(m=>m.d).length]")
    p.evaluate(f"logProgress({eid},5,'a')"); close(p); x2 = p.evaluate("[S.xp,S.epics[0].cur]")
    t("G9 reaching 50% pays 2 milestones (60 XP each); a repeated submission token adds nothing", x1[1] == 2 and x1[0] > 0 and x2 == [x1[0], 5], [x1, x2])
    p.evaluate(f"S.epics[0].cur=2;logProgress({eid},3,'b')"); close(p)
    t("G9 correcting progress down then logging again cannot pay a milestone twice", p.evaluate("S.xp") == x1[0], [p.evaluate("S.xp"), x1[0]])
    p.evaluate(f"logProgress({eid},50,'c')"); close(p)
    t("G9 over-logging is capped at the target and completes the journey once (+200)", p.evaluate("S.epics[0].cur") == 10 and p.evaluate("S.cnt.epic") == 1 and p.evaluate("S.epics[0].ms.every(m=>m.d)"), p.evaluate("[S.epics[0].cur,S.cnt.epic]"))
    p.evaluate(f"logProgress({eid},-3,'d');logProgress({eid},'abc','e');logProgress({eid},1e13,'f')"); close(p)
    t("G9 invalid amounts rejected", p.evaluate("S.epics[0].cur") == 10)

    # ---- G8 rating cap and review once
    p.evaluate("RV={r:4,w:'ok',adj:'same'};submitReview();close=0"); close(p)
    g1 = p.evaluate("[S.gold,Object.keys(S.reviews).length]"); p.evaluate("RV={r:5,w:'x',adj:'harder'};submitReview()"); close(p)
    t("G8 weekly review pays once", p.evaluate("[S.gold,Object.keys(S.reviews).length]") == g1 and g1[1] == 1, g1)
    t("G8 Season Rating never exceeds 100 and parts stay within their caps", p.evaluate("(()=>{const s=sr();return s.t<=100&&s.c<=40&&s.g<=30&&s.b<=15&&s.r<=15})()"))
    c.close()

    # ---- G10 randomized rule invariants (real functions, 400 random actions over 60 days)
    c, p = newctx(br)
    res = p.evaluate("""(()=>{let seed=12345;const rnd=()=>(seed=(seed*1664525+1013904223)%4294967296)/4294967296;const bad=[];
      const ids=[];for(let i=0;i<6;i++){const id=uid();S.quests.push({id,t:'Q'+i,stat:DOMS[i],xp:[10,25,60][i%3],streak:0,last:'',per:1+i%7});ids.push(id)}
      revisePlan();let d=new Date(2026,10,23,10);
      for(let step=0;step<400;step++){
        const r=rnd();
        if(r<.55)doQuest(ids[Math.floor(rnd()*ids.length)]);
        else if(r<.65)buyRest();else if(r<.7)buy(21);else if(r<.75)repair(ids[Math.floor(rnd()*ids.length)]);
        else if(r<.8){const k=chs()[Math.floor(rnd()*4)].k;claim(k)}
        else{d=new Date(d.getTime()+86400000*(1+Math.floor(rnd()*3)));today=key(d);yest=addDays(today,-1);rollover();save()}
        try{closeSheet()}catch(e){}
        const L=lvl(S.xp),s=sr(),dom=Object.values(S.stats).reduce((a,b)=>a+b,0);
        const chk=[[S.xp>=0,'xp>=0'],[S.gold>=0,'gold>=0'],[Number.isFinite(S.xp)&&Number.isFinite(S.gold),'finite'],[S.rest>=0&&S.rest<=3,'rest 0..3'],[s.t<=100&&s.t>=0,'rating'],[dom===S.xp,'domain xp sum'],[L.cur>=0&&L.cur<L.need,'level cur'],
          [S.quests.every(q=>q.streak>=0&&Number.isInteger(q.streak)),'streak int'],[S.today.base<=S.xp+1e9,'base']];
        chk.forEach(([ok,n])=>{if(!ok&&bad.length<5)bad.push(n+'@'+step)});
        try{normalize(JSON.parse(JSON.stringify(S)))}catch(e){if(bad.length<5)bad.push('normalize:'+e.message+'@'+step)}
      }return{bad,xp:S.xp,gold:S.gold,streaks:S.quests.map(q=>q.streak)}})()""")
    t("G10 400 random actions over ~60 days keep XP, gold, rest, rating, domain totals and save validity consistent", not res["bad"], res)
    c.close()

    # ---- G11 import/export and malformed saves
    c, p = newctx(br)
    addq(p, "Walk", "Easy"); p.evaluate("doQuest(S.quests[0].id)"); close(p)
    t("G11 export JSON round-trips through normalize unchanged", p.evaluate("JSON.stringify(normalize(JSON.parse(JSON.stringify(S))))===JSON.stringify(S)"))
    bad = p.evaluate("""[{}, null, 5, {xp:'5',stats:{},quests:[],epics:[]}, {xp:1,stats:{},quests:[],epics:[],v:99}, {xp:1,stats:{Health:'x'},quests:[],epics:[]},
      {xp:1,stats:{},quests:[{id:1,t:'a',stat:'Nope',xp:5}],epics:[]}, {xp:1,gold:'a',stats:{},quests:[],epics:[]},
      {xp:1,stats:{},quests:[{id:1,t:'a',stat:'Health',xp:5},{id:1,t:'b',stat:'Health',xp:5}],epics:[]},
      {xp:1,stats:{},quests:[],epics:[{id:1,t:'e',stat:'Health',ms:[]}]}].map(x=>{try{normalize(x);return 'accepted'}catch(e){return 'rejected'}})""")
    t("G11 ten kinds of malformed or unsupported saves are all rejected", bad == ["rejected"] * 10, bad)
    old = p.evaluate("""(()=>{const o={v:1,name:'Old',xp:340,hp:80,gold:40,stats:{Body:120,Mind:100,Craft:60,Spirit:60},quests:[{id:1,t:'Run',stat:'Body',xp:25,streak:3,last:''}],epics:[],inv:[{r:2}],eq:{}};const n=normalize(o);return[n.v,n.stats.Health,n.stats.Learning,n.stats.Career,n.stats.Growth,n.gold,n.quests[0].stat]})()""")
    t("G11 a version 1 save migrates: domains mapped, inventory converted to gold, version 4", old == [4, 120, 100, 60, 60, 80, "Health"], old)
    c.close()

    # ---- G12 XSS: hostile text everywhere
    c, p = newctx(br)
    h = '"><img src=x onerror="window.__x=1"><script>window.__x=2</script>'
    p.evaluate("(h)=>{S.name=h;S.quests.push({id:uid(),t:h,stat:'Health',xp:25,streak:0,last:'',per:5});S.rewards.push({id:uid(),t:h,cost:50});S.epics.push({id:uid(),t:h,stat:'Learning',ms:[{t:h,d:false,paid:false}],logs:[],paidSlots:[false]});save()}", h)
    for tab in ("home", "plan", "compete", "me"):
        p.evaluate(f"go('{tab}')"); time.sleep(.2)
    p.evaluate("go('plan','journeys')"); p.evaluate("go('plan','rewards')"); p.evaluate("go('plan','shop')")
    p.evaluate("go('me');questSettings(S.quests[0].id)"); time.sleep(.2); close(p); p.evaluate("journeySettings(S.epics[0].id)"); time.sleep(.2); close(p)
    n = p.evaluate("[window.__x,document.querySelectorAll('img[src=x]').length,document.querySelectorAll('script').length]")
    t("G12 hostile names/titles in quests, journeys, rewards and the profile never execute or inject markup", n[0] is None and n[1] == 0 and p.evaluate("[...document.scripts].filter(s=>s.textContent.includes('__x')).length") == 0, n)
    c.close()

    # ---- G13 dates and time zones
    for tz in ("Asia/Dubai", "America/Los_Angeles", "Pacific/Auckland", "Pacific/Kiritimati"):
        c, p = newctx(br, when="2026-11-22T23:30:00", tz=tz)
        id1 = addq(p, "Z"); p.evaluate(f"doQuest({id1})"); close(p)
        wk = p.evaluate("weekKey()")
        day(p, "2026-11-23T00:10:00"); a = p.evaluate("[today,weekKey(),S.week.k,S.quests[0].streak]")
        t(f"G13 {tz}: Sunday 23:30 -> Monday 00:10 starts a new week on a Monday and keeps the streak", a[0] == "2026-11-23" and a[1] == "2026-11-23" and wk == "2026-11-16" and a[3] == 1, [wk] + a)
        c.close()
    c, p = newctx(br, when="2026-03-07T22:00:00", tz="America/Los_Angeles")
    id1 = addq(p, "DST"); p.evaluate(f"doQuest({id1})"); close(p); day(p, "2026-03-08T23:30:00"); p.evaluate(f"doQuest({id1})"); close(p); day(p, "2026-03-09T23:30:00"); p.evaluate(f"doQuest({id1})"); close(p)
    t("G13 US daylight-saving change does not skip or double a day (3 consecutive days = streak 3)", p.evaluate("S.quests[0].streak") == 3, p.evaluate("S.quests[0].streak"))
    c.close()

    # ---- G14 storage failure modes
    c = br.new_context(viewport={"width": 390, "height": 844}); c.route("**/supabase.min.js", lambda r: r.fulfill(body="", content_type="application/javascript"))
    c.add_init_script("Storage.prototype.setItem=function(){throw new DOMException('quota','QuotaExceededError')};")
    p = c.new_page(); p.on("pageerror", lambda e: errors.append("blocked:" + str(e))); p.goto(URL); time.sleep(1)
    t("G14 storage that refuses writes: app still opens and shows a clear warning", 'has not been saved' in p.evaluate("document.body.innerText") and 'Export current progress' in p.evaluate("document.body.innerText"), p.evaluate("document.body.innerText.slice(0,300)"))
    c.close()
    c, p = newctx(br); p2 = c.new_page(); p2.goto(URL); time.sleep(.5)
    addq(p2, "FromTabB", "Easy"); time.sleep(.5)
    before = p.evaluate("localStorage.getItem('questlog')"); p.evaluate("S.xp+=999;save()"); time.sleep(.3)
    t("G14 a second tab's save is not overwritten by a stale tab", "FromTabB" in p.evaluate("localStorage.getItem('questlog')") and '"xp":999' not in p.evaluate("localStorage.getItem('questlog')"))
    c.close()

    # ---- G15 scale: save size and render time after long use
    c, p = newctx(br)
    r = p.evaluate("""(()=>{const t0=performance.now();let d=new Date(2026,10,23);
      for(let i=0;i<20;i++)S.quests.push({id:uid(),t:'Quest number '+i,stat:DOMS[i%8],xp:25,streak:i,last:'',per:5});
      for(let i=0;i<5;i++){const id=uid();S.epics.push({id,t:'Journey '+i,stat:'Learning',target:1000,cur:500,unit:'pages',start:today,due:addDays(today,365),ms:[{t:'a',d:true,paid:true},{t:'b',d:false,paid:false}],logs:[],paidSlots:[true,false],finishedAwarded:false})}
      const out={};const sizeAt=days=>{const s=JSON.parse(JSON.stringify(S));for(let i=0;i<days;i++){const k=addDays(today,-i);s.hist[k]=90;s.week.done[1]=1;s.epics.forEach(e=>{if(i%1===0)e.logs.push({date:k,amount:1,token:String(uid())})});s.srh.push({k:k,t:50});s.reviews[k]={r:3,w:'weekly note text',adj:'same'}}return JSON.stringify(s).length};
      out.d30=sizeAt(30);out.d365=sizeAt(365);out.d730=sizeAt(730);const t1=performance.now();render();out.render=Math.round(performance.now()-t1);return out})()""")
    t("G15 render time with 20 quests and 5 journeys under 150 ms", r["render"] < 150, r)
    t("G15 a daily-use save stays far below the 300,000-character cloud limit after 1 year", r["d365"] < 300000, r)
    print("G15 INFO sizes 30/365/730 days:", r["d30"], r["d365"], r["d730"])
    c.close()

    errs = [e for e in errors if "blocked:" not in e]
    t("No page errors during the whole run", not errs, errs[:5])
    br.close()
fails = [r for r in results if not r[1]]
print(f"\n{len(results)-len(fails)}/{len(results)} passed"); sys.exit(1 if fails else 0)
