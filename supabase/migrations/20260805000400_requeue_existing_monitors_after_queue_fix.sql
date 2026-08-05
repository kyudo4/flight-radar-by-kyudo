-- One-time repair for monitors created before the edit-generation fix.
-- Their queue rows may still carry a future next_scan_at from the old logic.

update public.monitor_scan_items items
set last_scanned_at = null,
    next_scan_at = now()
from public.monitors monitor
where monitor.id = items.monitor_id
  and monitor.status = 'active';

update public.monitors monitor
set last_scanned_at = null,
    next_scan_at = now()
where monitor.status = 'active'
  and exists (
    select 1 from public.monitor_scan_items items
    where items.monitor_id = monitor.id
  );
