import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.110.9';
import { createRemoteJWKSet, jwtVerify } from 'https://esm.sh/jose@5.10.0';

const json = (body: Record<string, unknown>, corsHeaders: Record<string, string>, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { ...corsHeaders, 'Content-Type': 'application/json' }
});

const jwks = createRemoteJWKSet(new URL('https://oauth.telegram.org/.well-known/jwks.json'));

Deno.serve(async (request) => {
  const allowedOrigin = Deno.env.get('APP_ORIGIN') || 'https://kyudo4.github.io';
  const origin = request.headers.get('origin') || '';
  const corsHeaders = {
    'Access-Control-Allow-Origin': allowedOrigin,
    'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Vary': 'Origin'
  };
  if (origin !== allowedOrigin) {
    return json({ error: 'Niedozwolone źródło żądania.' }, corsHeaders, 403);
  }
  if (request.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders });
  if (request.method !== 'POST') return json({ error: 'Method not allowed' }, corsHeaders, 405);

  try {
    const { id_token: rawIdToken } = await request.json();
    const idToken = typeof rawIdToken === 'string' ? rawIdToken.trim() : '';
    if (!idToken || idToken.length > 20000 || idToken.split('.').length !== 3) {
      return json({ error: 'Nieprawidłowy token Telegrama.' }, corsHeaders, 400);
    }

    const clientId = Deno.env.get('TELEGRAM_CLIENT_ID');
    const supabaseUrl = Deno.env.get('SUPABASE_URL');
    const serviceRoleKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
    if (!clientId || !supabaseUrl || !serviceRoleKey) {
      return json({ error: 'Brak konfiguracji funkcji logowania.' }, corsHeaders, 500);
    }

    const verified = await jwtVerify(idToken, jwks, {
      issuer: 'https://oauth.telegram.org',
      audience: clientId
    });
    const claims = verified.payload;
    const telegramId = String(claims.id ?? claims.sub ?? '').trim();
    if (!telegramId) return json({ error: 'Telegram nie zwrócił identyfikatora użytkownika.' }, corsHeaders, 400);

    const admin = createClient(supabaseUrl, serviceRoleKey, {
      auth: { autoRefreshToken: false, persistSession: false }
    });
    const now = Date.now();
    const windowStart = new Date(now - 15 * 60 * 1000).toISOString();
    const attempts = await admin.from('telegram_auth_attempts')
      .select('id', { count: 'exact', head: true })
      .eq('telegram_user_id', telegramId)
      .gte('attempted_at', windowStart);
    if (attempts.error) throw attempts.error;
    if ((attempts.count || 0) >= 10) {
      return json({ error: 'Za dużo prób logowania. Spróbuj ponownie za 15 minut.' }, corsHeaders, 429);
    }
    const recorded = await admin.from('telegram_auth_attempts').insert({ telegram_user_id: telegramId });
    if (recorded.error) throw recorded.error;
    await admin.from('telegram_auth_attempts').delete()
      .lt('attempted_at', new Date(now - 24 * 60 * 60 * 1000).toISOString());

    const email = `telegram-${telegramId}@auth.flight-radar.invalid`;
    const password = crypto.randomUUID().replaceAll('-', '')
      + crypto.randomUUID().replaceAll('-', '')
      + 'A9!';
    const metadata = {
      id: telegramId,
      sub: String(claims.sub ?? telegramId),
      name: String(claims.name ?? '').trim(),
      given_name: String(claims.given_name ?? '').trim(),
      family_name: String(claims.family_name ?? '').trim(),
      preferred_username: String(claims.preferred_username ?? '').trim(),
      picture: String(claims.picture ?? '').trim()
    };

    const listed = await admin.auth.admin.listUsers({ page: 1, perPage: 1000 });
    if (listed.error) throw listed.error;
    const existing = listed.data.users.find((candidate) => candidate.email === email);

    let authUser;
    if (existing) {
      const updated = await admin.auth.admin.updateUserById(existing.id, {
        password,
        user_metadata: { ...existing.user_metadata, ...metadata }
      });
      if (updated.error) throw updated.error;
      authUser = updated.data.user;
    } else {
      const created = await admin.auth.admin.createUser({
        email,
        password,
        email_confirm: true,
        user_metadata: metadata
      });
      if (created.error) throw created.error;
      authUser = created.data.user;
    }

    return json({ email, password, user_id: authUser.id }, corsHeaders);
  } catch (error) {
    console.error('telegram-auth failed', error);
    return json({ error: 'Logowanie Telegram nie powiodło się.' }, corsHeaders, 401);
  }
});
