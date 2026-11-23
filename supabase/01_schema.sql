-- Questlog cloud save, Supabase schema (stage 1: parity with the Claude-account cloud save).
-- Run once in the Supabase dashboard: SQL Editor -> New query -> paste -> Run.
-- Safe to run again. Contains no secrets. No social tables yet.

-- ---------------------------------------------------------------- tables
create table if not exists public.saves (
  user_id   uuid primary key references auth.users(id) on delete cascade,
  rev       integer not null check (rev >= 1),
  device_id text    not null check (char_length(device_id) between 1 and 64),
  saved_at  timestamptz not null default now(),
  fmt       integer not null default 4,
  name      text,
  xp        integer,
  gold      integer,
  json      text not null check (char_length(json) <= 300000)
);

-- one row per kind: 'link' (browser copy at link time), 'replaced' (account copy before an
-- intentional overwrite), 'pull' (browser copy before it was replaced by the account copy)
create table if not exists public.save_backups (
  user_id   uuid not null references auth.users(id) on delete cascade,
  kind      text not null check (kind in ('link','replaced','pull')),
  saved_at  timestamptz not null default now(),
  device_id text check (char_length(device_id) <= 64),
  json      text not null check (char_length(json) <= 300000),
  primary key (user_id, kind)
);

-- up to five daily restore points (slot = day number mod 5)
create table if not exists public.restore_points (
  user_id   uuid not null references auth.users(id) on delete cascade,
  slot      smallint not null check (slot between 0 and 4),
  day       date not null,
  taken_at  timestamptz not null default now(),
  copy_saved_at timestamptz,
  rev       integer,
  device_id text check (char_length(device_id) <= 64),
  json      text not null check (char_length(json) <= 300000),
  primary key (user_id, slot)
);

create table if not exists public.devices (
  user_id    uuid not null references auth.users(id) on delete cascade,
  device_id  text not null check (char_length(device_id) between 1 and 64),
  label      text check (char_length(label) <= 40),
  first_seen timestamptz not null default now(),
  last_seen  timestamptz not null default now(),
  revoked_at timestamptz,
  primary key (user_id, device_id)
);

alter table public.restore_points add column if not exists copy_saved_at timestamptz;

-- ---------------------------------------------------------------- access rules
alter table public.saves          enable row level security;
alter table public.save_backups   enable row level security;
alter table public.restore_points enable row level security;
alter table public.devices        enable row level security;

revoke all on table public.saves, public.save_backups, public.restore_points, public.devices from anon, public;
grant select, insert, update, delete on table public.saves, public.save_backups, public.restore_points, public.devices to authenticated;

do $$
declare t text;
begin
  foreach t in array array['saves','save_backups','restore_points','devices'] loop
    execute format('drop policy if exists owner_all on public.%I', t);
    execute format('create policy owner_all on public.%I for all to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id)', t);
  end loop;
end $$;

-- ---------------------------------------------------------------- atomic save
-- Compare-and-set on the revision. expected_rev = 0 means "I believe no account copy exists".
-- Returns ok=false with the current revision when the account copy moved on (a conflict).
-- Also keeps one restore point per day (the account copy as it was before the day's first change).
create or replace function public.push_save(
  p_expected_rev     integer,
  p_device           text,
  p_json             text,
  p_name             text,
  p_xp               integer,
  p_gold             integer,
  p_day              date,
  p_backup_replaced  boolean default false
) returns table (ok boolean, new_rev integer)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_uid  uuid := auth.uid();
  v_cur  public.saves%rowtype;
  v_slot smallint;
begin
  if v_uid is null then
    raise exception 'not signed in' using errcode = '28000';
  end if;
  if p_json is null or char_length(p_json) > 300000 then
    raise exception 'save too large' using errcode = '22023';
  end if;

  select * into v_cur from public.saves where user_id = v_uid for update;

  if not found then
    if p_expected_rev <> 0 then
      return query select false, 0;
      return;
    end if;
    insert into public.saves (user_id, rev, device_id, saved_at, name, xp, gold, json)
    values (v_uid, 1, p_device, now(), p_name, p_xp, p_gold, p_json);
    return query select true, 1;
    return;
  end if;

  if v_cur.rev <> p_expected_rev then
    return query select false, v_cur.rev;
    return;
  end if;

  v_slot := ((p_day - date '1970-01-01') % 5)::smallint;
  if not exists (select 1 from public.restore_points r where r.user_id = v_uid and r.slot = v_slot and r.day = p_day) then
    insert into public.restore_points (user_id, slot, day, taken_at, copy_saved_at, rev, device_id, json)
    values (v_uid, v_slot, p_day, now(), v_cur.saved_at, v_cur.rev, v_cur.device_id, v_cur.json)
    on conflict (user_id, slot) do update
      set day = excluded.day, taken_at = excluded.taken_at, copy_saved_at = excluded.copy_saved_at, rev = excluded.rev,
          device_id = excluded.device_id, json = excluded.json;
  end if;

  if p_backup_replaced then
    insert into public.save_backups (user_id, kind, saved_at, device_id, json)
    values (v_uid, 'replaced', now(), v_cur.device_id, v_cur.json)
    on conflict (user_id, kind) do update
      set saved_at = excluded.saved_at, device_id = excluded.device_id, json = excluded.json;
  end if;

  update public.saves
     set rev = v_cur.rev + 1, device_id = p_device, saved_at = now(),
         name = p_name, xp = p_xp, gold = p_gold, json = p_json
   where user_id = v_uid;

  return query select true, v_cur.rev + 1;
end;
$$;

revoke all on function public.push_save(integer, text, text, text, integer, integer, date, boolean) from public, anon;
grant execute on function public.push_save(integer, text, text, text, integer, integer, date, boolean) to authenticated;

-- ---------------------------------------------------------------- live updates (row access rules still apply)
do $$
begin
  alter publication supabase_realtime add table public.saves;
exception when duplicate_object then null;
          when undefined_object then null;
end $$;
