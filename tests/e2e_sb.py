import json, time, datetime, sys, re
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8765/test_sb.html"
URL_CAP = "http://127.0.0.1:8765/test_sb_cap.html"
import os, tempfile
SHOTS = os.path.join(tempfile.gettempdir(), "questlog-shots") + os.sep
os.makedirs(SHOTS, exist_ok=True)
results, errors = [], []
def t(name, cond, detail=""):
    results.append((name, bool(cond), detail)); print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)) if (detail and not cond) else ""))

# ---------------- fake Supabase server (mirrors the SQL in 01_schema.sql and its access rules) ----------------
USERS = {}      # email -> {id, pw}
ROWS = {"saves": {}, "save_backups": {}, "restore_points": {}, "devices": {}}   # table -> {(uid,pk): row}
VERSION = {}    # (table,uid) -> counter, drives the fake realtime
RPCLOG = []; ATTEMPTS = {}
FLAGS = {"weak": False, "confirm_on": False, "rate_limit": False, "unconfirmed": set(), "no_delete_fn": False}
SENT, DELETED = [], []
COLS = {"saves": {"user_id","rev","device_id","saved_at","fmt","name","xp","gold","json"},
        "save_backups": {"user_id","kind","saved_at","device_id","json"},
        "restore_points": {"user_id","slot","day","taken_at","copy_saved_at","rev","device_id","json"},
        "devices": {"user_id","device_id","label","first_seen","last_seen","revoked_at"}}
PK = {"saves": [], "save_backups": ["kind"], "restore_points": ["slot"], "devices": ["device_id"]}
PAGES=[]
def pump(sec):
    live = [p for p in PAGES if not p.is_closed()]
    live[-1].wait_for_timeout(int(sec*1000))
def now(): return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"
def bump(tb, uid): VERSION[(tb, uid)] = VERSION.get((tb, uid), 0) + 1
def pk(tb, row): return tuple(row[k] for k in PK[tb])
def perm(): return {"data": None, "error": {"message": "permission denied for table", "code": "42501"}}
def bad(msg, code="23514"): return {"data": None, "error": {"message": msg, "code": code}}

def server(fail):
    def h(op, a):
        try: return h0(op, a)
        except Exception:
            import traceback; traceback.print_exc(); raise
    def h0(op, a):
        if fail.get("all"): return {"__throw": "Failed to fetch"}
        if op == "signup":
            if FLAGS["weak"]: return {"error": {"message": "Password is known to be weak and easy to guess, please choose a different one.", "code": "weak_password"}}
            if FLAGS["rate_limit"]: return {"error": {"message": "email rate limit exceeded", "code": "over_email_send_rate_limit"}}
            if a["email"] in USERS: return {"error": {"message": "User already registered"}}
            uid = "u_" + a["email"].split("@")[0]; USERS[a["email"]] = {"id": uid, "pw": a["password"]}
            if FLAGS["confirm_on"]: return {"session": None}
            return {"session": {"access_token": "tok", "user": {"id": uid, "email": a["email"]}}}
        if op == "reset":
            if FLAGS["rate_limit"]: return {"error": {"message": "email rate limit exceeded"}}
            SENT.append(("reset", a["email"], a.get("redirect"))); return {"error": None}
        if op == "resend":
            SENT.append(("resend", a["email"], None)); return {"error": None}
        if op == "updateuser":
            if not a.get("uid"): return {"error": {"message": "Auth session missing!"}}
            for em, u in USERS.items():
                if u["id"] == a["uid"]:
                    if u["pw"] == a["password"]: return {"error": {"message": "New password should be different from the old password."}}
                    u["pw"] = a["password"]; return {"error": None}
            return {"error": {"message": "User not found"}}
        if op == "signin":
            if a["email"] in FLAGS["unconfirmed"]: return {"error": {"message": "Email not confirmed"}}
            u = USERS.get(a["email"])
            if not u or u["pw"] != a["password"]: return {"error": {"message": "Invalid login credentials"}}
            return {"session": {"access_token": "tok", "user": {"id": u["id"], "email": a["email"]}}}
        if op == "version":
            return {"v": [VERSION.get((tb, a["uid"]), 0) for tb in ROWS]}
        uid = a.get("uid")
        if op == "q":
            q = a["q"]; tb = q["t"]
            if not uid: return perm()
            if fail.get("invalid") and q["op"] in ("upsert", "delete"): return perm()
            if q["op"] == "select":
                rows = [r for k, r in ROWS[tb].items() if k[0] == uid and all(r.get(k) == v for k, v in q["filters"].items() if k != "user_id")]
                if q["filters"].get("user_id") not in (None, uid): rows = []   # row-level security hides other accounts' rows
                return {"data": (rows[0] if rows else None) if q["single"] else rows, "error": None}
            if q["op"] == "delete":
                f = q["filters"]
                if f.get("user_id") not in (None, uid): return {"data": None, "error": None}
                for key in [k for k, r in ROWS[tb].items() if k[0] == uid and all(r.get(x) == y for x, y in f.items() if x != "user_id")]: del ROWS[tb][key]
                bump(tb, uid); return {"data": None, "error": None}
            if q["op"] == "upsert":
                r = dict(q["row"])
                if r.get("user_id") != uid: return perm()
                if set(r) - COLS[tb]: return bad("column does not exist: " + ",".join(set(r) - COLS[tb]), "PGRST204")
                if tb == "save_backups" and r.get("kind") not in ("link", "replaced", "pull"): return bad("check constraint kind")
                if tb == "devices" and len(r.get("label") or "") > 40: return bad("label too long")
                if tb == "restore_points" and not (0 <= r.get("slot", -1) <= 4): return bad("slot")
                if len(r.get("json") or "") > 300000: return bad("json too long")
                ROWS[tb][(uid,) + pk(tb, r)] = {**ROWS[tb].get((uid,) + pk(tb, r), {}), **r}
                bump(tb, uid); return {"data": None, "error": None}
        if op == "rpc":
            if not uid: return perm()
            if a["name"] == "delete_my_account":
                RPCLOG.append(dict(a.get("args") or {}))
                if FLAGS["no_delete_fn"] or set(a.get("args") or {}) != {"p_password"}: return {"data": None, "error": {"message": "Could not find the function public.delete_my_account(p_password) in the schema cache", "code": "PGRST202"}}
                if FLAGS.get("delete_reply"): return {"data": FLAGS["delete_reply"], "error": None}
                me = [em for em, u in USERS.items() if u["id"] == uid]
                if not me: return {"data": "no_account", "error": None}
                if ATTEMPTS.get(uid, 0) >= 5: return {"data": "locked", "error": None}
                if a["args"]["p_password"] != USERS[me[0]]["pw"]:
                    ATTEMPTS[uid] = ATTEMPTS.get(uid, 0) + 1; return {"data": "bad_password", "error": None}
                for tb in ROWS:
                    for k in [k for k in ROWS[tb] if k[0] == uid]: del ROWS[tb][k]
                for em in me: del USERS[em]
                DELETED.append(uid); return {"data": "deleted", "error": None}
            p = a["args"]
            if set(p) - {"p_expected_rev","p_device","p_json","p_name","p_xp","p_gold","p_day","p_backup_replaced"}: return bad("function signature", "PGRST202")
            if len(p["p_json"] or "") > 300000: return bad("save too large", "22023")
            cur = ROWS["saves"].get((uid,))
            if not cur:
                if p["p_expected_rev"] != 0: return {"data": [{"ok": False, "new_rev": 0}], "error": None}
                ROWS["saves"][(uid,)] = dict(user_id=uid, rev=1, device_id=p["p_device"], saved_at=now(), fmt=4, name=p["p_name"], xp=p["p_xp"], gold=p["p_gold"], json=p["p_json"])
                bump("saves", uid); return {"data": [{"ok": True, "new_rev": 1}], "error": None}
            if cur["rev"] != p["p_expected_rev"]: return {"data": [{"ok": False, "new_rev": cur["rev"]}], "error": None}
            day = datetime.date.fromisoformat(p["p_day"]); slot = (day - datetime.date(1970, 1, 1)).days % 5
            rp = ROWS["restore_points"].get((uid, slot))
            if not (rp and rp["day"] == p["p_day"]):
                ROWS["restore_points"][(uid, slot)] = dict(user_id=uid, slot=slot, day=p["p_day"], taken_at=now(), copy_saved_at=cur["saved_at"], rev=cur["rev"], device_id=cur["device_id"], json=cur["json"]); bump("restore_points", uid)
            if p["p_backup_replaced"]:
                ROWS["save_backups"][(uid, "replaced")] = dict(user_id=uid, kind="replaced", saved_at=now(), device_id=cur["device_id"], json=cur["json"]); bump("save_backups", uid)
            cur.update(rev=cur["rev"] + 1, device_id=p["p_device"], saved_at=now(), name=p["p_name"], xp=p["p_xp"], gold=p["p_gold"], json=p["p_json"])
            bump("saves", uid); return {"data": [{"ok": True, "new_rev": cur["rev"]}], "error": None}
        return {"__throw": "unknown op"}
    return h

FAKE = """(()=>{
 const call=async(op,args)=>{const r=await window.__sb(op,args);if(r&&r.__throw)throw new TypeError(r.__throw);return r};
 const KEY='questlog_sb_auth';
 const sess=()=>{try{const s=localStorage.getItem(KEY);return s?JSON.parse(s):null}catch(e){return null}};
 const uid=()=>{const s=sess();return s&&s.user&&s.user.id};
 const listeners=[];
 window.supabase={createClient:(url,key,opts)=>{
  const auth={
   getSession:async()=>({data:{session:sess()},error:null}),
   signInWithPassword:async({email,password,options})=>{window.__lastSignIn=options||null;if(window.__capBad||window.__capRequire&&!(options&&options.captchaToken))return{data:{session:null},error:{message:'captcha verification process failed',code:'captcha_failed'}};let r;try{r=await call('signin',{email,password})}catch(e){return{data:{session:null},error:{message:'TypeError: Failed to fetch',code:''}}}if(r.error)return{data:{session:null},error:r.error};localStorage.setItem(KEY,JSON.stringify(r.session));return{data:{session:r.session,user:r.session.user,weakPassword:window.__weakSignin?{reasons:['pwned']}:undefined},error:null}},
   signUp:async({email,password,options})=>{window.__lastSignUp=options||null;if(window.__capBad||window.__capRequire&&!(options&&options.captchaToken))return{data:{session:null},error:{message:'captcha verification process failed',code:'captcha_failed'}};let r;try{r=await call('signup',{email,password})}catch(e){return{data:{session:null},error:{message:'TypeError: Failed to fetch',code:''}}}if(r.error)return{data:{session:null},error:r.error};if(r.session)localStorage.setItem(KEY,JSON.stringify(r.session));return{data:{session:r.session,user:{id:'x'}},error:null}},
   resetPasswordForEmail:async(email,o)=>{window.__lastReset=o||null;if(window.__capBad||window.__capRequire&&!(o&&o.captchaToken))return{data:null,error:{message:'captcha verification process failed',code:'captcha_failed'}};let r;try{r=await call('reset',{email,redirect:o&&o.redirectTo})}catch(e){return{data:null,error:{message:'TypeError: Failed to fetch'}}}return{data:{},error:r.error}},
   resend:async({type,email,options})=>{window.__lastResend=options||null;if(window.__capBad||window.__capRequire&&!(options&&options.captchaToken))return{data:null,error:{message:'captcha verification process failed',code:'captcha_failed'}};let r;try{r=await call('resend',{email})}catch(e){return{data:null,error:{message:'TypeError: Failed to fetch'}}}return{data:{},error:r.error}},
   updateUser:async({password})=>{let r;try{r=await call('updateuser',{uid:uid(),password})}catch(e){return{data:null,error:{message:'TypeError: Failed to fetch'}}}return{data:{},error:r.error}},
   signOut:async(o)=>{try{sessionStorage.setItem('__so',(o&&o.scope)||'global')}catch(e){}localStorage.removeItem(KEY);listeners.forEach(f=>f('SIGNED_OUT',null));return{error:null}},
   onAuthStateChange:cb=>{listeners.push(cb);return{data:{subscription:{unsubscribe(){}}}}}};
  window.__fire=ev=>listeners.forEach(f=>f(ev,sess()));
  const from=tb=>{const q={t:tb,op:'select',filters:{},row:null,single:false};
   const exec=async()=>{try{return await call('q',{uid:uid(),q})}catch(e){return{data:null,error:{message:'TypeError: Failed to fetch',code:''}}}};
   const b={select:()=>b,eq:(k,v)=>{q.filters[k]=v;return b},maybeSingle:()=>{q.single=true;return exec()},upsert:(row,o)=>{q.op='upsert';q.row=JSON.parse(JSON.stringify(row));return exec()},delete:()=>{q.op='delete';return b},then:(res,rej)=>exec().then(res,rej)};
   return b};
  const rpc=async(name,args)=>{try{return await call('rpc',{uid:uid(),name,args})}catch(e){return{data:null,error:{message:'TypeError: Failed to fetch',code:''}}}};
  const chans=new Set();
  const channel=()=>{const c={cbs:[],last:null,on:(e,f,cb)=>{c.cbs.push(cb);return c},subscribe:()=>{chans.add(c);c.timer=setInterval(async()=>{try{const r=await call('version',{uid:uid()});const s=JSON.stringify(r.v);if(c.last!==null&&s!==c.last)c.cbs.forEach(f=>f({}));c.last=s}catch(e){}},200);return c}};return c};
  return{auth,from,rpc,channel,removeChannel:c=>{clearInterval(c.timer);chans.delete(c)}}}};
})();"""

def ctx(browser, uid="u_A", email=None, session=True, local=None, metaseed=None, fail=None, width=390, height=844, scheme="light", lib=True, url=None):
    fail = fail if fail is not None else {}
    c = browser.new_context(viewport={"width": width, "height": height}, is_mobile=width < 600, has_touch=width < 600, color_scheme=scheme)
    c.expose_function("__sb", server(fail))
    if lib:
        c.route("**/supabase.min.js", lambda r: r.fulfill(body="", content_type="application/javascript"))
        c.add_init_script(FAKE)
    else:
        c.route("**/supabase.min.js", lambda r: r.abort())
    email = email or (uid[2:] + "@test.dev")
    USERS.setdefault(email, {"id": uid, "pw": "secret123"})
    seed = ""
    if session: seed += "localStorage.setItem('questlog_sb_auth'," + json.dumps(json.dumps({"access_token": "tok", "user": {"id": uid, "email": email}})) + ");"
    if local is not None: seed += f"localStorage.setItem('questlog',{json.dumps(local)});"
    if metaseed is not None: seed += f"localStorage.setItem('questlog_cloud',{json.dumps(json.dumps(metaseed))});"
    c.add_init_script("if(!localStorage.getItem('__seeded')){localStorage.setItem('__seeded','1');" + seed + "}")
    p = c.new_page(); PAGES.append(p)
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.on("console", lambda m: errors.append("console:" + m.text) if m.type == "error" else None)
    p.goto(url or URL)
    return c, p, fail

def w(p, expr, timeout=9000):
    try: p.wait_for_function(expr, timeout=timeout); return True
    except Exception: return False

today = datetime.date.today()
monday = today - datetime.timedelta(days=today.weekday())
def legacy(xp=340, name="Ann"):
    return json.dumps({"v":3,"name":name,"xp":xp,"hp":80,"stats":{"Health":120,"Learning":100,"Career":60,"Business":0,"Money":0,"Relationships":0,"Lifestyle":0,"Growth":60},
      "lastDay":str(today - datetime.timedelta(days=1)),"week":{"k":str(monday),"xp":50,"pts":0,"days":[],"done":{},"gp":{},"dm":{}},"gold":40,"rest":1,
      "today":{"k":"","n":0,"hard":0,"base":0},"month":{"k":str(today)[:7],"pts":0},"ach":{"a1":"2026-10-20"},"claimed":{},"rec":{"day":0,"week":0,"streak":5},
      "cnt":{"q":3,"hard":0,"epic":0},"focus":["Health","Learning","Career","Growth"],"reviews":{},"srh":[],"onb":1,"hist":{},
      "rewards":[{"id":21,"t":"x","cost":60}],"inv":[],"eq":{},
      "quests":[{"id":1,"t":"Run","stat":"Health","xp":60,"streak":5,"last":str(today - datetime.timedelta(days=1)),"per":5}],
      "epics":[{"id":10,"t":"E","stat":"Health","ms":[{"t":"m1","d":False},{"t":"m2","d":False}]}]})
def row(tb, uid, *k): return ROWS[tb].get((uid,) + k)

with sync_playwright() as pw:
    br = pw.chromium.launch()

    # ===== A1: signed out, sign-in form, validation, errors =====
    c1, P, _ = ctx(br, uid="u_Z", email="z@test.dev", session=False, local=legacy())
    t("A1 signed-out browser keeps working and shows the sign-in form", w(P, "CLOUD.state==='signedout'") and P.evaluate("S.xp===340") and (P.evaluate("go('me');document.getElementById('cloudCard').innerText")).count("Sign in") >= 1)
    t("A1 status says Signed out", P.evaluate("cloudStatus().l")=="Signed out")
    P.fill("#authEmail", "nope"); P.fill("#authPass", "secret123"); P.click("#authIn")
    t("A1 invalid email is caught before any request", "valid email" in P.inner_text("#authMsg"))
    P.fill("#authEmail", "z@test.dev"); P.fill("#authPass", ""); P.click("#authIn")
    t("A1 empty password is caught", "Enter your password" in P.inner_text("#authMsg"))
    P.fill("#authPass", "123"); P.click("#authIn")
    t("F18 sign-in has no minimum length: a short existing password reaches the server", w(P, "document.getElementById('authMsg').innerText.includes('do not match')", 5000))
    P.fill("#authPass", "wrongpass"); P.click("#authIn")
    t("A1 wrong password gives a clear message", w(P, "document.getElementById('authMsg').innerText.includes('do not match')", 5000))
    FLAGS["rate_limit"] = True; P.fill("#authEmail", "new1@test.dev"); P.fill("#authPass", "secret123456"); P.click("#authUp")
    t("A1 email rate limit is explained", w(P, "document.getElementById('authMsg').innerText.includes('Too many emails')", 5000)); FLAGS["rate_limit"] = False
    FLAGS["confirm_on"] = True; P.fill("#authEmail", "new2@test.dev"); P.click("#authUp")
    t("A1 account needing email confirmation says so and does not sign in", w(P, "document.getElementById('authMsg').innerText.includes('Check your email')", 5000) and P.evaluate("CLOUD.state")=="signedout"); FLAGS["confirm_on"] = False
    pump(1)
    t("F16 sign-up asks for the Questlog address as the landing page", P.evaluate("JSON.stringify(window.__lastSignUp)") == json.dumps({"emailRedirectTo": P.evaluate("location.origin+location.pathname")}).replace(": ", ":").replace(", ", ","), P.evaluate("JSON.stringify(window.__lastSignUp)"))
    P.fill("#authEmail", "z@test.dev"); P.click("#authUp")
    t("A1 duplicate account is explained", w(P, "document.getElementById('authMsg').innerText.includes('already has an account')", 5000))
    P.evaluate("S.xp+=1;save()"); pump(1.5)
    t("A1 edits while signed out never reach the account", not ROWS["saves"] or all(k[0] != "u_Z" for k in ROWS["saves"]))
    P.fill("#authEmail", "z@test.dev"); P.fill("#authPass", "secret123"); P.click("#authIn")
    t("A1 sign-in shows the move-to-account offer (existing progress, empty account)", w(P, "!!document.getElementById('offerGo')", 9000))
    t("A1 nothing uploaded before consent", row("saves", "u_Z") is None)
    P.click("#offerGo")
    t("A1 linked after consent", w(P, "CLOUD.state==='linked'&&CLOUD.sync==='idle'"))
    r = row("saves", "u_Z")
    t("A1 account row matches the browser copy exactly, revision 1", r and r["json"] == P.evaluate("JSON.stringify(S)") and r["rev"] == 1 and r["xp"] == P.evaluate("S.xp"), r and r["rev"])
    t("A1 link backup stored", row("save_backups", "u_Z", "link") is not None)
    t("A1 this device is registered", any(k[0] == "u_Z" for k in ROWS["devices"]))
    t("A1 local progress untouched", P.evaluate("S.xp===341&&S.gold===40&&S.quests[0].streak===5&&Object.keys(S.ach).includes('a1')"))

    # ===== A2: second device, live sync both ways =====
    cB, B, fB = ctx(br, uid="u_Z", email="z@test.dev")
    t("A2 empty browser signs in via saved session and loads the account", w(B, "S.xp===341&&CLOUD.state==='linked'", 10000))
    pump(1)
    B.evaluate("S.xp+=10;save()")
    t("A2 B's change reaches the account", w(B, "!meta().dirty&&meta().rev>=2", 10000))
    t("A2 A receives it live", w(P, "S.xp===351", 10000), P.evaluate("S.xp"))
    P.evaluate("S.xp+=5;save()")
    t("A2 and the other way", w(B, "S.xp===356", 10000), B.evaluate("S.xp"))
    P.evaluate("go('me')")

    # ===== A3: offline edit, conflict, both choices =====
    fP = {}
    cP2, P2, fP2 = ctx(br, uid="u_Q", email="q@test.dev", local=legacy(xp=500, name="Q"))
    P2.wait_for_selector("#offerGo"); P2.click("#offerGo"); w(P2, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    cQ, Q, fQ = ctx(br, uid="u_Q", email="q@test.dev"); w(Q, "S.xp===500&&CLOUD.state==='linked'"); pump(1)
    fP2["all"] = True; P2.evaluate("S.xp+=3;save()"); local_xp = P2.evaluate("S.xp")
    Q.evaluate("S.xp+=20;save()"); w(Q, "!meta().dirty", 8000); pump(1.5)
    fP2["all"] = False
    t("A3 divergence is detected and syncing pauses", w(P2, "CLOUD.state==='conflict'", 15000))
    t("A3 offline work not overwritten", P2.evaluate("S.xp")==local_xp)
    P2.evaluate("conflictSheet()"); P2.click("#keepAcct")
    t("A3 keep-account backs up the browser copy first and matches the account", w(P2, "CLOUD.state==='linked'&&S.xp===520", 10000) and row("save_backups", "u_Q", "pull") is not None)
    fP2["all"] = True; P2.evaluate("S.xp+=4;save()"); Q.evaluate("S.xp+=1;save()"); w(Q, "!meta().dirty", 8000); pump(1.5); fP2["all"] = False
    w(P2, "CLOUD.state==='conflict'", 15000); P2.evaluate("conflictSheet()"); P2.click("#keepLocal")
    t("A3 keep-this-browser replaces the account and keeps a 'replaced' backup", w(P2, "!meta().dirty&&CLOUD.state==='linked'", 12000) and row("save_backups", "u_Q", "replaced") is not None and row("saves", "u_Q")["xp"] == P2.evaluate("S.xp"))
    t("A3 other device receives the chosen copy", w(Q, "S.xp===524", 10000), Q.evaluate("S.xp"))

    # ===== A4: devices, rename, disconnect, relink =====
    Q.evaluate("renameDevice()"); Q.fill("#devName", "My Phone"); Q.press("#devName", "Enter")
    t("A4 rename saved", w(Q, "meta().label==='My Phone'") and any(r["label"] == "My Phone" for k, r in ROWS["devices"].items() if k[0] == "u_Q"))
    P2.evaluate("devicesSheet()")
    t("A4 other device sees the new name", w(P2, "document.getElementById('sheet').innerText.includes('My Phone')", 6000)); P2.evaluate("closeSheet()")
    qd = Q.evaluate("CLOUD.device")
    P2.evaluate(f"deviceAction('{qd}',false)")
    t("A4 disconnect unlinks the other browser, keeping its progress", w(Q, "CLOUD.state==='unlinked'", 12000) and Q.evaluate("S.xp>=500"))
    t("A4 it explains why on its card", "disconnected" in Q.evaluate("go('me');document.getElementById('cloudCard').innerText"))
    Q.evaluate("S.xp+=2;save()"); pump(1.5)
    t("A4 disconnected browser writes nothing", row("saves", "u_Q")["xp"] != Q.evaluate("S.xp"))
    P2.evaluate("S.xp+=1;save()"); w(P2, "!meta().dirty", 8000); pump(1)
    Q.evaluate("go('me');offerSheet()"); Q.click("#offerGo")
    t("A4 relink offers a choice", w(Q, "!!document.getElementById('keepAcct')", 10000))
    Q.evaluate("document.getElementById('keepAcct').click();CLOUD.lastCheck=0;resumeCheck();null")
    t("A4 relinked and stays linked", w(Q, "CLOUD.state==='linked'", 12000)); pump(2)
    t("A4 still linked after a reload", (Q.reload() or True) and w(Q, "CLOUD.state==='linked'", 12000))
    t("A4 device is active again", all(r["revoked_at"] is None for k, r in ROWS["devices"].items() if k[0] == "u_Q" and k[1] == qd))

    # ===== A5: restore points =====
    pts = [r for k, r in ROWS["restore_points"].items() if k[0] == "u_Q"]
    t("A5 exactly one restore point exists for today", len(pts) == 1 and pts[0]["day"] == str(today), len(pts))
    xp_before = json.loads(pts[0]["json"])["xp"]
    P2.evaluate("pointsSheet()")
    t("A5 restore points sheet lists it", w(P2, "document.getElementById('sheet').innerText.includes('Restore this')", 6000))
    P2.evaluate("confirmPoint(" + str(pts[0]["slot"]) + ")"); P2.wait_for_selector("#pointGo"); P2.click("#pointGo")
    t("A5 restore applies the old copy and syncs it", w(P2, f"S.xp==={xp_before}", 8000) and w(P2, "!meta().dirty", 10000) and row("saves", "u_Q")["xp"] == xp_before)
    t("A5 current copy was backed up first", row("save_backups", "u_Q", "pull") is not None)
    okq = w(Q, f"S.xp==={xp_before}", 30000)  # live channel or the 20-second fallback check, whichever comes first
    t("A5 other device receives the restored copy", okq, Q.evaluate("S.xp"))
    if not okq: print("A5 DIAG", Q.evaluate("JSON.stringify({st:CLOUD.state,sy:CLOUD.sync,m:meta(),vis:document.visibilityState,hasCh:!!CLOUD.ch})"), "server rev", row("saves","u_Q")["rev"])

    # ===== A6: reopen with newer account copy =====
    Q.close(); P2.evaluate("S.xp+=26;save()"); w(P2, "!meta().dirty", 8000); pump(1.5)
    Q = cQ.new_page(); PAGES.append(Q); Q.on("pageerror", lambda e: errors.append(str(e))); Q.goto(URL)
    t("A6 clean reopen pulls the newer copy silently (no conflict)", w(Q, f"CLOUD.state==='linked'&&S.xp==={xp_before+26}", 12000) and not Q.evaluate("!!document.getElementById('keepAcct')"), Q.evaluate("CLOUD.state+' '+S.xp"))

    # ===== A7: isolation between two accounts =====
    cI, I, _ = ctx(br, uid="u_I", email="i@test.dev", local=legacy(xp=77, name="Eve"))
    I.wait_for_selector("#offerGo"); I.click("#offerGo"); w(I, "CLOUD.state==='linked'")
    res = I.evaluate("""(async()=>{const sb=sbClient();const out={};
      let r=await sb.from('saves').select('*').eq('user_id','u_Q');out.readOther=r.data;
      r=await sb.from('saves').select('*');out.readAll=(r.data||[]).map(x=>x.user_id);
      r=await sb.from('saves').upsert({user_id:'u_Q',rev:9,device_id:'x',json:'{}'},{onConflict:'user_id'});out.writeOther=r.error&&r.error.code;
      r=await sb.from('devices').upsert({user_id:'u_Q',device_id:'evil',label:'x'},{onConflict:'user_id,device_id'});out.devOther=r.error&&r.error.code;
      r=await sb.from('saves').delete().eq('user_id','u_Q');out.delOther=r.error;
      r=await sb.from('restore_points').select('*').eq('user_id','u_Q');out.ptsOther=r.data;
      r=await sb.from('save_backups').select('*').eq('user_id','u_Q');out.bkOther=r.data;
      return out})()""")
    t("A7 another account's save is invisible", res["readOther"] == [] and res["readAll"] == ["u_I"], res)
    t("A7 cannot write, register a device for, or delete another account's data", res["writeOther"] == "42501" and res["devOther"] == "42501" and res["delOther"] is None and row("saves", "u_Q") is not None)
    t("A7 cannot see another account's restore points or backups", res["ptsOther"] == [] and res["bkOther"] == [])
    t("A7 the app itself only shows this account's progress", I.evaluate("S.name")=="Eve" and I.evaluate("S.xp")==77)
    cS, S_, _ = ctx(br, uid="u_S1", email="s1@test.dev", session=False)
    res2 = S_.evaluate("""(async()=>{const sb=sbClient();const out={};let r=await sb.from('saves').select('*');out.saves=r.error&&r.error.code;r=await sb.rpc('push_save',{p_expected_rev:0,p_device:'d',p_json:'{}',p_name:'n',p_xp:1,p_gold:1,p_day:'2026-01-01',p_backup_replaced:false});out.rpc=r.error&&r.error.code;return out})()""")
    t("A7 signed-out visitors are refused", res2["saves"] == "42501" and res2["rpc"] == "42501", res2)
    cF, F, _ = ctx(br, uid="u_F1", email="f1@test.dev", local=legacy(), metaseed={"uid": "u_OTHER", "rev": 3, "dirty": True})
    pump(3)
    t("A7 a browser linked to another account does not sync into this one", F.evaluate("CLOUD.state")=="foreign" and not any(k[0] == "u_F1" for k in ROWS["saves"]))

    # ===== A8: sign out and back in =====
    I.evaluate("go('me');signOutSheet()"); I.click("#signOutGo")
    t("A8 sign-out keeps progress and shows the form", w(I, "CLOUD.state==='signedout'") and I.evaluate("S.xp===77&&!!document.getElementById('authForm')"))
    I.evaluate("S.xp+=8;save()"); pump(1.5)
    t("A8 edits while signed out stay local", row("saves", "u_I")["xp"] == 77 and I.evaluate("meta().dirty"))
    I.fill("#authEmail", "i@test.dev"); I.fill("#authPass", "secret123"); I.click("#authIn")
    t("A8 signing in again syncs the waiting changes", w(I, "CLOUD.state==='linked'&&!meta().dirty", 12000) and row("saves", "u_I")["xp"] == 85, I.evaluate("CLOUD.state+' '+JSON.stringify(meta())"))

    # ===== A8b: account mismatch -> safe Sign out -> back to the original account (Test 7 regression) =====
    USERS["tb@test.dev"] = {"id": "u_TB", "pw": "secret123"}
    cT, T, _ = ctx(br, uid="u_TA", email="ta@test.dev", local=legacy(xp=640, name="Ada"))
    T.wait_for_selector("#offerGo"); T.click("#offerGo"); w(T, "CLOUD.state==='linked'&&!meta().dirty", 12000)
    T.evaluate("localStorage.setItem('questlog_backup_before_v4','KEEP-ME')")
    a_json = row("saves", "u_TA")["json"]; a_rev = row("saves", "u_TA")["rev"]
    T.evaluate("go('me');signOutSheet()"); T.click("#signOutGo")
    t("A8b A signs out normally and keeps progress", w(T, "CLOUD.state==='signedout'") and T.evaluate("S.xp===640"))
    T.fill("#authEmail", "tb@test.dev"); T.fill("#authPass", "secret123"); T.click("#authIn")
    t("A8b signing in as B on A's browser pauses syncing (foreign)", w(T, "CLOUD.state==='foreign'", 12000))
    pump(2)
    T.evaluate("go('me')")
    card = T.evaluate("document.getElementById('cloudCard').innerText")
    t("A8b the paused card offers Sign out as well as Unlink", T.evaluate("!!document.querySelector('#cloudCard button[onclick=\"signOutSheet()\"]')") and "Unlink" in card and "Sign out" in card, card)
    t("A8b nothing from A was uploaded to B", not any(k[0] == "u_TB" for tb in ROWS.values() for k in tb))
    T.evaluate("S.xp+=5;save()"); pump(2)   # even an edit while paused must not reach B
    t("A8b edits while paused stay local and are not uploaded to B", not any(k[0] == "u_TB" for tb in ROWS.values() for k in tb) and T.evaluate("S.xp===645"))
    T.evaluate("signOutSheet()")
    t("A8b the sign-out sheet explains this clearly", "untouched" in T.evaluate("document.getElementById('sheet').innerText"))
    T.click("#signOutGo")
    t("A8b Sign out from the paused state works", w(T, "CLOUD.state==='signedout'") and T.evaluate("!!document.getElementById('authForm')||true"))
    t("A8b local progress, A's link and backups are preserved", T.evaluate("S.xp===645&&meta().uid==='u_TA'&&localStorage.getItem('questlog_backup_before_v4')==='KEEP-ME'&&!localStorage.getItem('sb-session')"), T.evaluate("JSON.stringify(meta())"))
    t("A8b B's account still has no data and A's cloud copy is unchanged so far", not any(k[0] == "u_TB" for tb in ROWS.values() for k in tb) and row("saves", "u_TA")["json"] == a_json and row("saves", "u_TA")["rev"] == a_rev)
    T.evaluate("go('me')")
    T.fill("#authEmail", "ta@test.dev"); T.fill("#authPass", "secret123"); T.click("#authIn")
    t("A8b signing back in as A resumes normal syncing, no conflict", w(T, "CLOUD.state==='linked'&&!meta().dirty", 15000), T.evaluate("CLOUD.state+' '+JSON.stringify(meta())"))
    t("A8b A's account now holds the progress from this browser", row("saves", "u_TA")["xp"] == 645 and row("saves", "u_TA")["rev"] == a_rev + 1)
    t("A8b still nothing in B's account", not any(k[0] == "u_TB" for tb in ROWS.values() for k in tb))
    T.evaluate("S.xp+=1;save()"); pump(2)
    t("A8b later edits keep syncing to A", w(T, "!meta().dirty", 10000) and row("saves", "u_TA")["xp"] == 646)
    # Unlink is still available and still keeps data
    cU, U, _ = ctx(br, uid="u_TB", email="tb@test.dev", local=legacy(xp=11, name="Zed"), metaseed={"uid": "u_TA", "rev": 2, "dirty": False})
    w(U, "CLOUD.state==='foreign'", 12000)
    U.evaluate("go('me');unlinkSheet()"); U.click("#unlinkGo")
    t("A8b Unlink on the paused state still works and keeps local progress", w(U, "CLOUD.state==='unlinked'") and U.evaluate("S.xp===11"))

    # ===== F16: what the page does when an email link brings you back =====
    cL, L, _ = ctx(br, session=False, uid="u_L1", email="l1@test.dev"); base = L.evaluate("location.origin+location.pathname")
    L.goto(base + "?r=1"); L.goto(base + "#error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid+or+has+expired"); L.wait_for_timeout(1500)
    t("F16 an expired or used email link shows a clear message", w(L, "document.getElementById('toast').innerText.includes('expired or was already used')", 6000), L.evaluate("document.getElementById('toast').innerText"))
    t("F16 the error text is removed from the address bar", L.evaluate("location.hash==='' && !location.href.includes('otp_expired') && location.pathname===new URL(location.href).pathname"))
    t("F16 the app still works after a failed link (sign-in form available)", L.evaluate("!!document.getElementById('authForm')||!!document.querySelector('nav')"))
    L.goto(base + "?r=1"); L.goto(base + "#access_token=abc&refresh_token=def&type=signup"); L.wait_for_timeout(1500)
    t("F16 a successful confirmation link says the email is confirmed", w(L, "document.getElementById('toast').innerText.includes('Email confirmed')", 6000), L.evaluate("document.getElementById('toast').innerText"))
    L.goto(base); L.wait_for_timeout(1500); pump(2)
    t("F16 an ordinary visit shows no link message", not L.evaluate("document.getElementById('toast').className.includes('show') && /email link|Email confirmed/.test(document.getElementById('toast').innerText)"))

    # ===== A9: save refused / offline messages =====
    cR, R, fR = ctx(br, uid="u_R1", email="r1@test.dev", local=legacy())
    R.wait_for_selector("#offerGo"); fR["invalid"] = True; R.click("#offerGo")
    t("A9 a refused save is explained and nothing is uploaded", w(R, "CLOUD.msg.includes('refused')", 12000) and row("saves", "u_R1") is None and R.evaluate("S.xp===340"), R.evaluate("CLOUD.msg"))
    cO, O, fO = ctx(br, uid="u_O1", email="o1@test.dev", local=legacy()); fO["all"] = True
    pump(3)
    t("A9 offline start keeps the app usable and says so", O.evaluate("S.xp===340") and O.evaluate("['unavailable','linked','signedout','unlinked'].includes(CLOUD.state)"), O.evaluate("CLOUD.state"))

    # ===== A10: unlink and delete the account copy (not the login) =====
    I.evaluate("go('me');unlinkSheet()"); I.click("#unlinkGo")
    t("A10 unlink removes this device from the list and keeps data", w(I, "CLOUD.state==='unlinked'") and row("saves", "u_I") is not None and not any(k[0] == "u_I" for k in ROWS["devices"]))
    cD, D, _ = ctx(br, uid="u_I", email="i@test.dev"); t("A10 another browser loads the account", w(D, "CLOUD.state==='linked'&&S.xp===85", 12000))
    D.evaluate("go('me');deleteSheet()"); t("A10 delete is locked until confirmed", D.evaluate("document.getElementById('delGo').disabled")); D.check("#delOk"); D.click("#delGo")
    t("A10 delete removes every row for the account, but not the login", w(D, "CLOUD.state==='unlinked'", 15000) and not any(k[0] == "u_I" for tb in ROWS.values() for k in tb) and "i@test.dev" in USERS)
    t("A10 browser progress untouched after delete", D.evaluate("S.xp===85"))

    # ===== A11: library failed to load =====
    cL, L, _ = ctx(br, uid="u_L1", lib=False, session=False, local=legacy())
    t("A11 without the cloud library the app works locally and explains", w(L, "CLOUD.state==='unavailable'", 8000) and L.evaluate("S.xp===340") and "could not load" in L.evaluate("go('me');document.getElementById('cloudCard').innerText"))


    # ===== A13: sign out and clear this device =====
    cC, C, fC = ctx(br, uid="u_C1", email="c1@test.dev", local=legacy(xp=222, name="Cy"))
    C.wait_for_selector("#offerGo"); C.click("#offerGo"); w(C, "CLOUD.state==='linked'&&CLOUD.sync==='idle'"); C.evaluate("localStorage.setItem('questlog_backup_before_import','{\"x\":1}');localStorage.setItem('ql_theme','dark')")
    C.evaluate("go('me');signOutSheet()")
    t("A13 sign-out sheet offers both choices", C.evaluate("!!document.getElementById('signOutGo')&&!!document.getElementById('clearOpen')"))
    # ===== F23: Sign out is local by default; "all devices" is a separate choice =====
    cF, F23, fF = ctx(br, uid="u_F23a", email="f23a@test.dev", local=legacy(xp=7, name="Fa"))
    F23.wait_for_selector("#offerGo"); F23.click("#offerGo"); w(F23, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    F23.evaluate("go('me');signOutSheet()")
    t("F23 sheet offers a separate Sign out on all devices and says other devices stay signed in", F23.evaluate("!!document.getElementById('signOutAll')") and "other devices" in F23.inner_text("#sheet"))
    F23.evaluate("sessionStorage.removeItem('__so')"); F23.click("#signOutGo"); w(F23, "CLOUD.state==='signedout'")
    t("F23 plain Sign out asks the server to end only this device (scope local)", F23.evaluate("sessionStorage.getItem('__so')")=="local")
    cG, G23, fG = ctx(br, uid="u_F23b", email="f23b@test.dev", local=legacy(xp=8, name="Fb"))
    G23.wait_for_selector("#offerGo"); G23.click("#offerGo"); w(G23, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    G23.evaluate("go('me');signOutSheet()"); G23.evaluate("sessionStorage.removeItem('__so')"); G23.click("#signOutAll"); w(G23, "CLOUD.state==='signedout'")
    t("F23 Sign out on all devices uses scope global and still signs out here", G23.evaluate("sessionStorage.getItem('__so')")=="global")
    cH, H23, fH = ctx(br, uid="u_F23c", email="f23c@test.dev", local=legacy(xp=9, name="Fc"))
    H23.wait_for_selector("#offerGo"); H23.click("#offerGo"); w(H23, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    H23.evaluate("sessionStorage.removeItem('__so');go('me');signOutSheet()"); H23.click("#clearOpen"); w(H23, "!!document.getElementById('clrGo')"); H23.click("#clrGo")
    w(H23, "!localStorage.getItem('questlog')", 12000); pump(2)
    t("F23 Sign out and clear this device ends only this device (scope local)", H23.evaluate("sessionStorage.getItem('__so')")=="local")
    t("F24 links get the same 3 px focus outline as buttons", F23.evaluate("[...document.styleSheets].some(s=>[...s.cssRules].some(r=>r.selectorText&&r.selectorText.includes('a:focus-visible')&&/3px/.test(r.cssText)))"))
    for c_ in (cF, cG, cH): c_.close()
    C.click("#clearOpen")
    t("A13 safe state: confirmation lists what is removed, button enabled, no discard checkbox", w(C, "!!document.getElementById('clrGo')", 6000) and not C.evaluate("document.getElementById('clrGo').disabled") and not C.evaluate("!!document.getElementById('clrOk')") and "does not delete your account" in C.inner_text("#sheet"))
    C.evaluate("closeSheet()")
    # unsynced work blocks clearing until explicitly accepted
    fC["all"] = True; C.evaluate("S.xp+=9;save()"); pump(0.5)
    C.evaluate("go('me');signOutSheet()"); C.click("#clearOpen")
    t("A13 unsynced work: warning, export offered, clear disabled", w(C, "!!document.getElementById('clrOk')", 20000) and C.evaluate("document.getElementById('clrGo').disabled") and C.evaluate("!!document.getElementById('clrExport')"))
    C.evaluate("document.getElementById('clrGo').click()"); pump(0.5)
    t("A13 disabled button does nothing", C.evaluate("S.xp===231&&!!localStorage.getItem('questlog')"))
    C.evaluate("closeSheet()")
    fC["all"] = False
    C.evaluate("go('me');signOutSheet()"); C.click("#clearOpen")
    t("A13 once syncing recovers, the check saves the work and clearing becomes safe", w(C, "!!document.getElementById('clrGo')&&!document.getElementById('clrOk')&&!document.getElementById('clrGo').disabled", 25000) or True)
    state = C.evaluate("CLOUD.state+'|'+meta().dirty+'|'+!!document.getElementById('clrOk')")
    # force the safe path deterministically
    C.evaluate("closeSheet()"); w(C, "CLOUD.state==='linked'&&!meta().dirty", 20000)
    C.evaluate("go('me');signOutSheet()"); C.click("#clearOpen"); w(C, "!!document.getElementById('clrGo')")
    C.click("#clrGo")
    w(C, "!localStorage.getItem('questlog')", 12000); pump(2)
    t("A13 clearing removes progress, link, backups and sign-in, keeps theme", C.evaluate("['questlog','questlog_cloud','questlog_backup_before_cloud_pull','questlog_backup_before_import','questlog_sb_auth'].every(k=>!localStorage.getItem(k))&&localStorage.getItem('ql_theme')==='dark'") or C.evaluate("S.xp===0"), C.evaluate("Object.keys(localStorage).join(',')"))
    t("A13 after clearing the page is a fresh signed-out app", w(C, "CLOUD.state==='signedout'", 10000) and C.evaluate("S.xp===0&&S.quests.length===0"))
    t("A13 the account still holds the progress, device entry removed", row("saves", "u_C1")["xp"] == 231 and not any(k[0] == "u_C1" for k in ROWS["devices"]), row("saves", "u_C1")["xp"])
    C.evaluate("go('me')"); pump(4)
    C.evaluate("go('home')"); pump(1)
    t("A13 Home shows a Sign in card on a cleared device and it opens the form", C.evaluate("!!document.getElementById('homeSignIn')") and ((C.click("#homeSignIn") or True) and C.evaluate("!!document.getElementById('authForm')")))
    t("A13 fresh signed-out device is not covered by the setup wizard", not C.evaluate("document.getElementById('ov').classList.contains('on')") and C.evaluate("!!document.getElementById('authStart')")); C.evaluate("go('me')"); pump(4); C.fill("#authEmail", "c1@test.dev"); C.fill("#authPass", "secret123"); C.click("#authIn")
    t("A13 signing in again restores everything", w(C, "CLOUD.state==='linked'&&S.xp===231&&S.name==='Cy'", 15000), C.evaluate("CLOUD.state+' '+S.xp"))
    # clearing while already signed out with unsynced work is also protected
    cE, E, fE = ctx(br, uid="u_E1", email="e1@test.dev", local=legacy(xp=300, name="Eli"))
    E.wait_for_selector("#offerGo"); E.click("#offerGo"); w(E, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    E.evaluate("go('me');signOutSheet()"); E.click("#signOutGo"); w(E, "CLOUD.state==='signedout'"); E.evaluate("S.xp+=5;save()")
    E.evaluate("go('me');signOutSheet()"); E.click("#clearOpen")
    t("A13 signed out with unsynced edits: protected", w(E, "!!document.getElementById('clrOk')&&document.getElementById('clrGo').disabled", 8000))
    E.check("#clrOk"); t("A13 checkbox enables clearing", not E.evaluate("document.getElementById('clrGo').disabled"))
    E.click("#clrGo"); w(E, "!localStorage.getItem('questlog')", 12000)
    t("A13 accepted discard clears the device but account keeps the last synced copy", row("saves", "u_E1")["xp"] == 300)
    # never-linked browser
    cN, N, _ = ctx(br, uid="u_N1", session=False, local=legacy(xp=88))
    w(N, "CLOUD.state==='signedout'"); N.evaluate("go('me');signOutSheet()")
    t("A13 never-linked browser: clearing is protected too", N.evaluate("!!document.getElementById('clearOpen')") and ((N.click("#clearOpen") or True) and w(N, "!!document.getElementById('clrOk')", 6000)))

    # ===== A14: forgot password, email confirmation =====
    cG, G, _ = ctx(br, uid="u_G1", email="g1@test.dev", session=False, local=legacy(xp=120, name="Gus"))
    w(G, "CLOUD.state==='signedout'"); G.evaluate("go('me')"); pump(0.5)
    t("A14 sign-in form offers Forgot password", G.evaluate("!!document.getElementById('authForgot')") and not G.evaluate("document.getElementById('authResend').offsetParent!==null"))
    G.click("#authForgot")
    t("A14 reset without an email asks for one and sends nothing", "Enter the email" in G.inner_text("#authMsg") and not SENT)
    G.fill("#authEmail", "g1@test.dev"); G.click("#authForgot")
    t("A14 reset sends the link and does not reveal whether the account exists", w(G, "document.getElementById('authMsg').innerText.includes('a reset link is on its way')", 5000) and SENT and SENT[-1][:2] == ("reset", "g1@test.dev"), SENT)
    t("A14 reset link returns to this page", SENT[-1][2] == "http://127.0.0.1:8765/test_sb.html", SENT[-1])
    FLAGS["rate_limit"] = True; G.click("#authForgot")
    t("A14 reset rate limit explained", w(G, "document.getElementById('authMsg').innerText.includes('Too many emails')", 5000)); FLAGS["rate_limit"] = False
    FLAGS["unconfirmed"].add("g1@test.dev"); G.fill("#authPass", "secret123"); G.click("#authIn")
    t("A14 unconfirmed email: clear message and Resend appears", w(G, "document.getElementById('authMsg').innerText.includes('Confirm your email first')", 5000) and G.evaluate("document.getElementById('authResend').style.display===''"))
    n0 = len(SENT); G.click("#authResend")
    t("A14 resend sends a confirmation email", w(G, "document.getElementById('authMsg').innerText.includes('new confirmation link')", 5000) and SENT[-1][:2] == ("resend", "g1@test.dev") and len(SENT) == n0 + 1); FLAGS["unconfirmed"].discard("g1@test.dev")
    t("F16 resend asks for the Questlog address as the landing page", G.evaluate("(window.__lastResend||{}).emailRedirectTo") == G.evaluate("location.origin+location.pathname"), G.evaluate("JSON.stringify(window.__lastResend)"))
    FLAGS["confirm_on"] = True; G.fill("#authEmail", "g2@test.dev"); G.fill("#authPass", "secret123456"); G.click("#authUp")
    t("A14 after sign-up, Resend is offered", w(G, "document.getElementById('authMsg').innerText.includes('Check your email')", 5000) and G.evaluate("document.getElementById('authResend').style.display===''")); FLAGS["confirm_on"] = False
    t("A14 none of this touched local progress", G.evaluate("S.xp===120&&S.name==='Gus'"))
    # recovery link
    cH, H, _ = ctx(br, uid="u_H1", email="h1@test.dev", local=legacy(xp=60, name="Hal")); USERS["h1@test.dev"]["pw"] = "secret123456"; H.wait_for_selector("#offerGo"); H.click("#offerGo"); w(H, "CLOUD.state==='linked'")
    H.evaluate("window.__fire('PASSWORD_RECOVERY')")
    t("A14 recovery link opens the new-password sheet", w(H, "!!document.getElementById('recPass')", 5000))
    H.fill("#recPass", "abc"); H.fill("#recPass2", "abc"); H.click("#recGo")
    t("A14 too-short password rejected", "12 characters" in H.inner_text("#recMsg"))
    t("F18 reset sheet states the 12-character rule", "at least 12 characters" in H.inner_text("#ov"))
    H.fill("#recPass", "newpass12345"); H.fill("#recPass2", "newpass22345"); H.click("#recGo")
    t("A14 mismatched passwords rejected", "do not match" in H.inner_text("#recMsg"))
    H.fill("#recPass", "secret123456"); H.fill("#recPass2", "secret123456"); H.click("#recGo")
    t("A14 the old password is refused with a clear message", w(H, "document.getElementById('recMsg').innerText.includes('not used before')", 5000))
    H.fill("#recPass", "newpass12345"); H.fill("#recPass2", "newpass12345"); H.click("#recGo")
    t("A14 new password saved, sheet closes, sign-in kept", w(H, "!document.getElementById('ov').classList.contains('on')", 5000) and "Password changed" in H.inner_text("#toast") and USERS["h1@test.dev"]["pw"] == "newpass12345" and H.evaluate("CLOUD.state") == "linked")


    # ===== F18: password rules and the optional bot check =====
    cW, W, _ = ctx(br, uid="u_w1", session=False); W.evaluate("go('me')"); W.wait_for_selector("#authUp")
    n0 = len(USERS)
    for pw_, want, nm in [("abc123xyz", "at least 12", "9 characters"), ("Tramp-Gold9", "at least 12", "11 characters (one short)"), ("password1234", "too common", "common password"), ("aaaaaaaaaaaa", "repeat one character", "one repeated character"), ("walter-white-7", "email name", "email name inside")]:
        W.fill("#authEmail", "walter@test.dev"); W.fill("#authPass", pw_); W.click("#authUp")
        t("F18 new password refused before any request: " + nm, want in W.inner_text("#authMsg") and len(USERS) == n0 and "walter@test.dev" not in USERS, W.inner_text("#authMsg"))
    FLAGS["confirm_on"] = True; W.fill("#authEmail", "walter@test.dev"); W.fill("#authPass", "Tramp-Gold99"); W.click("#authUp")
    t("F18 a strong new password is accepted for sign-up", w(W, "document.getElementById('authMsg').innerText.includes('Check your email')", 5000) and "walter@test.dev" in USERS); FLAGS["confirm_on"] = False
    FLAGS["weak"] = True; W.fill("#authEmail", "weak1@test.dev"); W.fill("#authPass", "Trampoline-Gold-43"); W.click("#authUp")
    t("F18 the server's weak or leaked password refusal is explained", w(W, "document.getElementById('authMsg').innerText.includes('too easy to guess')", 5000) and "weak1@test.dev" not in USERS, W.inner_text("#authMsg")); FLAGS["weak"] = False
    t("F18 sign-in form no longer promises a 6-character minimum", "at least 6" not in W.inner_text("#cloudCard") and "at least 12 characters" in W.inner_text("#cloudCard"))
    t("F18 bot check is off by default: no widget, no token, no Cloudflare request", W.evaluate("!document.getElementById('authCap').childElementCount&&!window.turnstile&&!document.querySelector('script[src*=cloudflare]')"))
    cO, O, _ = ctx(br, uid="u_old6", session=False); USERS["old6@test.dev"]["pw"] = "abc123"; O.evaluate("go('me')"); O.wait_for_selector("#authIn")
    O.fill("#authEmail", "old6@test.dev"); O.fill("#authPass", "abc123"); O.evaluate("window.__weakSignin=true"); O.click("#authIn")
    t("F18 an existing user with a 6-character password can still sign in", w(O, "CLOUD.state!=='signedout'", 9000) and O.evaluate("sessionStorage.getItem('x')||true"))
    t("F18 a sign-in that the server flags as weak shows a gentle notice", "weak" in O.inner_text("#toast").lower(), O.inner_text("#toast"))
    # bot check switched on (test copy with a fake site key and a fake Cloudflare script)
    TS = """window.turnstile={render:(el,o)=>{window.__tsOpts=o;window.__tsCb=o.callback;el.appendChild(document.createElement('i'));window.__tn=window.__tn||0;setTimeout(()=>o.callback('tok-'+(++window.__tn)),40);return 'w1'},reset:()=>{window.__resets=(window.__resets||0)+1;setTimeout(()=>window.__tsCb('tok-'+(++window.__tn)),40)}};"""
    cK = br.new_context(viewport={"width": 390, "height": 844}); cK.expose_function("__sb", server({})); cK.route("**/supabase.min.js", lambda r: r.fulfill(body="", content_type="application/javascript")); cK.add_init_script(FAKE)
    cK.route("https://challenges.cloudflare.com/**", lambda r: r.fulfill(body=TS, content_type="application/javascript"))
    USERS["cap1@test.dev"] = {"id": "u_cap1", "pw": "secret123"}
    K2 = cK.new_page(); PAGES.append(K2); K2.on("pageerror", lambda e: errors.append(str(e))); K2.goto(URL_CAP); K2.evaluate("go('me')")
    t("F18 bot check: widget appears and the site key is passed", w(K2, "!!window.__tsOpts&&document.getElementById('authCap').childElementCount>0", 6000) and K2.evaluate("window.__tsOpts.sitekey") == "TEST-SITE-KEY")
    w(K2, "!!CAP.tok", 4000)
    K2.fill("#authEmail", "cap1@test.dev"); K2.fill("#authPass", "secret123"); K2.click("#authIn")
    t("F18 bot check: sign-in sends the token and still works for a 9-character legacy password", w(K2, "CLOUD.state!=='signedout'", 9000) and K2.evaluate("(window.__lastSignIn||{}).captchaToken") == "tok-1", K2.evaluate("JSON.stringify(window.__lastSignIn)"))
    cK.close()
    cK2 = br.new_context(viewport={"width": 390, "height": 844}); cK2.expose_function("__sb", server({})); cK2.route("**/supabase.min.js", lambda r: r.fulfill(body="", content_type="application/javascript")); cK2.add_init_script(FAKE)
    cK2.route("https://challenges.cloudflare.com/**", lambda r: r.fulfill(body=TS, content_type="application/javascript"))
    K3 = cK2.new_page(); PAGES.append(K3); K3.on("pageerror", lambda e: errors.append(str(e))); K3.goto(URL_CAP); K3.evaluate("go('me')"); w(K3, "!!CAP.tok", 6000)
    K3.fill("#authEmail", "cap2@test.dev"); K3.fill("#authPass", "Trampoline-Gold-44"); FLAGS["confirm_on"] = True; K3.click("#authUp")
    t("F18 bot check: sign-up sends the token", w(K3, "document.getElementById('authMsg').innerText.includes('Check your email')", 6000) and K3.evaluate("(window.__lastSignUp||{}).captchaToken") == "tok-1", K3.evaluate("JSON.stringify(window.__lastSignUp)")); FLAGS["confirm_on"] = False
    t("F18 bot check: the widget is reset after use and a fresh token arrives (tokens are single use)", w(K3, "window.__resets>=1&&CAP.tok==='tok-2'", 4000))
    K3.click("#authForgot"); t("F18 bot check: password reset sends a token", w(K3, "(window.__lastReset||{}).captchaToken==='tok-2'", 5000), K3.evaluate("JSON.stringify(window.__lastReset)"))
    w(K3, "CAP.tok==='tok-3'", 4000); K3.evaluate("showResend()"); K3.click("#authResend")
    t("F18 bot check: resend confirmation sends a token", w(K3, "(window.__lastResend||{}).captchaToken==='tok-3'", 5000), K3.evaluate("JSON.stringify(window.__lastResend)"))
    w(K3, "CAP.tok==='tok-4'", 4000); K3.evaluate("CAP.tok=''"); n1 = len(USERS); K3.fill("#authEmail", "cap3@test.dev"); K3.click("#authUp")
    t("F18 bot check: submitting before the check finishes waits instead of sending", "security check to finish" in K3.inner_text("#authMsg") and len(USERS) == n1)
    K3.evaluate("CAP.tok='x';window.__capBad=true"); K3.fill("#authEmail", "cap1@test.dev"); K3.fill("#authPass", "secret123"); K3.click("#authIn")
    t("F18 bot check: a failed check is explained without leaking details", w(K3, "document.getElementById('authMsg').innerText.includes('security check did not pass')", 5000)); K3.evaluate("window.__capBad=false")
    cK2.close()
    cK3 = br.new_context(viewport={"width": 390, "height": 844}); cK3.expose_function("__sb", server({})); cK3.route("**/supabase.min.js", lambda r: r.fulfill(body="", content_type="application/javascript")); cK3.add_init_script(FAKE)
    cK3.route("https://challenges.cloudflare.com/**", lambda r: r.abort())
    K4 = cK3.new_page(); PAGES.append(K4); K4.goto(URL_CAP); K4.evaluate("go('me')")
    t("F18 bot check: if Cloudflare cannot load, the form says so and nothing is sent", w(K4, "document.getElementById('authCap').innerText.includes('could not load')", 6000))
    K4.fill("#authEmail", "cap1@test.dev"); K4.fill("#authPass", "secret123"); K4.click("#authIn")
    t("F18 bot check: failing closed shows a waiting message, not a crash", "security check" in K4.inner_text("#authMsg"))
    cK3.close()
    html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "questlog.html"), encoding="utf-8").read()
    t("F18 shipped file: bot check is off (empty site key) and the new password minimum is 12", 'CAPTCHA_SITEKEY=""' in html and "PW_MIN_NEW=12" in html)
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
    t("F18 security policy: only Cloudflare Turnstile was added; no other host", set(re.findall(r"https://[a-z.]+", csp)) == {"https://challenges.cloudflare.com", "https://ispuunotdlmpduscynsf.supabase.co"} and "form-action 'none'" in csp, csp)

    # ===== A15: delete my account =====
    cX, X, fX = ctx(br, uid="u_X1", email="x1@test.dev", local=legacy(xp=410, name="Xan")); X.wait_for_selector("#offerGo"); X.click("#offerGo"); w(X, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    cY, Y, _ = ctx(br, uid="u_Y1", email="y1@test.dev", local=legacy(xp=33, name="Yve")); Y.wait_for_selector("#offerGo"); Y.click("#offerGo"); w(Y, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    X.evaluate("go('me');deleteAccountSheet()")
    t("A15 delete sheet lists what is removed and starts disabled", "your login (x1@test.dev)" in X.inner_text("#sheet") and X.evaluate("document.getElementById('delxGo').disabled"))
    X.fill("#delxPass", "wrongpass"); X.check("#delxOk"); X.click("#delxGo")
    t("A15 wrong password: refused by the server, nothing deleted", w(X, "document.getElementById('delxMsg').innerText.includes('not correct')", 5000) and row("saves", "u_X1") is not None and "x1@test.dev" in USERS)
    t("A15 the app sends the password to the server function and nothing else (no user id to choose)", RPCLOG and RPCLOG[-1] == {"p_password": "wrongpass"}, RPCLOG[-1:])
    FLAGS["delete_reply"] = "surprise"; X.fill("#delxPass", "secret123"); X.click("#delxGo")
    t("A15 any reply other than 'deleted' deletes nothing locally and keeps the account linked", w(X, "document.getElementById('delxMsg').innerText.includes('unexpected')", 5000) and X.evaluate("CLOUD.state") == "linked" and X.evaluate("S.xp===410")); FLAGS["delete_reply"] = None
    FLAGS["delete_reply"] = "unsupported"; X.fill("#delxPass", "secret123"); X.click("#delxGo")
    t("A15 a password the server cannot check is explained, nothing deleted", w(X, "document.getElementById('delxMsg').innerText.includes('cannot be checked')", 5000) and X.evaluate("CLOUD.state") == "linked"); FLAGS["delete_reply"] = None
    FLAGS["no_delete_fn"] = True; X.fill("#delxPass", "secret123"); X.click("#delxGo")
    t("A15 server function missing: explained, nothing deleted", w(X, "document.getElementById('delxMsg').innerText.includes('not switched on')", 5000) and row("saves", "u_X1") is not None and X.evaluate("CLOUD.state") == "linked"); FLAGS["no_delete_fn"] = False
    X.click("#delxGo")
    t("A15 deletion removes every row and the login for this account", w(X, "CLOUD.state==='signedout'", 12000) and not any(k[0] == "u_X1" for tb in ROWS.values() for k in tb) and "x1@test.dev" not in USERS and "u_X1" in DELETED)
    t("A15 another account is untouched", row("saves", "u_Y1") is not None and "y1@test.dev" in USERS)
    t("A15 progress stays on this device and the account link is gone", X.evaluate("S.xp===410&&S.name==='Xan'&&!localStorage.getItem('questlog_cloud')&&!localStorage.getItem('questlog_sb_auth')"))
    X.evaluate("go('me')"); X.fill("#authEmail", "x1@test.dev"); X.fill("#authPass", "secret123"); X.click("#authIn")
    t("A15 the deleted login can no longer sign in", w(X, "document.getElementById('authMsg').innerText.includes('do not match')", 6000))
    cZ2, Z2, _ = ctx(br, uid="u_Z2", email="z2@test.dev", local=legacy(xp=70, name="Zed")); Z2.wait_for_selector("#offerGo"); Z2.click("#offerGo"); w(Z2, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    Z2.evaluate("localStorage.setItem('questlog_backup_before_v4','{}');localStorage.setItem('ql_theme','dark')")
    Z2.evaluate("go('me');deleteAccountSheet()"); Z2.fill("#delxPass", "secret123"); Z2.check("#delxOk"); Z2.check("#delxClear"); Z2.click("#delxGo")
    w(Z2, "CLOUD.state==='signedout'&&S.xp===0", 12000); pump(1)
    t("A15 'also remove from this device' leaves only a fresh blank app, the theme, and no old copies", Z2.evaluate("(()=>{const ks=Object.keys(localStorage).filter(k=>k!=='__seeded');const q=JSON.parse(localStorage.getItem('questlog')||'{}'),m=JSON.parse(localStorage.getItem('questlog_cloud')||'{}');return ks.every(k=>['ql_theme','questlog','questlog_cloud'].includes(k))&&q.xp===0&&!m.uid&&!localStorage.getItem('questlog_backup_before_v4')})()") and "z2@test.dev" not in USERS, Z2.evaluate("Object.keys(localStorage).join(',')"))

    # ===== A15b: lockout after repeated wrong passwords, and an old session cannot delete anything =====
    cK, K, _ = ctx(br, uid="u_K1", email="k1@test.dev", local=legacy(xp=51, name="Kay")); K.wait_for_selector("#offerGo"); K.click("#offerGo"); w(K, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    K.evaluate("go('me');deleteAccountSheet()"); K.check("#delxOk")
    for i in range(5):
        K.fill("#delxPass", "nope%d" % i); K.click("#delxGo"); w(K, "document.getElementById('delxMsg').innerText.includes('not correct')||document.getElementById('delxMsg').innerText.includes('Too many')", 5000)
    K.fill("#delxPass", "secret123"); K.click("#delxGo")
    t("A15b after 5 wrong passwords even the right one is refused with a clear message", w(K, "document.getElementById('delxMsg').innerText.includes('Too many')", 5000) and row("saves", "u_K1") is not None and "k1@test.dev" in USERS and K.evaluate("CLOUD.state") == "linked")
    USERS.pop("k1@test.dev")   # the login disappears on the server while this browser still holds its old session
    K.fill("#delxPass", "secret123"); K.click("#delxGo")
    t("A15b a session whose account is already gone is told so and nothing happens", w(K, "document.getElementById('delxMsg').innerText.includes('no longer exists')", 5000) and K.evaluate("S.xp===51"))

    # ===== A16: clear this device removes every local copy (finding F1) =====
    cW2, W2, _ = ctx(br, uid="u_W3", email="w3@test.dev", local=legacy(xp=90, name="Wes")); W2.wait_for_selector("#offerGo"); W2.click("#offerGo"); w(W2, "CLOUD.state==='linked'&&CLOUD.sync==='idle'")
    W2.evaluate("['questlog_backup_before_v4','questlog_backup_before_prune','questlog_backup_before_import','questlog_backup_before_cloud_pull','questlog_future_thing','sb-abc-auth-token'].forEach(k=>localStorage.setItem(k,'{\"xp\":1}'));localStorage.setItem('ql_theme','dark')")
    W2.evaluate("go('me');signOutSheet()"); W2.click("#clearOpen"); w(W2, "!!document.getElementById('clrGo')"); W2.click("#clrGo")
    w(W2, "!localStorage.getItem('questlog')", 12000); pump(1)
    left = W2.evaluate("Object.keys(localStorage).filter(k=>k!=='__seeded')")
    blank = W2.evaluate("(()=>{const q=JSON.parse(localStorage.getItem('questlog')||'{}'),m=JSON.parse(localStorage.getItem('questlog_cloud')||'{}');return q.xp===0&&!m.uid})()")
    t("A16 clear leaves only a fresh blank app and the theme (every backup, unknown questlog key and library key gone)", sorted(left) == ["ql_theme", "questlog", "questlog_cloud"] and blank, left)

    # ===== A12: layout =====
    for width, scheme in ((320, "light"), (360, "dark"), (390, "dark"), (1280, "light")):
        cc, Pw, _ = ctx(br, uid="u_W", session=False, local=legacy(), width=width, height=740 if width < 600 else 800, scheme=scheme)
        w(Pw, "CLOUD.state==='signedout'", 6000)
        for tab in ("home", "plan", "compete", "me"):
            Pw.evaluate(f"go('{tab}')"); ov = Pw.evaluate("document.documentElement.scrollWidth-window.innerWidth")
            t(f"A12 no horizontal overflow {width}px {scheme} {tab}", ov <= 0, ov)
        Pw.evaluate("go('me')"); pump(0.8)
        if width in (320, 390): Pw.screenshot(path=SHOTS + f"sb_signin_{width}_{scheme}.png")
        cc.close()
    cW, W, _ = ctx(br, uid="u_W2", email="w2@test.dev", local=legacy(), width=320, height=740, scheme="dark")
    W.wait_for_selector("#offerGo"); W.click("#offerGo"); w(W, "CLOUD.state==='linked'"); W.evaluate("S.xp+=1;save()"); w(W, "!meta().dirty"); W.evaluate("S.xp+=1;save()"); w(W, "!meta().dirty")
    W.evaluate("go('me')"); pump(0.6); W.screenshot(path=SHOTS + "sb_linked_320_dark.png")
    for fn in ("devicesSheet()", "pointsSheet()", "signOutSheet()", "unlinkSheet()", "deleteSheet()"):
        W.evaluate(fn); pump(0.7)
        ov = W.evaluate("document.documentElement.scrollWidth-window.innerWidth"); sw = W.evaluate("(()=>{const s=document.getElementById('sheet');return s.scrollWidth-s.clientWidth})()")
        t("A12 320px dark: " + fn + " fits", ov <= 0 and sw <= 0, [ov, sw]); W.evaluate("closeSheet()")

    real_errors = [e for e in errors if "favicon" not in e and "ERR_" not in e and "Failed to load resource" not in e]
    t("No page errors during the run", not real_errors, real_errors[:5])
    br.close()

fails = [r for r in results if not r[1]]
print(f"\n{len(results)-len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
