-- The monitor validation trigger calls this pure date-counting helper while
-- an authenticated user creates or edits a monitor. Keep direct access closed
-- to anonymous users, but allow the authenticated trigger path to execute.

revoke execute on function public.valid_round_trip_pair_count(date, date, date, date) from public, anon;
grant execute on function public.valid_round_trip_pair_count(date, date, date, date) to authenticated;
