-- Questlog: READ-ONLY checks for the account-deletion setup. Changes and deletes nothing. Safe to run any time.
-- Paste into the Supabase SQL Editor and read each result. "Expected" says what a correct project shows.

-- 1. The function: expect one row; security_definer = true; search_path = {search_path=""} ; owner = postgres;
--    arguments = "p_password text". There must be NO row with empty arguments.
select p.oid::regprocedure as function, p.prosecdef as security_definer, p.proconfig as settings, pg_get_userbyid(p.proowner) as owner
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public' and p.proname = 'delete_my_account';

-- 2. Who may run it: expect authenticated = true, anon = false.
select has_function_privilege('anon',          'public.delete_my_account(text)', 'execute') as anon_can_run,
       has_function_privilege('authenticated', 'public.delete_my_account(text)', 'execute') as signed_in_can_run;

-- 3. Every table in your own schemas that points at a login, and what happens when the login is deleted.
--    Expect every row to say "cascade" (or a table you know about). Anything "no action" or "restrict" would
--    BLOCK deletion; anything not listed here is not removed by the function, so review it.
select c.conrelid::regclass as table_name, a.attname as column_name,
       case c.confdeltype when 'c' then 'cascade' when 'n' then 'set null' when 'r' then 'restrict' when 'a' then 'no action' else c.confdeltype::text end as when_login_deleted
from pg_constraint c join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
where c.contype = 'f' and c.confrelid = 'auth.users'::regclass
order by 1, 2;

-- 4. Tables in the public schema with a user_id column: expect exactly saves, save_backups, restore_points, devices, delete_attempts.
select table_name from information_schema.columns where table_schema = 'public' and column_name = 'user_id' order by 1;

-- 5. Triggers on your tables and on logins: expect no rows, unless you added some yourself.
select event_object_schema, event_object_table, trigger_name, action_timing, event_manipulation
from information_schema.triggers
where event_object_schema = 'public' or (event_object_schema = 'auth' and event_object_table = 'users')
order by 1, 2, 3;

-- 6. Storage: Questlog does not use it. Expect 0 buckets and 0 files.
select (select count(*) from storage.buckets) as buckets, (select count(*) from storage.objects) as files;

-- 7. The lockout table must not be reachable from the app: expect rls_on = true and no grants to anon/authenticated.
select c.relrowsecurity as rls_on,
       has_table_privilege('anon', 'public.delete_attempts', 'select') as anon_can_read,
       has_table_privilege('authenticated', 'public.delete_attempts', 'select') as signed_in_can_read
from pg_class c where c.oid = 'public.delete_attempts'::regclass;

-- 8. Exactly who can run the function: expect exactly two rows, the owner (postgres) and authenticated. No PUBLIC, anon or service_role.
select coalesce(g.rolname, 'PUBLIC') as can_run_it
from pg_proc p cross join lateral aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x left join pg_roles g on g.oid = x.grantee
where p.oid = 'public.delete_my_account(text)'::regprocedure
order by 1;

-- 9. Why the script revokes: Supabase's default grants for new objects in public (shows anon/authenticated/service_role).
select defaclrole::regrole as created_by, defaclobjtype as kind, defaclacl as default_grants from pg_default_acl where defaclnamespace = 'public'::regnamespace;

-- 10. The function owner can delete logins (expect true).
select pg_get_userbyid(proowner) as owner, has_table_privilege(pg_get_userbyid(proowner), 'auth.users', 'DELETE') as owner_can_delete_logins
from pg_proc where oid = 'public.delete_my_account(text)'::regprocedure;
