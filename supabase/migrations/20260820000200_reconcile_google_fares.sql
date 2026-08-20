-- After restoring historical one-way Google fares, immediately assign every
-- matching current offer to active monitors. Without this step, an offer
-- found for another monitor could remain absent from a user's dashboard until
-- the next source query happened to revisit the route.

do $$
declare
  monitor_row record;
begin
  for monitor_row in
    select id from public.monitors where status = 'active'
  loop
    perform public.reconcile_monitor_offers(monitor_row.id);
  end loop;
end;
$$;
