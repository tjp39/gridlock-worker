-- Gridlock: week 1 tables
-- Paste this whole file into Supabase → SQL Editor → New query → Run.
-- All times are stored as timestamptz. AEMO publishes in market time (AEST, UTC+10, no daylight saving).

-- 1. Regional price + demand, one row per region per 5-minute interval
create table if not exists prices (
  interval_end        timestamptz not null,
  region              text        not null,          -- NSW1, QLD1, VIC1, SA1, TAS1
  rrp                 numeric,                       -- energy price, $/MWh
  total_demand        numeric,                       -- MW
  available_gen       numeric,                       -- MW
  dispatchable_gen    numeric,                       -- MW
  net_interchange     numeric,                       -- MW, positive = exporting
  raise1sec_rrp numeric, raise6sec_rrp numeric, raise60sec_rrp numeric, raise5min_rrp numeric, raisereg_rrp numeric,
  lower1sec_rrp numeric, lower6sec_rrp numeric, lower60sec_rrp numeric, lower5min_rrp numeric, lowerreg_rrp numeric,
  inserted_at         timestamptz not null default now(),
  primary key (interval_end, region)
);

-- 2. Interconnector flows and limits
create table if not exists interconnectors (
  interval_end        timestamptz not null,
  interconnector_id   text        not null,          -- e.g. V-SA (Heywood), V-S-MNSP1 (Murraylink), T-V-MNSP1 (Basslink)
  mw_flow             numeric,
  metered_mw_flow     numeric,
  export_limit        numeric,
  import_limit        numeric,
  marginal_value      numeric,                       -- non-zero = flow is constrained
  inserted_at         timestamptz not null default now(),
  primary key (interval_end, interconnector_id)
);

-- 3. Binding constraints only (marginal value <> 0), keeps the table small
create table if not exists binding_constraints (
  interval_end        timestamptz not null,
  constraint_id       text        not null,
  rhs                 numeric,
  marginal_value      numeric,                       -- $/MW cost of the constraint
  violation_degree    numeric,
  inserted_at         timestamptz not null default now(),
  primary key (interval_end, constraint_id)
);

create index if not exists prices_region_time_idx on prices (region, interval_end desc);
create index if not exists ic_time_idx on interconnectors (interval_end desc);
create index if not exists bc_time_idx on binding_constraints (interval_end desc);

-- Row Level Security: the public website may READ, only the worker (service role key) may WRITE.
alter table prices              enable row level security;
alter table interconnectors     enable row level security;
alter table binding_constraints enable row level security;

drop policy if exists "public read" on prices;
drop policy if exists "public read" on interconnectors;
drop policy if exists "public read" on binding_constraints;

create policy "public read" on prices              for select using (true);
create policy "public read" on interconnectors     for select using (true);
create policy "public read" on binding_constraints for select using (true);
