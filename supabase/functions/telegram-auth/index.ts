import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.110.9';
import { createRemoteJWKSet, jwtVerify } from 'npm:jose@5.10.0';

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
  'Access-Control-Allow-Methods': 'POST, OPTIONS'
};

const json = (body: Record<string, unknown>, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { ...corsHeaders, 'Content-Type': 'application/json' }
});

const jwks = createRemoteJWKSet(new URL('https://oauth.telegram.org/.well-known/jwks.json'));

Deno.serve(async (request) => {
  if (request.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders });
  if (request.method !== 'POST') return json({ error: 'Method not allowed' }, 405);

  try {
    const { id_token: idToken } = await request.json();
    if (typeof idToken !== 'string' || idToken.length < 1000 || idToken.length > 20000) {
      return json({ error: 'Nieprawidłowy token Telegrama.' }, 400);
    }

    const clientId = Deno.env.get('TELEGRAM_CLIENT_ID');
    const supabaseUrl = Deno.env.get('SUPABASE_URL');
    const serviceRoleKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
    if (!clientId || !supabaseUrl || !serviceRoleKey) {
      return json({ error: 'Brak konfiguracji funkcji logowania.' }, 500);
    }

    const verified = await jwtVerify(idToken, jwks, {
      issuer: 'https://oauth.telegram.org',
      audience: clientId
    });
    const claims = verified.payload;
    const telegramId = String(claims.id ?? claims.sub ?? '').trim();
    if (!telegramId) return json({ error: 'Telegram nie zwrócił identyfikatora użytkownika.' }, 400);

    const email = `telegram-${telegramId}@auth.flight-radar.invalid`;
    const password = crypto.randomUUID() + crypto.randomUUID() + 'A9!';
    const metadata = {
      id: telegramId,
      sub: String(claims.sub ?? telegramId),
      name: String(claims.name ?? '').trim(),
      given_name: String(claims.given_name ?? '').trim(),
      family_name: String(claims.family_name ?? '').trim(),
      preferred_username: String(claims.preferred_username ?? '').trim(),
      picture: String(claims.picture ?? '').trim()
    };

    const admin = createClient(supabaseUrl, serviceRoleKey, {
      auth: { autoRefreshToken: false, persistSession: false }
    });
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

    return json({ email, password, user_id: authUser.id });
  } catch (error) {
    console.error('telegram-auth failed', error);
    return json({ error: 'Logowanie Telegram nie powiodło się.' }, 401);
  }
});
