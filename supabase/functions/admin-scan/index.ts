import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.110.9';

const json = (body: Record<string, unknown>, corsHeaders: Record<string, string>, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { ...corsHeaders, 'Content-Type': 'application/json' }
});

Deno.serve(async (request) => {
  const allowedOrigin = Deno.env.get('APP_ORIGIN') || 'https://kyudo4.github.io';
  const origin = request.headers.get('origin') || '';
  const corsHeaders = {
    'Access-Control-Allow-Origin': allowedOrigin,
    'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Vary': 'Origin'
  };
  if (origin !== allowedOrigin) return json({ error: 'Niedozwolone źródło żądania.' }, corsHeaders, 403);
  if (request.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders });
  if (request.method !== 'POST') return json({ error: 'Method not allowed' }, corsHeaders, 405);

  try {
    const authHeader = request.headers.get('authorization') || '';
    const accessToken = authHeader.startsWith('Bearer ') ? authHeader.slice(7).trim() : '';
    const supabaseUrl = Deno.env.get('SUPABASE_URL');
    const serviceRoleKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
    const githubToken = Deno.env.get('GITHUB_ACTIONS_TOKEN');
    const repository = Deno.env.get('GITHUB_REPOSITORY') || 'kyudo4/flight-radar-by-kyudo';
    const workflow = Deno.env.get('GITHUB_WORKFLOW_ID') || 'scan.yml';
    if (!accessToken || !supabaseUrl || !serviceRoleKey || !githubToken) {
      return json({ error: 'Brak konfiguracji ręcznego skanu.' }, corsHeaders, 500);
    }

    const admin = createClient(supabaseUrl, serviceRoleKey, { auth: { autoRefreshToken: false, persistSession: false } });
    const { data: authData, error: authError } = await admin.auth.getUser(accessToken);
    if (authError || !authData.user) return json({ error: 'Sesja jest nieprawidłowa lub wygasła.' }, corsHeaders, 401);
    const { data: profile, error: profileError } = await admin.from('profiles').select('role,status').eq('id', authData.user.id).single();
    if (profileError || profile?.role !== 'admin' || profile?.status !== 'active') {
      return json({ error: 'Brak uprawnień administratora.' }, corsHeaders, 403);
    }

    let reservedRun: string | null = null;
    const { data: rpcRun, error: reserveError } = await admin.rpc('reserve_scan_slot');
    if (!reserveError) {
      reservedRun = rpcRun as string | null;
      if (!reservedRun) return json({ error: 'Skan już trwał lub był uruchomiony w ciągu ostatnich 10 minut.' }, corsHeaders, 429);
    } else {
      // Compatibility fallback for projects where the new migration has not
      // been applied yet. It keeps the old guard working during rollout;
      // once the RPC exists, the transactional reservation above is used.
      console.warn('reserve_scan_slot is not available yet; using compatibility guard', reserveError.message);
      const recentCutoff = new Date(Date.now() - 10 * 60 * 1000).toISOString();
      const { data: recentRuns, error: runsError } = await admin.from('scan_runs')
        .select('started_at,status').gte('started_at', recentCutoff).order('started_at', { ascending: false }).limit(1);
      if (runsError) throw runsError;
      if (recentRuns?.length) return json({ error: 'Skan już trwał lub był uruchomiony w ciągu ostatnich 10 minut.' }, corsHeaders, 429);
    }

    const githubResponse = await fetch(`https://api.github.com/repos/${repository}/actions/workflows/${workflow}/dispatches`, {
      method: 'POST',
      headers: {
        Accept: 'application/vnd.github+json',
        Authorization: `Bearer ${githubToken}`,
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        ref: 'main',
        ...(reservedRun ? { inputs: { reserved_run_id: reservedRun } } : {})
      })
    });
    if (!githubResponse.ok) {
      console.error('GitHub workflow dispatch failed with status', githubResponse.status);
      if (reservedRun) {
        await admin.from('scan_runs').update({ status: 'error', finished_at: new Date().toISOString(), error: `GitHub HTTP ${githubResponse.status}` }).eq('id', reservedRun);
      }
      return json({ error: 'GitHub nie przyjął żądania uruchomienia skanu.' }, corsHeaders, 502);
    }
    return json({ ok: true }, corsHeaders, 202);
  } catch (error) {
    console.error('admin-scan failed', error);
    return json({ error: 'Nie udało się uruchomić skanu.' }, corsHeaders, 500);
  }
});
