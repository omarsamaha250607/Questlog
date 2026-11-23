import os
import psycopg2, uuid, datetime, sys
SQL=open('../supabase/01_schema.sql').read()
admin=psycopg2.connect(host=os.environ.get('PGHOST','/tmp/pgtest'),port=int(os.environ.get('PGPORT','54329')),user=os.environ.get('PGUSER','postgres'),password=os.environ.get('PGPASSWORD') or None,dbname='postgres'); admin.autocommit=True
a=admin.cursor()
a.execute("drop schema if exists public cascade; create schema public; drop schema if exists auth cascade;")
a.execute("""
do $$ begin
 if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if;
 if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated nologin; end if;
 if not exists (select 1 from pg_roles where rolname='service_role') then create role service_role nologin bypassrls; end if;
end $$;
create schema auth;
create table auth.users(id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $$;
grant usage on schema public, auth to anon, authenticated;
grant execute on function auth.uid() to anon, authenticated;
-- Supabase's default: new public objects are granted to anon/authenticated/service_role
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
drop publication if exists supabase_realtime; create publication supabase_realtime;
""")
a.execute(SQL); a.execute(SQL)  # twice: must be safely re-runnable
print("schema applied twice without error")
A,B=str(uuid.uuid4()),str(uuid.uuid4())
a.execute("insert into auth.users values (%s),(%s)",(A,B))
res=[]
def t(n,c,d=""): res.append((n,bool(c),d)); print(("PASS " if c else "FAIL ")+n+((" :: "+str(d)) if d and not c else ""))
def conn(role,uid=None):
    c=psycopg2.connect(host=os.environ.get('PGHOST','/tmp/pgtest'),port=int(os.environ.get('PGPORT','54329')),user=os.environ.get('PGUSER','postgres'),password=os.environ.get('PGPASSWORD') or None,dbname='postgres'); c.autocommit=True
    cur=c.cursor(); cur.execute(f"set role {role}")
    if uid: cur.execute("select set_config('request.jwt.claim.sub',%s,false)",(uid,))
    return cur
def tryq(cur,sql,args=()):
    try: cur.execute(sql,args); return ("ok",cur.fetchall() if cur.description else cur.rowcount)
    except Exception as e: return ("err",str(e).split("\n")[0])
anon,ca,cb=conn('anon'),conn('authenticated',A),conn('authenticated',B)
today=datetime.date.today()
push="select * from public.push_save(%s,%s,%s,%s,%s,%s,%s,%s)"
# anonymous
for tb in ("saves","save_backups","restore_points","devices"):
    r=tryq(anon,f"select * from public.{tb}"); t(f"anon cannot read {tb}", r[0]=="err")
t("anon cannot insert saves", tryq(anon,"insert into public.saves(user_id,rev,device_id,json) values (%s,1,'x','{}')",(A,))[0]=="err")
t("anon cannot call push_save", tryq(anon,push,(0,'d','{}','n',1,1,today,False))[0]=="err")
# A creates
r=tryq(ca,push,(0,'devA','{"v":1,"who":"A"}','A',100,5,today,False)); t("A first save: ok rev 1", r==("ok",[(True,1)]), r)
r=tryq(ca,"select user_id::text,rev,json from public.saves"); t("A reads only own save", r[0]=="ok" and len(r[1])==1 and r[1][0][0]==A, r)
t("B sees none of A's save", tryq(cb,"select * from public.saves")==("ok",[]))
t("B cannot insert a save for A", tryq(cb,"insert into public.saves(user_id,rev,device_id,json) values (%s,1,'x','{}')",(A,))[0]=="err")
t("B update of A's save changes 0 rows", tryq(cb,"update public.saves set xp=999 where user_id=%s",(A,))==("ok",0))
t("B delete of A's save removes 0 rows", tryq(cb,"delete from public.saves where user_id=%s",(A,))==("ok",0))
r=tryq(ca,"select xp,rev from public.saves"); t("A's save untouched by B's attempts", r==("ok",[(100,1)]), r)
# B independent
r=tryq(cb,push,(0,'devB','{"v":1,"who":"B"}','B',7,0,today,False)); t("B creates own save, rev 1", r==("ok",[(True,1)]), r)
r=tryq(ca,"select json from public.saves"); t("A still sees only A's json", r==("ok",[('{"v":1,"who":"A"}',)]), r)
# CAS
r=tryq(ca,push,(0,'devA2','{"x":1}','A',1,1,today,False)); t("stale push (expected 0) is a conflict, rev stays 1", r==("ok",[(False,1)]), r)
r=tryq(ca,push,(5,'devA2','{"x":1}','A',1,1,today,False)); t("push with a future revision is a conflict", r==("ok",[(False,1)]), r)
r=tryq(ca,push,(1,'devA','{"v":2,"who":"A"}','A',150,9,today,True)); t("correct push -> rev 2", r==("ok",[(True,2)]), r)
r=tryq(ca,"select json,rev from public.restore_points"); t("restore point holds the pre-change copy", r==("ok",[('{"v":1,"who":"A"}',1)]), r)
r=tryq(ca,"select json from public.save_backups where kind='replaced'"); t("'replaced' backup holds the pre-change copy", r==("ok",[('{"v":1,"who":"A"}',)]), r)
r=tryq(ca,push,(2,'devA','{"v":3,"who":"A"}','A',160,9,today,False)); t("second push same day -> rev 3", r==("ok",[(True,3)]), r)
r=tryq(ca,"select json,rev from public.restore_points"); t("same-day push does not replace the restore point", r==("ok",[('{"v":1,"who":"A"}',1)]), r)
tomorrow=today+datetime.timedelta(days=1)
r=tryq(ca,push,(3,'devA','{"v":4,"who":"A"}','A',170,9,tomorrow,False)); t("next day push -> rev 4", r==("ok",[(True,4)]), r)
r=tryq(ca,"select count(*) from public.restore_points"); t("next day creates a second restore point", r==("ok",[(2,)]), r)
# size
r=tryq(ca,push,(4,'devA','x'*300001,'A',1,1,today,False)); t("oversize save rejected", r[0]=="err", r)
r=tryq(ca,"select rev from public.saves"); t("oversize attempt did not change the save", r==("ok",[(4,)]), r)
# not signed-in-like: authenticated without uid
cn=conn('authenticated')
t("authenticated role without a user id cannot push", tryq(cn,push,(0,'d','{}','n',1,1,today,False))[0]=="err")
# other tables isolation
t("B sees none of A's restore points", tryq(cb,"select * from public.restore_points")==("ok",[]))
t("B sees none of A's backups", tryq(cb,"select * from public.save_backups")==("ok",[]))
t("A can register a device", tryq(ca,"insert into public.devices(user_id,device_id,label) values (%s,'devA','Mac')",(A,))==("ok",1))
t("B cannot register a device for A", tryq(cb,"insert into public.devices(user_id,device_id,label) values (%s,'evil','x')",(A,))[0]=="err")
t("B sees none of A's devices", tryq(cb,"select * from public.devices")==("ok",[]))
t("B cannot mark A's device disconnected", tryq(cb,"update public.devices set revoked_at=now() where user_id=%s",(A,))==("ok",0))
t("B cannot write a backup for A", tryq(cb,"insert into public.save_backups(user_id,kind,json) values (%s,'pull','{}')",(A,))[0]=="err")
t("B cannot write a restore point for A", tryq(cb,"insert into public.restore_points(user_id,slot,day,json) values (%s,0,current_date,'{}')",(A,))[0]=="err")
t("label longer than 40 rejected", tryq(ca,"update public.devices set label=%s where device_id='devA'",('y'*41,))[0]=="err")
# can the client write rev directly? (allowed for the owner only)
t("B cannot change A's rev directly", tryq(cb,"update public.saves set rev=99 where user_id=%s",(A,))==("ok",0))
# delete own
t("A deletes own save", tryq(ca,"delete from public.saves")==("ok",1))
t("A's delete did not touch B", tryq(cb,"select rev from public.saves")==("ok",[(1,)]))
# realtime publication
a.execute("select tablename from pg_publication_tables where pubname='supabase_realtime'"); t("saves is published for live updates", ('saves',) in a.fetchall())
bad=[r for r in res if not r[1]]; print(f"\n{len(res)-len(bad)}/{len(res)} passed"); sys.exit(1 if bad else 0)
