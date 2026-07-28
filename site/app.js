(() => {
  const cfg = window.ASIA_RADAR_CONFIG || {};
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));
  const configReady = cfg.supabaseUrl && cfg.supabaseAnonKey && !String(cfg.supabaseUrl).includes("YOUR_") && !String(cfg.supabaseAnonKey).includes("YOUR_");
  let client = null, user = null, profile = null, monitors = [];

  function show(id, on = true) { $(id).classList.toggle("hidden", !on); }
  function message(text, good = false) { const el = $("authMessage"); el.textContent = text; el.className = "message" + (good ? " good" : ""); }
  function hashToken(value) {
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)).then(buf => [...new Uint8Array(buf)].map(x => x.toString(16).padStart(2, "0")).join(""));
  }
  const csv = value => value.split(",").map(x => x.trim().toUpperCase()).filter(Boolean);
  const dateFmt = value => value ? new Date(value + "T12:00:00").toLocaleDateString("pl-PL") : "—";

  async function init() {
    $("themeButton").onclick = () => { document.body.classList.toggle("dark"); localStorage.setItem("afr-theme", document.body.classList.contains("dark") ? "dark" : "light"); };
    if (localStorage.getItem("afr-theme") === "dark") document.body.classList.add("dark");
    $("signOutButton").onclick = () => client?.auth.signOut();
    $("telegramLoginButton").onclick = signInWithTelegram;
    if (!configReady || !window.supabase) { show("setupView"); return; }
    client = window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey);
    client.auth.onAuthStateChange((_event, session) => { user = session?.user || null; loadApp().catch(err => message(err.message)); });
    const { data } = await client.auth.getSession(); user = data.session?.user || null; await loadApp();
  }

  async function loadApp() {
    if (!user) { show("authView"); show("appView", false); show("signOutButton", false); return; }
    show("authView", false); show("appView"); show("signOutButton"); $("userBadge").textContent = user.user_metadata?.preferred_username ? `@${user.user_metadata.preferred_username}` : "Telegram";
    const { data, error } = await client.from("profiles").select("*").eq("id", user.id).single();
    if (error) throw error; profile = data;
    if (new URLSearchParams(location.search).get("invite")) {
      const token = new URLSearchParams(location.search).get("invite");
      const claimed = await client.rpc("claim_invite", { invite_token: token });
      if (claimed.data) { history.replaceState({}, "", location.pathname); profile.status = "active"; }
    }
    if (profile.status !== "active" && profile.role !== "admin") { renderBlocked(); return; }
    await syncTelegramConnection();
    await Promise.all([loadMonitors(), loadOffers()]);
    if (profile.role === "admin") { show("adminPanel"); await loadAdmin(); }
    $("newMonitorButton").onclick = () => $("monitorDialog").showModal();
    $("closeDialog").onclick = $("cancelDialog").onclick = () => $("monitorDialog").close();
    $("monitorForm").onsubmit = saveMonitor;
    $("refreshButton").onclick = () => Promise.all([loadMonitors(), loadOffers()]);
  }

  function renderBlocked() {
    $("statusStrip").innerHTML = `<strong>⏳ Konto oczekuje na aktywację</strong><span>Poproś administratora o zaproszenie lub sprawdź, czy użyłeś właściwego linku.</span>`;
    $("monitorList").innerHTML = `<div class="empty">Konto nie jest jeszcze aktywne.</div>`; $("offerList").innerHTML = "";
  }

  async function loadMonitors() {
    const { data, error } = await client.from("monitors").select("*").order("created_at", { ascending: false });
    if (error) throw error; monitors = data || []; $("monitorCount").textContent = `${monitors.length}/2 aktywne`;
    $("monitorList").innerHTML = monitors.length ? monitors.map(renderMonitor).join("") : `<div class="empty">Nie masz jeszcze monitoringu. Zacznij od gotowego profilu Premium Asia.</div>`;
    document.querySelectorAll("[data-action='pause']").forEach(btn => btn.onclick = () => updateMonitor(btn.dataset.id, { status: btn.dataset.status === "active" ? "paused" : "active" }));
    document.querySelectorAll("[data-action='delete']").forEach(btn => btn.onclick = () => deleteMonitor(btn.dataset.id));
  }

  function renderMonitor(m) {
    const f = m.filters || {}, r = m.telegram_rules || {}, status = m.status === "active" ? "Aktywny" : "Wstrzymany";
    const route = `${(f.origins || []).join(", ")} → ${(f.destinations || []).join(", ")}`;
    return `<article class="monitor-card"><div class="card-top"><div><h3>${esc(m.name)}</h3><div class="card-meta">${esc(route)} · ${esc(f.cabin || "BUSINESS")} · ${dateFmt(f.from)}–${dateFmt(f.to)}</div></div><span class="pill">${status}</span></div><div class="card-meta">Do ${esc(f.budget_pln || "—")} PLN · maks. ${esc(f.max_duration_h || 24)}h · Telegram od ${esc(r.min_stars || 4)}⭐</div><div class="card-actions"><button data-action="pause" data-id="${m.id}" data-status="${m.status}">${m.status === "active" ? "Wstrzymaj" : "Wznów"}</button><button data-action="delete" data-id="${m.id}">Usuń</button></div></article>`;
  }

  async function saveMonitor(e) {
    e.preventDefault(); if (monitors.length >= 2) { $("formMessage").textContent = "Limit dwóch monitorów na osobę."; return; }
    const f = { origins: csv($("monitorOrigins").value), destinations: csv($("monitorDestinations").value), from: $("monitorFrom").value, to: $("monitorTo").value, cabin: $("monitorCabin").value, budget_pln: Number($("monitorBudget").value), max_duration_h: Number($("monitorDuration").value), max_stops: Number($("monitorStops").value) };
    const r = { min_stars: Number($("telegramStars").value), drop_percent: Number($("telegramDrop").value), immediate_new_low: $("telegramImmediate").checked };
    const payload = { user_id: user.id, name: $("monitorName").value.trim(), filters: f, app_rules: { min_stars: 1 }, telegram_rules: r, expires_at: f.to || null };
    const { error } = await client.from("monitors").insert(payload); $("formMessage").textContent = error ? error.message : "Zapisano."; if (!error) { $("monitorDialog").close(); $("monitorForm").reset(); await loadMonitors(); }
  }
  async function updateMonitor(id, patch) { const { error } = await client.from("monitors").update(patch).eq("id", id); if (error) alert(error.message); else await loadMonitors(); }
  async function deleteMonitor(id) { if (!confirm("Usunąć ten monitoring?")) return; const { error } = await client.from("monitors").delete().eq("id", id); if (error) alert(error.message); else await loadMonitors(); }

  async function loadOffers() {
    const { data, error } = await client.from("user_matches").select("id, stars, feedback, notified_at, updated_at, flight_offers(route,travel_date,airline_name,price_pln,cabin,duration_minutes,stops,link,source,tags)").eq("visible", true).order("updated_at", { ascending: false }).limit(40);
    if (error) throw error; const offers = data || []; $("offerList").innerHTML = offers.length ? offers.map(renderOffer).join("") : `<div class="empty">Brak dopasowanych ofert. Skaner uzupełni je podczas kolejnych przebiegów.</div>`;
    document.querySelectorAll("[data-feedback]").forEach(btn => btn.onclick = () => sendFeedback(btn.dataset.feedback, btn.dataset.match));
    const last = offers[0]?.updated_at; $("statusStrip").innerHTML = `<span>🔎 <strong>Ostatni wynik:</strong> ${last ? new Date(last).toLocaleString("pl-PL") : "brak"}</span><span>⏱ Skan Google: co 3 godziny</span><span>🔐 Wyniki tylko dla Ciebie</span>`;
  }
  function renderOffer(m) { const o = m.flight_offers || {}; const mins = Number(o.duration_minutes || 0); const duration = mins ? `${Math.floor(mins / 60)}h ${mins % 60}m` : "—"; return `<article class="offer-card"><div class="card-top"><div><div class="stars">${"⭐".repeat(m.stars || 1)}</div><div class="route">${esc(o.route)}</div><div class="card-meta">✈ ${esc(o.airline_name)} · ${esc(o.cabin)} · ${dateFmt(o.travel_date)}</div></div><div class="price">${Number(o.price_pln || 0).toLocaleString("pl-PL")} PLN</div></div><div class="card-meta">${duration} · ${o.stops === 0 ? "bez przesiadek" : `${o.stops ?? "?"} przes.`} · ${esc(o.source)}</div><div class="card-actions"><button data-feedback="buy" data-match="${m.id}">👍 Kupiłbym</button><button data-feedback="expensive" data-match="${m.id}">💸 Za drogo</button><button data-feedback="skip" data-match="${m.id}">🙅 Nie</button></div><a href="${esc(o.link)}" target="_blank" rel="noopener">Otwórz ofertę →</a></article>`; }
  async function sendFeedback(verdict, matchId) { const { error } = await client.from("feedback").insert({ user_id: user.id, match_id: matchId, verdict }); if (!error) { const label = { buy: "Kupiłbym", expensive: "Za drogo", skip: "Pominięto" }[verdict] || verdict; alert(`Zapisano: ${label}`); } }
  async function signInWithTelegram() {
    const provider = cfg.telegramAuthProvider || "custom:telegram";
    const redirectTo = location.origin + location.pathname + location.search;
    const { error } = await client.auth.signInWithOAuth({ provider, options: { redirectTo } });
    if (error) message(`Nie udało się uruchomić logowania Telegram: ${error.message}`);
  }
  async function syncTelegramConnection() {
    const metadata = user.user_metadata || {};
    const telegramId = String(metadata.id || metadata.sub || "").trim();
    if (!telegramId) return;
    const username = String(metadata.preferred_username || metadata.username || "").trim();
    const { error } = await client.rpc("sync_telegram_connection", { telegram_chat_id: telegramId, telegram_username: username });
    if (error) console.warn("Nie udało się zsynchronizować Telegrama", error.message);
  }

  async function loadAdmin() {
    const { data, error } = await client.from("profiles").select("id,email,display_name,telegram_user_id,status,role,last_seen_at,created_at").order("created_at"); if (error) throw error;
    const users = data || []; $("seatCount").textContent = `${users.filter(x => x.status === "active").length}/10 aktywnych`; $("userList").innerHTML = users.map(u => `<div class="user-row"><span><strong>${esc(u.display_name || u.email || "Telegram użytkownik")}</strong><br><small class="muted">Telegram ID: ${esc(u.telegram_user_id || "—")}</small></span><span class="status-${esc(u.status)}">${esc(u.status)}</span>${u.id === user.id ? "<span class='muted'>Ty</span>" : `<button class="secondary" data-user="${u.id}" data-status="${u.status}" data-action="toggle-user">${u.status === "active" ? "Zawieś" : "Aktywuj"}</button><button class="danger" data-user="${u.id}" data-action="delete-user">Usuń</button>`}</div>`).join("");
    document.querySelectorAll("[data-action='toggle-user']").forEach(btn => btn.onclick = async () => { const next = btn.dataset.status === "active" ? "suspended" : "active"; const result = await client.from("profiles").update({ status: next }).eq("id", btn.dataset.user); if (result.error) alert(result.error.message); else await loadAdmin(); });
    document.querySelectorAll("[data-action='delete-user']").forEach(btn => btn.onclick = async () => { if (!confirm("Usunąć konto, jego monitory i alerty? Tego nie można cofnąć.")) return; const result = await client.from("profiles").delete().eq("id", btn.dataset.user); if (result.error) alert(result.error.message); else await loadAdmin(); });
    $("inviteButton").onclick = createInvite;
  }
  async function createInvite() { const token = crypto.randomUUID().replaceAll("-", ""); const hash = await hashToken(token); const { error } = await client.from("invites").insert({ token_hash: hash, created_by: user.id }); if (error) { $("inviteOutput").textContent = error.message; return; } const link = `${location.origin}${location.pathname}?invite=${token}`; $("inviteOutput").innerHTML = `Skopiuj: <a href="${esc(link)}">${esc(link)}</a>`; }
  init().catch(err => { if (configReady) { show("authView"); message(`Nie udało się uruchomić panelu: ${err.message}`); } });
})();
