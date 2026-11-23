import os, psycopg2, uuid, datetime, sys
HERE = os.path.dirname(os.path.abspath(__file__))
SQLD = os.path.join(HERE, "..", "supabase")
def rd(n): return open(os.path.join(SQLD, n)).read()
def cn(db="postgres"): return psycopg2.connect(host=os.environ.get('PGHOST','/tmp/pgtest'),port=int(os.environ.get('PGPORT','54329')),user=os.environ.get('PGUSER','postgres'),password=os.environ.get('PGPASSWORD') or None,dbname=db)
admin=cn(); admin.autocommit=True; a=admin.cursor()
a.execute("drop schema if exists public cascade; create schema public; drop schema if exists auth cascade; drop schema if exists extensions cascade; create schema extensions; create extension if not exists pgcrypto with schema extensions;")
a.execute("""
do $$ begin
 if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
 if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
 if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin bypassrls; end if;
end $$;
create schema auth;
create table auth.users(id uuid primary key, email text, encrypted_password text);
create table auth.sessions(id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade);
create table auth.identities(id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade);
create table auth.refresh_tokens(id bigserial primary key, session_id uuid references auth.sessions(id) on delete cascade);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $$;
grant usage on schema public, auth, extensions to anon, authenticated;
grant execute on function auth.uid() to anon, authenticated;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
drop publication if exists supabase_realtime; create publication supabase_realtime;
""")
a.execute(rd("01_schema.sql"))
# simulate a project that already ran the earlier password-less draft of 04
a.execute("""create function public.delete_my_account() returns void language plpgsql security definer set search_path='' as $$ begin delete from auth.users where id=auth.uid(); end $$;
grant execute on function public.delete_my_account() to authenticated;""")
SQL4=rd("04_account_deletion.sql")
a.execute(SQL4); a.execute(SQL4); print("04 applied over the old draft and again, without error")
A,B,C=(str(uuid.uuid4()) for _ in range(3))
a.execute("insert into auth.users(id,email,encrypted_password) values (%s,'a@x',extensions.crypt('alpha-pass',extensions.gen_salt('bf'))),(%s,'b@x',extensions.crypt('bravo-pass',extensions.gen_salt('bf'))),(%s,'c@x',extensions.crypt('charlie-pass',extensions.gen_salt('bf')))",(A,B,C))
for u in (A,B,C):
    a.execute("insert into auth.sessions(user_id) values (%s)",(u,)); a.execute("insert into auth.identities(user_id) values (%s)",(u,))
a.execute("insert into auth.refresh_tokens(session_id) select id from auth.sessions")
today=datetime.date.today(); res=[]
def t(n,c,d=""): res.append((n,bool(c))); print(("PASS " if c else "FAIL ")+n+((" :: "+str(d)) if d and not c else ""))
def conn(role,uid=None):
    c=cn(); c.autocommit=True; cur=c.cursor(); cur.execute(f"set role {role}")
    if uid: cur.execute("select set_config('request.jwt.claim.sub',%s,false)",(uid,))
    return cur
def tryq(cur,sql,args=()):
    try: cur.execute(sql,args); return ("ok",cur.fetchall() if cur.description else cur.rowcount)
    except Exception as e: return ("err",str(e).split("\n")[0])
push="select * from public.push_save(%s,%s,%s,%s,%s,%s,%s,%s)"
for u,l in ((A,'devA'),(B,'devB'),(C,'devC')):
    cu=conn('authenticated',u); tryq(cu,push,(0,l,'{"who":"%s"}'%l,l,1,1,today,False)); tryq(cu,push,(1,l,'{"n":2}',l,2,1,today,True))
    tryq(cu,"insert into public.devices(user_id,device_id,label) values (%s,%s,'x')",(u,l))
anon,ca,cb,cc=conn('anon'),conn('authenticated',A),conn('authenticated',B),conn('authenticated',C)
DEL="select public.delete_my_account(%s)"
def cnt(tb,u): a.execute(f"select count(*) from public.{tb} where user_id=%s",(u,)); return a.fetchone()[0]
def alive(u): a.execute("select count(*) from auth.users where id=%s",(u,)); return a.fetchone()[0]==1
def userdata(u): return sum(cnt(tb,u) for tb in ("saves","save_backups","restore_points","devices"))
# --- shape and permissions
a.execute("select pronargs, pg_get_function_arguments(oid) from pg_proc where proname='delete_my_account' and pronamespace='public'::regnamespace"); r=a.fetchall()
t("only the password-taking version exists (the old no-password draft was replaced)", r==[(1,'p_password text')], r)
a.execute("select prosecdef, proconfig, pg_get_userbyid(proowner) from pg_proc where proname='delete_my_account'"); r=a.fetchone()
t("security definer, empty search_path, owned by the database owner", r[0] is True and r[1]==['search_path=""'] and r[2]=='postgres', r)
a.execute("select has_function_privilege('anon','public.delete_my_account(text)','execute'), has_function_privilege('authenticated','public.delete_my_account(text)','execute')"); t("anon cannot run it, signed-in users can", a.fetchone()==(False,True))
a.execute("select count(*) from pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f',p.proowner))) x where p.proname='delete_my_account' and x.grantee=0"); t("PUBLIC has no execute right", a.fetchone()[0]==0)
a.execute("select coalesce(g.rolname,'PUBLIC') from pg_proc p cross join lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) x left join pg_roles g on g.oid=x.grantee where p.proname='delete_my_account' order by 1"); r=[x[0] for x in a.fetchall()]
t("exactly the owner and signed-in users can run it (no PUBLIC, anon or service_role)", r==['authenticated','postgres'], r)
t("service_role cannot run it either", tryq(conn('service_role'),DEL,('x',))[0]=="err")
a.execute("select distinct coalesce(g.rolname,'PUBLIC') from pg_class c cross join lateral aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) x left join pg_roles g on g.oid=x.grantee where c.relname='delete_attempts' order by 1"); r=[x[0] for x in a.fetchall()]
t("only the owner and service_role can touch the lockout table", r==['postgres','service_role'], r)
t("anonymous call refused", tryq(anon,DEL,('alpha-pass',))[0]=="err")
r=tryq(conn('authenticated',None),DEL,('x',)); t("signed-in role without a user id is refused (42501)", r[0]=="err" and "not signed in" in r[1], r)
t("lockout table is unreachable for the app (read, write)", tryq(ca,"select * from public.delete_attempts")[0]=="err" and tryq(ca,"insert into public.delete_attempts(user_id) values (%s)",(A,))[0]=="err" and tryq(anon,"select * from public.delete_attempts")[0]=="err")
a.execute("select count(*) from pg_constraint c where c.contype='f' and c.confrelid='auth.users'::regclass and c.confdeltype<>'c'"); t("every foreign key to a login cascades on delete", a.fetchone()[0]==0)
a.execute("select count(*) from information_schema.triggers where event_object_schema='public'"); t("no triggers on the app's tables", a.fetchone()[0]==0)
a.execute("select array_agg(table_name::text order by table_name::text) from information_schema.columns where table_schema='public' and column_name='user_id'"); TABLES=a.fetchone()[0]
t("exactly the five known per-account tables carry a user_id", TABLES==['delete_attempts','devices','restore_points','save_backups','saves'], TABLES)
# --- password checks
before=(userdata(A),userdata(B))
for bad in ('wrong','',None,'ALPHA-PASS','alpha-pass '):
    r=tryq(ca,DEL,(bad,)); t(f"wrong or empty password {bad!r} -> bad_password, nothing deleted", r==("ok",[('bad_password',)]) and alive(A) and userdata(A)==before[0], r)
    a.execute("update public.delete_attempts set failures=0 where user_id=%s",(A,)) if False else None
a.execute("select failures from public.delete_attempts where user_id=%s",(A,)); t("failed attempts are remembered (the count survives the call)", a.fetchone()[0]==5)
r=tryq(ca,DEL,('alpha-pass',)); t("after 5 wrong tries even the right password is refused for now ('locked')", r==("ok",[('locked',)]) and alive(A) and userdata(A)==before[0], r)
a.execute("update public.delete_attempts set window_start=now()-interval '16 minutes' where user_id=%s",(A,))
# a different account's password cannot delete this account
r=tryq(cb,DEL,('alpha-pass',)); t("B using A's password cannot delete B (it is checked against the caller's own login)", r==("ok",[('bad_password',)]) and alive(B) and alive(A), r)
r=tryq(cb,DEL,('bravo-pass',)); t("B's own password only ever deletes B, never A", r==("ok",[('deleted',)]) and not alive(B) and alive(A) and userdata(A)==before[0], r)
D=str(uuid.uuid4()); a.execute("insert into auth.users(id,email,encrypted_password) values (%s,'d@x','$argon2id$v=19$m=65536,t=3,p=4$abc$def')",(D,)); a.execute("insert into public.saves(user_id,rev,device_id,json) values (%s,1,'d','{}')",(D,))
r=tryq(conn('authenticated',D),DEL,('anything',)); t("a password hash this function cannot check is refused safely, nothing deleted", r==("ok",[('unsupported',)]) and alive(D) and cnt("saves",D)==1, r)
# --- shadowing attack: a look-alike function in public cannot change the check
a.execute("create function public.crypt(text,text) returns text language sql as $$ select $2 $$")
r=tryq(cc,DEL,('not-the-password',)); t("a look-alike crypt() in public cannot bypass the password check", r==("ok",[('bad_password',)]) and alive(C), r)
a.execute("drop function public.crypt(text,text)")
# --- success path for A (after the lockout window passes)
r=tryq(ca,DEL,('alpha-pass',)); t("correct password deletes the caller's account", r==("ok",[('deleted',)]), r)
t("every per-account row is gone from every table", all(cnt(tb,A)==0 for tb in TABLES))
t("the login, its sessions, identities and refresh tokens are gone", not alive(A) and all((a.execute(f"select count(*) from auth.{tb} where user_id=%s",(A,)) or a.fetchone()[0])==0 for tb in ("sessions","identities")))
a.execute("select count(*) from auth.refresh_tokens r left join auth.sessions s on s.id=r.session_id where s.id is null"); t("no orphaned refresh tokens remain", a.fetchone()[0]==0)
t("C's data and login are untouched", userdata(C)>0 and alive(C) and cnt("delete_attempts",C)==1)
# --- old access token (same user id) after deletion
r=tryq(ca,DEL,('alpha-pass',)); t("a still-valid old token cannot delete again or learn anything ('no_account')", r==("ok",[('no_account',)]), r)
r=tryq(ca,push,(0,'devA','{"x":1}','A',1,1,today,False)); t("an old token cannot write a new save (login no longer exists)", r[0]=="err", r)
t("an old token cannot add a device or read anything", tryq(ca,"insert into public.devices(user_id,device_id,label) values (%s,'z','z')",(A,))[0]=="err" and tryq(ca,"select * from public.saves")==("ok",[]))
t("an old token cannot see C's data", tryq(ca,"select * from public.saves where user_id=%s",(C,))==("ok",[]))
# --- C can still delete with the right password afterwards (re-deploy / lockout reset)
r=tryq(cc,DEL,('charlie-pass',)); t("another account still works normally and is deleted only by its own password", r==("ok",[('deleted',)]) and not alive(C))
# --- deployment safety: no exposure while the script runs, all-or-nothing, pre-flight
import re
def reset_fn():
    a.execute("drop function if exists public.delete_my_account(text); drop table if exists public.delete_attempts cascade")
def exists_fn(cur): cur.execute("select to_regprocedure('public.delete_my_account(text)') is not null, to_regclass('public.delete_attempts') is not null"); return cur.fetchone()
reset_fn()
c1=cn(); c1.autocommit=True; k1=c1.cursor()
body=SQL4.rstrip(); assert body.endswith("commit;"); k1.execute(body[:-len("commit;")])   # everything except the final commit
k2=conn('anon'); k3=conn('authenticated',B)
t("while the script is still running, other sessions cannot see or call the function or table", exists_fn(k2)==(False,False) and tryq(k3,DEL,('x',))[0]=="err")
k1.execute("rollback"); c1.close()
t("an unfinished deployment leaves nothing behind", exists_fn(a)==(False,False))
bad_sql=SQL4.replace("\ncommit;","\nselect 1/0;\ncommit;")
try: a.execute(bad_sql); failed=False
except Exception: failed=True
a.execute("rollback")
t("if any step fails the whole deployment is rolled back, leaving nothing behind", failed and exists_fn(a)==(False,False))
a.execute("do $$ begin if not exists (select 1 from pg_roles where rolname='limited_deployer') then create role limited_deployer nologin; end if; end $$; grant usage on schema public, auth, extensions to limited_deployer; grant create on schema public to limited_deployer")
try: a.execute("set role limited_deployer"); a.execute(SQL4); msg=""
except Exception as e: msg=str(e); a.execute("rollback")
a.execute("reset role")
t("pre-flight stops a deploy from a role that cannot manage logins, before creating anything", "Pre-flight failed" in msg and exists_fn(a)==(False,False), msg[:120])
a.execute(SQL4); t("the script applies cleanly again afterwards", exists_fn(a)==(True,True))
a.execute("select coalesce(g.rolname,'PUBLIC') from pg_proc p cross join lateral aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) x left join pg_roles g on g.oid=x.grantee where p.proname='delete_my_account' order by 1"); t("final permissions are again owner + authenticated only", [x[0] for x in a.fetchall()]==['authenticated','postgres'])
# the self-check really trips: grant to anon by hand, then see the script revoke it; and a tampered script is caught
a.execute("grant execute on function public.delete_my_account(text) to anon, public"); a.execute(SQL4)
a.execute("select has_function_privilege('anon','public.delete_my_account(text)','execute')"); t("re-running the script repairs any permission someone widened by hand", a.fetchone()[0] is False)
tampered=SQL4.replace("revoke all on function public.delete_my_account(text) from public, anon, authenticated, service_role;","")
try: a.execute("grant execute on function public.delete_my_account(text) to anon"); a.execute(tampered); caught=False
except Exception as e: caught="Permission check failed" in str(e); a.execute("rollback")
t("the built-in permission self-check aborts a deployment that would leave the function exposed", caught)
a.execute(SQL4)
bad=[x for x in res if not x[1]]; print(f"\n{len(res)-len(bad)}/{len(res)} passed"); sys.exit(1 if bad else 0)
