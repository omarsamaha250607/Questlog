-- Questlog: let a signed-in person delete their own account, after the SERVER has re-checked their password.
-- Run once in the Supabase SQL Editor (safe to run again). Running this script deletes NOTHING: it only creates
-- a function and a small lockout table, and replaces the earlier password-less draft of the function if it exists.
--
-- Deployment is ONE transaction (begin ... commit). Other sessions cannot see anything until the commit, and the
-- permissions are locked down and self-checked before the commit. If any step or check fails, everything is rolled
-- back and nothing is left behind. Even if it were visible, the function refuses anyone who is not signed in
-- (auth.uid() is null) and every signed-in caller must still give the correct password.
--
-- How it is protected
--  * Acts only on auth.uid(), the signed-in caller. It takes no user id, so nobody can name another account.
--  * Re-checks the caller's current password against the stored hash INSIDE the database.
--  * After 5 wrong passwords in 15 minutes it refuses for the rest of that window. Wrong passwords are returned as
--    a result, not raised as an error, so the count is not rolled back.
--  * Runs as its owner ("security definer") because only that role may remove a row from auth.users. Empty
--    search_path and fully-qualified names stop look-alike functions being slipped in.
--  * Execute right: signed-in users only. PUBLIC, anon and service_role are revoked. The script checks this itself.
--  * Deleting the login also removes its sessions, refresh tokens and identities (Supabase cascades them). An access
--    token already in a browser can live until it expires (about an hour) but can read and write nothing.

begin;

-- 0. Pre-flight: stop before creating anything if the role running this script cannot delete logins.
do $$
begin
  if not has_table_privilege(current_user, 'auth.users', 'DELETE') or not has_table_privilege(current_user, 'auth.users', 'SELECT') then
    raise exception 'Pre-flight failed: the role running this script (%) cannot read and delete from auth.users. Run it in the Supabase SQL Editor as the default postgres role.', current_user;
  end if;
end $$;

create extension if not exists pgcrypto with schema extensions;

-- 1. Counts wrong passwords. Not readable or writable by the app at all; only the function below touches it.
create table if not exists public.delete_attempts (
  user_id      uuid primary key references auth.users(id) on delete cascade,
  failures     integer     not null default 0,
  window_start timestamptz not null default now()
);
alter table public.delete_attempts enable row level security;
revoke all on public.delete_attempts from public, anon, authenticated;

-- 2. Remove the earlier draft that had no password check, so it cannot linger.
drop function if exists public.delete_my_account();

-- 3. The function.
create or replace function public.delete_my_account(p_password text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid   uuid := auth.uid();
  v_hash  text;
  v_fail  integer;
  v_start timestamptz;
begin
  if v_uid is null then
    raise exception 'not signed in' using errcode = '42501';
  end if;

  -- Lock the login row so two simultaneous attempts cannot both pass the lockout check.
  select u.encrypted_password into v_hash from auth.users u where u.id = v_uid for update;
  if not found then
    return 'no_account';          -- the login is already gone (for example an old access token)
  end if;

  -- Only bcrypt hashes (what Supabase stores for email sign-ups) can be checked here. Anything else: refuse safely.
  if v_hash is not null and v_hash <> '' and left(v_hash, 2) <> '$2' then
    return 'unsupported';
  end if;

  select a.failures, a.window_start into v_fail, v_start from public.delete_attempts a where a.user_id = v_uid;
  if found and v_start > now() - interval '15 minutes' and v_fail >= 5 then
    return 'locked';
  end if;

  if p_password is null or p_password = '' or v_hash is null or v_hash = ''
     or extensions.crypt(p_password, v_hash) <> v_hash then
    insert into public.delete_attempts as a (user_id, failures, window_start) values (v_uid, 1, now())
    on conflict (user_id) do update
      set failures     = case when a.window_start > now() - interval '15 minutes' then a.failures + 1 else 1 end,
          window_start = case when a.window_start > now() - interval '15 minutes' then a.window_start else now() end;
    return 'bad_password';
  end if;

  delete from public.restore_points where user_id = v_uid;
  delete from public.save_backups   where user_id = v_uid;
  delete from public.devices        where user_id = v_uid;
  delete from public.saves          where user_id = v_uid;
  delete from auth.users            where id = v_uid;   -- cascades: delete_attempts, sessions, refresh tokens, identities
  return 'deleted';
end;
$$;

-- 4. Permissions. Supabase's defaults grant new functions to PUBLIC, anon, authenticated and service_role.
revoke all on function public.delete_my_account(text) from public, anon, authenticated, service_role;
grant execute on function public.delete_my_account(text) to authenticated;

-- 5. Self-check before commit: if anything but the owner and signed-in users can run the function, or anything
--    but the owner and service_role can touch the lockout table, fail and roll everything back.
do $$
declare bad text;
begin
  select string_agg(coalesce(g.rolname, 'PUBLIC'), ', ') into bad
  from pg_proc p
  cross join lateral aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x
  left join pg_roles g on g.oid = x.grantee
  where p.oid = 'public.delete_my_account(text)'::regprocedure
    and x.grantee <> p.proowner and g.rolname is distinct from 'authenticated';
  if bad is not null then raise exception 'Permission check failed: these can run delete_my_account: %', bad; end if;

  if not has_function_privilege('authenticated', 'public.delete_my_account(text)', 'execute') then
    raise exception 'Permission check failed: signed-in users cannot run delete_my_account';
  end if;

  if not exists (select 1 from pg_proc p where p.oid = 'public.delete_my_account(text)'::regprocedure and p.prosecdef and p.proconfig = array['search_path=""']) then
    raise exception 'Check failed: function must be security definer with an empty search_path';
  end if;

  if not has_table_privilege((select pg_get_userbyid(proowner) from pg_proc where oid = 'public.delete_my_account(text)'::regprocedure), 'auth.users', 'DELETE') then
    raise exception 'Check failed: the function owner cannot delete from auth.users';
  end if;

  select string_agg(coalesce(g.rolname, 'PUBLIC'), ', ') into bad
  from pg_class c
  cross join lateral aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) x
  left join pg_roles g on g.oid = x.grantee
  where c.oid = 'public.delete_attempts'::regclass
    and x.grantee <> c.relowner and g.rolname is distinct from 'service_role';
  if bad is not null then raise exception 'Permission check failed: these can access delete_attempts: %', bad; end if;
end $$;

commit;
