import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.110.9';

const json = (body: Record<string, unknown>, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' }
});

const safeEqual = (left: string, right: string) => {
  if (!left || left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
};

Deno.serve(async (request) => {
  if (request.method !== 'POST') return json({ error: 'Method not allowed' }, 405);

  const expectedSecret = Deno.env.get('TELEGRAM_WEBHOOK_SECRET') || '';
  const suppliedSecret = request.headers.get('x-telegram-bot-api-secret-token') || '';
  if (!safeEqual(suppliedSecret, expectedSecret)) return json({ error: 'Forbidden' }, 403);

  const contentLength = Number(request.headers.get('content-length') || 0);
  if (contentLength > 65536) return json({ error: 'Payload too large' }, 413);

  const supabaseUrl = Deno.env.get('SUPABASE_URL');
  const serviceRoleKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
  const botToken = Deno.env.get('TG_BOT_TOKEN');
  if (!supabaseUrl || !serviceRoleKey || !botToken) {
    console.error('telegram-feedback-webhook is missing server configuration');
    return json({ error: 'Server configuration error' }, 500);
  }

  try {
    const update = await request.json();
    const callback = update?.callback_query;
    if (!callback) return json({ ok: true });

    const callbackId = String(callback.id || '').trim();
    const callbackData = String(callback.data || '').trim();
    const chatId = String(callback.message?.chat?.id || '').trim();
    const parts = callbackData.split('|', 3);
    const matchId = parts.length === 3 && parts[0] === 'fb' ? parts[1] : '';
    const verdict = parts.length === 3 ? parts[2] : '';
    const allowedVerdicts = new Set(['buy', 'expensive', 'skip', 'toolong', 'badairline']);
    const validMatch = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(matchId);
    const validChat = /^-?[0-9]{1,20}$/.test(chatId);

    const answerCallback = async (text: string) => {
      if (!callbackId) return;
      const response = await fetch(`https://api.telegram.org/bot${botToken}/answerCallbackQuery`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ callback_query_id: callbackId, text })
      });
      if (!response.ok) throw new Error(`Telegram answerCallbackQuery HTTP ${response.status}`);
    };

    if (!validMatch || !validChat || !allowedVerdicts.has(verdict)) {
      await answerCallback('Nieprawidłowa odpowiedź');
      return json({ ok: true });
    }

    const admin = createClient(supabaseUrl, serviceRoleKey, {
      auth: { autoRefreshToken: false, persistSession: false }
    });
    const connection = await admin.from('telegram_connections')
      .select('user_id').eq('chat_id', chatId).maybeSingle();
    if (connection.error) throw connection.error;

    let saved = false;
    if (connection.data?.user_id) {
      const match = await admin.from('user_matches').select('id')
        .eq('id', matchId).eq('user_id', connection.data.user_id).maybeSingle();
      if (match.error) throw match.error;
      if (match.data) {
        const feedback = await admin.from('feedback').upsert({
          user_id: connection.data.user_id,
          match_id: matchId,
          verdict
        }, { onConflict: 'user_id,match_id' });
        if (feedback.error) throw feedback.error;
        const updated = await admin.from('user_matches').update({ feedback: verdict })
          .eq('id', matchId).eq('user_id', connection.data.user_id);
        if (updated.error) throw updated.error;
        saved = true;
      }
    }

    await answerCallback(saved ? 'Zapisano' : 'Nie znaleziono powiązanej oferty');
    return json({ ok: true, saved });
  } catch (error) {
    console.error('telegram-feedback-webhook failed', error);
    return json({ error: 'Temporary processing error' }, 500);
  }
});
