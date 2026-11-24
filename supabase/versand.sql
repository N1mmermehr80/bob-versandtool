-- BOB-Versandtool: gemeinsame Ablage in Supabase (bob-db.fix-das.de)
-- Ersetzt den Netzwerkordner (FolderBackend). Idempotent – kann erneut ausgeführt werden.
-- Zugriff: nur angemeldete Microsoft-Konten, die in versand_benutzer stehen (aktiv).
-- Schreiben nur über die Funktionen versand_* (security definer, prüfen versand_erlaubt()).

-- ---------------------------------------------------------------- Benutzer / Hilfsfunktionen
create table if not exists public.versand_benutzer (
  email     text primary key check (email = lower(email)),
  name      text,
  aktiv     boolean not null default true,
  angelegt  timestamptz not null default now()
);

create or replace function public.versand_erlaubt() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from versand_benutzer
                 where email = lower(coalesce(auth.jwt() ->> 'email', '')) and aktiv);
$$;

create or replace function public.versand_name() returns text
language sql stable as $$
  select coalesce(nullif(auth.jwt() -> 'user_metadata' ->> 'full_name', ''),
                  nullif(auth.jwt() -> 'user_metadata' ->> 'name', ''),
                  auth.jwt() ->> 'email');
$$;

-- Zeitstempel im Format des Tools (lokale Zeit, ISO ohne Zone, Sekunden)
create or replace function public.versand_jetzt() returns text
language sql stable as $$
  select to_char(now() at time zone 'Europe/Berlin', 'YYYY-MM-DD"T"HH24:MI:SS');
$$;

create or replace function public.versand_pruefen() returns void
language plpgsql stable security definer set search_path = public as $$
begin
  if not versand_erlaubt() then
    raise exception 'Kein Zugriff auf das Versandtool für %', coalesce(auth.jwt() ->> 'email', 'unbekannt')
      using errcode = 'VS403';
  end if;
end $$;

-- ---------------------------------------------------------------- Tabellen
create table if not exists public.versand_auftraege (
  ref         text primary key,
  version     integer not null default 1,
  data        jsonb   not null,
  created     text    not null,
  updated_by  text,
  updated_at  text
);

create table if not exists public.versand_ausgeblendet (
  ref   text primary key,
  kind  text not null check (kind in ('deleted', 'completed')),
  by    text,
  at    text,
  data  jsonb
);

create table if not exists public.versand_einstellungen (
  id            integer primary key default 1 check (id = 1),
  data          jsonb not null,
  geaendert_von text,
  geaendert_am  text
);

create table if not exists public.versand_protokoll (
  id              bigserial primary key,
  zeitpunkt       text,
  benutzer        text,
  auftrag         text,
  sendungsnummer  text,
  parcel_id       text,
  empfaenger      text,
  hinweis         text,
  eingetragen     timestamptz not null default now()
);
create index if not exists versand_protokoll_zeit on public.versand_protokoll (zeitpunkt);

-- Änderungszähler für den schnellen Abgleich (statt Ordner-Signatur)
create table if not exists public.versand_stand (
  id integer primary key default 1 check (id = 1),
  n  bigint not null default 0
);
insert into public.versand_stand (id, n) values (1, 0) on conflict do nothing;

create or replace function public.versand_stand_erhoehen() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  update versand_stand set n = n + 1 where id = 1;
  return null;
end $$;

drop trigger if exists versand_auftraege_stand on public.versand_auftraege;
create trigger versand_auftraege_stand after insert or update or delete on public.versand_auftraege
  for each statement execute function public.versand_stand_erhoehen();
drop trigger if exists versand_ausgeblendet_stand on public.versand_ausgeblendet;
create trigger versand_ausgeblendet_stand after insert or update or delete on public.versand_ausgeblendet
  for each statement execute function public.versand_stand_erhoehen();

-- ---------------------------------------------------------------- Zugriffsregeln (nur lesen; schreiben über Funktionen)
alter table public.versand_benutzer      enable row level security;
alter table public.versand_auftraege     enable row level security;
alter table public.versand_ausgeblendet  enable row level security;
alter table public.versand_einstellungen enable row level security;
alter table public.versand_protokoll     enable row level security;
alter table public.versand_stand         enable row level security;

drop policy if exists lesen on public.versand_benutzer;
create policy lesen on public.versand_benutzer for select to authenticated
  using (email = lower(coalesce(auth.jwt() ->> 'email', '')));
do $$ declare t text; begin
  foreach t in array array['versand_auftraege','versand_ausgeblendet','versand_einstellungen',
                           'versand_protokoll','versand_stand'] loop
    execute format('drop policy if exists lesen on public.%I', t);
    execute format('create policy lesen on public.%I for select to authenticated using (public.versand_erlaubt())', t);
  end loop;
end $$;

revoke all on public.versand_benutzer, public.versand_auftraege, public.versand_ausgeblendet,
  public.versand_einstellungen, public.versand_protokoll, public.versand_stand from anon, authenticated;
grant select on public.versand_benutzer, public.versand_auftraege, public.versand_ausgeblendet,
  public.versand_einstellungen, public.versand_protokoll, public.versand_stand to authenticated;

-- ---------------------------------------------------------------- Funktionen (entsprechen FolderBackend)
-- Anmeldeversuche nicht freigeschalteter Konten (meldet der Server-Wächter in Teams)
create table if not exists public.versand_zugriffsversuche (
  email        text primary key,
  name         text,
  erster       timestamptz not null default now(),
  letzter      timestamptz not null default now(),
  anzahl       integer not null default 1,
  gemeldet_am  timestamptz
);
alter table public.versand_zugriffsversuche enable row level security;
revoke all on public.versand_zugriffsversuche from anon, authenticated;

create or replace function public.versand_ich() returns jsonb
language plpgsql volatile security definer set search_path = public as $$
declare ok boolean := versand_erlaubt(); mail text := lower(coalesce(auth.jwt() ->> 'email', ''));
begin
  if not ok and mail <> '' then
    insert into versand_zugriffsversuche (email, name) values (mail, versand_name())
    on conflict (email) do update set letzter = now(), anzahl = versand_zugriffsversuche.anzahl + 1,
                                      name = excluded.name;
  end if;
  return jsonb_build_object('email', auth.jwt() ->> 'email', 'name', versand_name(), 'erlaubt', ok);
end $$;

create or replace function public.versand_signatur() returns bigint
language plpgsql stable security definer set search_path = public as $$
begin
  perform versand_pruefen();
  return (select n from versand_stand where id = 1);
end $$;

create or replace function public.versand_put(p_ref text, p_version integer, p_data jsonb)
returns public.versand_auftraege
language plpgsql security definer set search_path = public as $$
declare cur versand_auftraege;
begin
  perform versand_pruefen();
  select * into cur from versand_auftraege where ref = p_ref for update;
  if not found then
    raise exception 'Auftrag % nicht mehr vorhanden', p_ref using errcode = 'VS404';
  end if;
  if cur.version <> p_version then
    raise exception 'Auftrag % wurde von % geändert', p_ref, cur.updated_by
      using errcode = 'VS409', detail = to_jsonb(cur)::text;
  end if;
  update versand_auftraege set version = version + 1, data = p_data, updated_by = versand_name(),
         updated_at = versand_jetzt()
   where ref = p_ref returning * into cur;
  return cur;
end $$;

create or replace function public.versand_claim(p_ref text, p_timeout_s integer default 900)
returns public.versand_auftraege
language plpgsql security definer set search_path = public as $$
declare cur versand_auftraege; d jsonb; me text := versand_name(); locked_at timestamp;
begin
  perform versand_pruefen();
  select * into cur from versand_auftraege where ref = p_ref for update;
  if not found then
    raise exception 'Auftrag % nicht mehr vorhanden', p_ref using errcode = 'VS404';
  end if;
  d := cur.data;
  if d ->> 'status' in ('Label erstellt', 'bereits vorhanden', 'manuell versendet', 'teilweise')
     or coalesce(d ->> 'tracking', '') <> '' then
    raise exception 'bereits erledigt (%, von %)', d ->> 'status', cur.updated_by
      using errcode = 'VS409', detail = to_jsonb(cur)::text;
  end if;
  begin
    locked_at := (d ->> 'locked_at')::timestamp;
  exception when others then
    locked_at := null;
  end;
  if coalesce(d ->> 'locked_by', '') <> '' and d ->> 'locked_by' <> me and locked_at is not null
     and locked_at > (now() at time zone 'Europe/Berlin') - make_interval(secs => p_timeout_s) then
    raise exception 'wird gerade von % gesendet', d ->> 'locked_by'
      using errcode = 'VS409', detail = to_jsonb(cur)::text;
  end if;
  d := d || jsonb_build_object('locked_by', me, 'locked_at', versand_jetzt(), 'status', 'wird gesendet…');
  update versand_auftraege set version = version + 1, data = d, updated_by = me, updated_at = versand_jetzt()
   where ref = p_ref returning * into cur;
  return cur;
end $$;

create or replace function public.versand_add(p_data jsonb) returns text
language plpgsql security definer set search_path = public as $$
declare r text := p_data ->> 'ref'; k text;
begin
  perform versand_pruefen();
  select kind into k from versand_ausgeblendet where ref = r;
  if found then return k; end if;
  insert into versand_auftraege (ref, version, data, created, updated_by, updated_at)
  values (r, 1, p_data, versand_jetzt(), versand_name(), versand_jetzt())
  on conflict (ref) do nothing;
  return case when found then 'added' else 'already' end;
end $$;

create or replace function public.versand_hide(p_ref text, p_kind text, p_data jsonb default null)
returns void
language plpgsql security definer set search_path = public as $$
begin
  perform versand_pruefen();
  insert into versand_ausgeblendet (ref, kind, by, at, data)
  values (p_ref, p_kind, versand_name(), versand_jetzt(), p_data)
  on conflict (ref) do update set kind = excluded.kind, by = excluded.by, at = excluded.at,
                                  data = coalesce(excluded.data, versand_ausgeblendet.data);
  delete from versand_auftraege where ref = p_ref;
end $$;

create or replace function public.versand_unhide(p_ref text) returns void
language plpgsql security definer set search_path = public as $$
begin
  perform versand_pruefen();
  delete from versand_ausgeblendet where ref = p_ref;
end $$;

create or replace function public.versand_settings_put(p_data jsonb) returns void
language plpgsql security definer set search_path = public as $$
begin
  perform versand_pruefen();
  insert into versand_einstellungen (id, data, geaendert_von, geaendert_am)
  values (1, p_data, versand_name(), versand_jetzt())
  on conflict (id) do update set data = excluded.data, geaendert_von = excluded.geaendert_von,
                                 geaendert_am = excluded.geaendert_am;
end $$;

create or replace function public.versand_log(p_zeitpunkt text, p_auftrag text, p_sendungsnummer text,
                                              p_parcel_id text, p_empfaenger text, p_hinweis text)
returns void
language plpgsql security definer set search_path = public as $$
begin
  perform versand_pruefen();
  insert into versand_protokoll (zeitpunkt, benutzer, auftrag, sendungsnummer, parcel_id, empfaenger, hinweis)
  values (p_zeitpunkt, versand_name(), p_auftrag, p_sendungsnummer, p_parcel_id, p_empfaenger, p_hinweis);
end $$;

-- Einmalige Übernahme aus dem Netzwerkordner. Vorhandenes bleibt unverändert (keine Überschreibung).
-- p: {"auftraege":[{ref,version,data,created,updated_by,updated_at}], "ausgeblendet":[{ref,kind,by,at,data}],
--     "einstellungen":{...} | null, "protokoll":[{zeitpunkt,benutzer,auftrag,sendungsnummer,parcel_id,empfaenger,hinweis}]}
create or replace function public.versand_import(p jsonb) returns jsonb
language plpgsql security definer set search_path = public as $$
declare n_a int; n_h int; n_p int := 0;
begin
  perform versand_pruefen();
  insert into versand_ausgeblendet (ref, kind, by, at, data)
  select x ->> 'ref', coalesce(x ->> 'kind', 'deleted'), x ->> 'by', x ->> 'at', x -> 'data'
    from jsonb_array_elements(coalesce(p -> 'ausgeblendet', '[]')) x
   where coalesce(x ->> 'ref', '') <> ''
  on conflict (ref) do nothing;
  get diagnostics n_h = row_count;
  insert into versand_auftraege (ref, version, data, created, updated_by, updated_at)
  select x ->> 'ref', coalesce((x ->> 'version')::int, 1), x -> 'data',
         coalesce(x ->> 'created', versand_jetzt()), x ->> 'updated_by', x ->> 'updated_at'
    from jsonb_array_elements(coalesce(p -> 'auftraege', '[]')) x
   where coalesce(x ->> 'ref', '') <> ''
     and not exists (select 1 from versand_ausgeblendet h where h.ref = x ->> 'ref')
  on conflict (ref) do nothing;
  get diagnostics n_a = row_count;
  if p -> 'einstellungen' is not null and jsonb_typeof(p -> 'einstellungen') = 'object'
     and not exists (select 1 from versand_einstellungen) then
    insert into versand_einstellungen (id, data, geaendert_von, geaendert_am)
    values (1, p -> 'einstellungen', versand_name(), versand_jetzt());
  end if;
  if jsonb_array_length(coalesce(p -> 'protokoll', '[]')) > 0 then
    insert into versand_protokoll (zeitpunkt, benutzer, auftrag, sendungsnummer, parcel_id, empfaenger, hinweis)
    select x ->> 'zeitpunkt', x ->> 'benutzer', x ->> 'auftrag', x ->> 'sendungsnummer', x ->> 'parcel_id',
           x ->> 'empfaenger', x ->> 'hinweis'
      from jsonb_array_elements(p -> 'protokoll') x
     where not exists (select 1 from versand_protokoll q
                        where q.zeitpunkt = x ->> 'zeitpunkt' and q.benutzer is not distinct from x ->> 'benutzer'
                          and q.auftrag is not distinct from x ->> 'auftrag'
                          and q.hinweis is not distinct from x ->> 'hinweis');
    get diagnostics n_p = row_count;
  end if;
  return jsonb_build_object('auftraege', n_a, 'ausgeblendet', n_h, 'protokoll', n_p);
end $$;

revoke all on function public.versand_put(text, integer, jsonb), public.versand_claim(text, integer),
  public.versand_add(jsonb), public.versand_hide(text, text, jsonb), public.versand_unhide(text),
  public.versand_settings_put(jsonb), public.versand_log(text, text, text, text, text, text),
  public.versand_import(jsonb), public.versand_signatur(), public.versand_ich(),
  public.versand_pruefen(), public.versand_erlaubt(), public.versand_stand_erhoehen() from public, anon;
grant execute on function public.versand_put(text, integer, jsonb), public.versand_claim(text, integer),
  public.versand_add(jsonb), public.versand_hide(text, text, jsonb), public.versand_unhide(text),
  public.versand_settings_put(jsonb), public.versand_log(text, text, text, text, text, text),
  public.versand_import(jsonb), public.versand_signatur(), public.versand_ich(),
  public.versand_erlaubt() to authenticated;

-- ---------------------------------------------------------------- Dateiablage (Labels, Retouren, Gewichte, Sendungsverfolgung)
insert into storage.buckets (id, name, public, file_size_limit)
values ('versand', 'versand', false, 20971520)
on conflict (id) do nothing;

drop policy if exists versand_lesen on storage.objects;
create policy versand_lesen on storage.objects for select to authenticated
  using (bucket_id = 'versand' and public.versand_erlaubt());
drop policy if exists versand_anlegen on storage.objects;
create policy versand_anlegen on storage.objects for insert to authenticated
  with check (bucket_id = 'versand' and public.versand_erlaubt());
drop policy if exists versand_aendern on storage.objects;
create policy versand_aendern on storage.objects for update to authenticated
  using (bucket_id = 'versand' and public.versand_erlaubt())
  with check (bucket_id = 'versand' and public.versand_erlaubt());
