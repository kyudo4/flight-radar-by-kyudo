-- The airport directory previously labelled MXA as plain "Manila", which
-- made it sort before Manila Ninoy Aquino (MNL). Repair the affected monitor
-- without touching legitimate MXA selections outside this exact scope.

update public.monitors monitor
set filters = jsonb_set(
  monitor.filters,
  '{destinations}',
  to_jsonb(array(
    select case when upper(value) = 'MXA' then 'MNL' else upper(value) end
    from jsonb_array_elements_text(monitor.filters -> 'destinations') with ordinality as destination(value, position)
    order by position
  )),
  true
),
next_scan_at = now()
where lower(trim(monitor.name)) = 'filipiny'
  and monitor.filters -> 'origins' @> '["WAW"]'::jsonb
  and monitor.filters -> 'destinations' @> '["MXA"]'::jsonb
  and monitor.filters -> 'destinations' @> '["CEB"]'::jsonb;

-- The scanner recreates the corrected MNL rows atomically at the start of
-- the targeted catch-up run. Remove only the obsolete MXA queue rows now.
delete from public.monitor_scan_items item
using public.monitors monitor
where item.monitor_id = monitor.id
  and lower(trim(monitor.name)) = 'filipiny'
  and item.destination = 'MXA';
