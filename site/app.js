(() => {
  const cfg = window.ASIA_RADAR_CONFIG || {};
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));
  const safeHref = value => /^https?:\/\//i.test(String(value || "")) ? esc(value) : "#";
  const configReady = cfg.supabaseUrl && cfg.supabaseAnonKey && !String(cfg.supabaseUrl).includes("YOUR_") && !String(cfg.supabaseAnonKey).includes("YOUR_");
  let client = null, user = null, profile = null, monitors = [], offers = [];
  let editingMonitorId = null;

  const AIRPORTS = { GDN: "Gdańsk", WAW: "Warszawa", POZ: "Poznań", OSL: "Oslo", ARN: "Sztokholm", CPH: "Kopenhaga", VIE: "Wiedeń", BUD: "Budapeszt", MXP: "Mediolan", IST: "Stambuł", BKK: "Bangkok", SIN: "Singapur", KUL: "Kuala Lumpur", HKG: "Hongkong", HAN: "Hanoi", SGN: "Ho Chi Minh", HND: "Tokio (Haneda)", NRT: "Tokio (Narita)", ICN: "Seul" };
  const CABINS = { BUSINESS: "Business", FIRST: "First", PREMIUM_ECONOMY: "Premium Economy", ECONOMY: "Economy" };

  function show(id, on = true) { $(id).classList.toggle("hidden", !on); }
  function message(text, good = false) { const el = $("authMessage"); el.textContent = text; el.className = "message" + (good ? " good" : ""); }
  function hashToken(value) {
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)).then(buf => [...new Uint8Array(buf)].map(x => x.toString(16).padStart(2, "0")).join(""));
  }
  const csv = value => value.split(",").map(x => x.trim().toUpperCase()).filter(Boolean);
  const dateFmt = value => value ? new Date(value + "T12:00:00").toLocaleDateString("pl-PL") : "—";
  const dateTimeFmt = value => value ? new Date(value).toLocaleString("pl-PL", { dateStyle: "short", timeStyle: "short" }) : "jeszcze nie sprawdzono";
  const airportName = value => AIRPORTS[String(value || "").toUpperCase()] || String(value || "");
  const routeName = value => String(value || "").split(/\s*→\s*/).map(airportName).join(" → ");
  const monitorCabins = filters => Array.isArray(filters?.cabins) && filters.cabins.length ? filters.cabins : (filters?.cabin ? [filters.cabin] : []);

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
    if (!user) { show("authView"); show("appView", false); show("appTabs", false); show("signOutButton", false); return; }
    show("authView", false); show("appView"); show("signOutButton"); $("userBadge").textContent = user.user_metadata?.preferred_username ? `@${user.user_metadata.preferred_username}` : "Telegram";
    const { data, error } = await client.from("profiles").select("*").eq("id", user.id).single();
    if (error) throw error; profile = data;
    if (new URLSearchParams(location.search).get("invite")) {
      const token = new URLSearchParams(location.search).get("invite");
      const claimed = await client.rpc("claim_invite", { invite_token: token });
      if (claimed.data) { history.replaceState({}, "", location.pathname); profile.status = "active"; }
    }
    if (profile.status !== "active") { show("appTabs", false); show("adminTab", false); show("adminView", false); showAppTab("radar"); renderBlocked(); return; }
    show("appTabs");
    show("adminTab", profile.role === "admin");
    $("radarTab").onclick = () => showAppTab("radar");
    $("adminTab").onclick = () => showAppTab("admin");
    $("alertsSectionTab").onclick = () => focusRadarSection("alerts");
    $("monitorsSectionTab").onclick = () => focusRadarSection("monitors");
    showAppTab("radar");
    await syncTelegramConnection();
    await Promise.all([loadMonitors(), loadOffers()]);
    if (profile.role === "admin") await loadAdmin();
    $("newMonitorButton").onclick = () => openMonitorDialog();
    $("closeDialog").onclick = $("cancelDialog").onclick = () => $("monitorDialog").close();
    $("monitorForm").onsubmit = saveMonitor;
    $("refreshButton").onclick = () => Promise.all([loadMonitors(), loadOffers()]);
    ["offerSearch", "offerCabinFilter", "offerStarsFilter", "offerSort"].forEach(id => $(id).oninput = renderOffers);
  }

  function renderBlocked() {
    const suspended = profile?.status === "suspended";
    $("statusStrip").innerHTML = `<strong>${suspended ? "⛔ Konto zawieszone" : "⏳ Konto oczekuje na aktywację"}</strong><span>${suspended ? "Skontaktuj się z administratorem." : "Poproś administratora o zaproszenie lub sprawdź, czy użyłeś właściwego linku."}</span>`;
    $("monitorList").innerHTML = `<div class="empty">${suspended ? "Dostęp do aplikacji jest obecnie wyłączony." : "Konto nie jest jeszcze aktywne."}</div>`; $("offerList").innerHTML = "";
  }

  function showAppTab(tab) {
    const adminVisible = tab === "admin" && profile?.role === "admin";
    show("radarView", !adminVisible);
    show("adminView", adminVisible);
    $("radarTab").classList.toggle("active", !adminVisible);
    $("adminTab").classList.toggle("active", adminVisible);
  }

  function focusRadarSection(section) {
    const sectionId = section === "monitors" ? "monitorsSection" : "alertsSection";
    document.getElementById(sectionId)?.scrollIntoView({ behavior: "smooth", block: "start" });
    $("alertsSectionTab").classList.toggle("active", section !== "monitors");
    $("monitorsSectionTab").classList.toggle("active", section === "monitors");
  }

  async function loadMonitors() {
    const { data, error } = await client.from("monitors").select("*").order("created_at", { ascending: false });
    if (error) throw error; monitors = data || []; $("monitorCount").textContent = `${monitors.length}/2 wykorzystane`;
    $("monitorList").innerHTML = monitors.length ? monitors.map(renderMonitor).join("") : `<div class="empty">Czysta karta. Ustaw własne kryteria, aby uruchomić pierwszy monitoring.</div>`;
    document.querySelectorAll("[data-action='edit']").forEach(btn => btn.onclick = () => openMonitorDialog(monitors.find(m => m.id === btn.dataset.id)));
    document.querySelectorAll("[data-action='pause']").forEach(btn => btn.onclick = () => updateMonitor(btn.dataset.id, { status: btn.dataset.status === "active" ? "paused" : "active" }));
    document.querySelectorAll("[data-action='delete']").forEach(btn => btn.onclick = () => deleteMonitor(btn.dataset.id));
  }

  function renderMonitor(m) {
    const f = m.filters || {}, r = m.telegram_rules || {};
    const cabins = monitorCabins(f);
    const cabinLabel = cabins.map(cabin => CABINS[cabin] || cabin).join(", ");
    const route = `${(f.origins || []).map(airportName).join(", ")} → ${(f.destinations || []).map(airportName).join(", ")}`;
    const nextScan = m.status === "active" ? `Następny skan: ${dateTimeFmt(m.next_scan_at)}` : "Skan wstrzymany";
    const airlineRules = [(f.preferred_airlines || []).length ? `Preferowane: ${(f.preferred_airlines || []).join(", ")}` : "", (f.excluded_airlines || []).length ? `Wykluczone: ${(f.excluded_airlines || []).join(", ")}` : ""].filter(Boolean).join(" · ");
    return `<article class="monitor-card"><div><h3>${esc(m.name)}</h3><div class="card-meta">${esc(route)} · ${esc(cabinLabel || "—")} · ${dateFmt(f.from)}–${dateFmt(f.to)}</div></div><div class="card-meta">Do ${esc(f.budget_pln ?? "—")} PLN · maks. ${esc(f.max_duration_h ?? "—")}h · Telegram od ${esc(r.min_stars ?? "—")}⭐</div>${airlineRules ? `<div class="card-meta">${esc(airlineRules)}</div>` : ""}<div class="card-meta">Ostatni skan: ${esc(dateTimeFmt(m.last_scanned_at))} · ${esc(nextScan)}</div><div class="card-actions"><button data-action="edit" data-id="${m.id}">Edytuj</button><button data-action="pause" data-id="${m.id}" data-status="${m.status}">${m.status === "active" ? "Wstrzymaj" : "Wznów"}</button><button data-action="delete" data-id="${m.id}">Usuń</button></div></article>`;
  }

  function openMonitorDialog(monitor = null) {
    editingMonitorId = monitor?.id || null;
    const f = monitor?.filters || {}, r = monitor?.telegram_rules || {};
    $("monitorDialogEyebrow").textContent = editingMonitorId ? "Edycja monitora" : "Nowy monitor";
    $("monitorDialogTitle").textContent = editingMonitorId ? "Zmień własne kryteria" : "Ustaw własne kryteria";
    $("monitorName").value = monitor?.name || "";
    $("monitorOrigins").value = (f.origins || []).join(", "); $("monitorDestinations").value = (f.destinations || []).join(", ");
    $("monitorFrom").value = f.from || ""; $("monitorTo").value = f.to || "";
    const selectedCabins = monitorCabins(f); document.querySelectorAll("input[name='monitorCabin']").forEach(input => { input.checked = selectedCabins.includes(input.value); }); $("monitorBudget").value = f.budget_pln || "";
    $("monitorDuration").value = f.max_duration_h || ""; $("monitorStops").value = Number.isInteger(f.max_stops) ? f.max_stops : "";
    $("monitorPreferredAirlines").value = (f.preferred_airlines || []).join(", ");
    $("monitorExcludedAirlines").value = (f.excluded_airlines || []).join(", "); $("monitorDirectOnly").checked = Boolean(f.direct_only);
    $("telegramStars").value = r.min_stars || ""; $("telegramDrop").value = r.drop_percent || ""; $("telegramImmediate").checked = Boolean(r.immediate_new_low);
    $("formMessage").textContent = ""; $("monitorDialog").showModal();
  }

  async function saveMonitor(e) {
    e.preventDefault(); if (!editingMonitorId && monitors.length >= 2) { $("formMessage").textContent = "Limit dwóch monitorów na osobę."; return; }
    const name = $("monitorName").value.trim();
    const origins = csv($("monitorOrigins").value), destinations = csv($("monitorDestinations").value);
    const from = $("monitorFrom").value, to = $("monitorTo").value;
    const cabins = [...document.querySelectorAll("input[name='monitorCabin']:checked")].map(input => input.value), budgetRaw = $("monitorBudget").value.trim(), durationRaw = $("monitorDuration").value.trim(), stopsRaw = $("monitorStops").value.trim();
    const starsRaw = $("telegramStars").value, dropRaw = $("telegramDrop").value.trim();
    const budget = Number(budgetRaw), maxDuration = Number(durationRaw), maxStops = Number(stopsRaw), telegramStars = Number(starsRaw), telegramDrop = Number(dropRaw);
    const validIata = values => values.length > 0 && values.every(value => /^[A-Z]{3}$/.test(value));
    if (!name) { $("formMessage").textContent = "Podaj nazwę monitoringu."; return; }
    if (!validIata(origins) || !validIata(destinations) || origins.length > 5 || destinations.length > 5) { $("formMessage").textContent = "Wpisz od 1 do 5 lotnisk wylotu i maksymalnie 5 celów jako trzyznakowe kody IATA."; return; }
    const days = from && to ? Math.round((new Date(`${to}T12:00:00`) - new Date(`${from}T12:00:00`)) / 86400000) + 1 : 0;
    if (!from || !to || from > to || days > 32) { $("formMessage").textContent = "Zakres dat jest nieprawidłowy (maksymalnie 32 dni)."; return; }
    if (!cabins.length || cabins.length > 4 || !budgetRaw || !durationRaw || !stopsRaw || !starsRaw || !dropRaw || !Number.isFinite(budget) || budget <= 0 || !Number.isFinite(maxDuration) || maxDuration <= 0 || maxDuration > 24 || !Number.isInteger(maxStops) || maxStops < 0 || maxStops > 9 || !Number.isInteger(telegramStars) || telegramStars < 3 || telegramStars > 5 || !Number.isFinite(telegramDrop) || telegramDrop < 1 || telegramDrop > 50) { $("formMessage").textContent = "Wybierz co najmniej jedną klasę i uzupełnij wszystkie wymagane kryteria. Czas może wynosić maksymalnie 24 h, a przesiadki 0–9."; return; }
    const excludedAirlines = csv($("monitorExcludedAirlines").value);
    const preferredAirlines = csv($("monitorPreferredAirlines").value);
    const f = { origins, destinations, from, to, cabins, cabin: cabins[0], budget_pln: budget, max_duration_h: maxDuration, max_stops: maxStops, preferred_airlines: preferredAirlines, direct_only: $("monitorDirectOnly").checked, excluded_airlines: excludedAirlines };
    const r = { min_stars: telegramStars, drop_percent: telegramDrop, immediate_new_low: $("telegramImmediate").checked };
    const payload = { name, filters: f, app_rules: { min_stars: 1 }, telegram_rules: r, expires_at: f.to || null };
    let result;
    try {
      const refreshed = { ...payload, last_scanned_at: null, next_scan_at: new Date().toISOString() };
      result = editingMonitorId
        ? await client.from("monitors").update(refreshed).eq("id", editingMonitorId).eq("user_id", user.id)
        : await client.from("monitors").insert({ ...payload, user_id: user.id });
    } catch (error) {
      $("formMessage").textContent = `Nie udało się zapisać: ${error.message || "błąd połączenia"}`;
      return;
    }
    $("formMessage").textContent = result.error ? result.error.message : "Zapisano.";
    if (!result.error) { $("monitorDialog").close(); $("monitorForm").reset(); editingMonitorId = null; await loadMonitors(); }
  }
  async function updateMonitor(id, patch) { const update = patch.status === "active" ? { ...patch, last_scanned_at: null, next_scan_at: new Date().toISOString() } : patch; try { const { error } = await client.from("monitors").update(update).eq("id", id).eq("user_id", user.id); if (error) throw error; await loadMonitors(); } catch (error) { alert(`Nie udało się zmienić monitora: ${error.message || "błąd połączenia"}`); } }
  async function deleteMonitor(id) { if (!confirm("Usunąć ten monitoring?")) return; try { const { error } = await client.from("monitors").delete().eq("id", id).eq("user_id", user.id); if (error) throw error; await Promise.all([loadMonitors(), loadOffers()]); } catch (error) { alert(`Nie udało się usunąć monitora: ${error.message || "błąd połączenia"}`); } }

  async function loadOffers() {
    const { data, error } = await client.from("user_matches").select("id, stars, feedback, notified_at, updated_at, flight_offers(route,travel_date,airline_name,price_pln,cabin,duration_minutes,stops,aircraft,link,source,tags)").eq("visible", true).order("updated_at", { ascending: false }).limit(40);
    if (error) throw error; offers = data || []; renderOffers();
    const last = offers[0]?.updated_at; $("statusStrip").innerHTML = `<span>🔎 <strong>Ostatni wynik:</strong> ${last ? new Date(last).toLocaleString("pl-PL") : "brak"}</span><span>⏱ Skan Google: co 3 godziny</span><span>🔐 Wyniki tylko dla Ciebie</span>`;
  }
  function renderOffers() {
    const query = String($("offerSearch")?.value || "").trim().toLowerCase();
    const cabin = $("offerCabinFilter")?.value || "";
    const minStars = Number($("offerStarsFilter")?.value || 0);
    const sort = $("offerSort")?.value || "newest";
    const filtered = offers.filter(match => {
      const offer = match.flight_offers || {};
      const haystack = `${offer.route || ""} ${offer.airline_name || ""} ${offer.source || ""}`.toLowerCase();
      return (!query || haystack.includes(query)) && (!cabin || offer.cabin === cabin) && Number(match.stars || 0) >= minStars;
    }).sort((a, b) => {
      const ao = a.flight_offers || {}, bo = b.flight_offers || {};
      if (sort === "price") return Number(ao.price_pln || Infinity) - Number(bo.price_pln || Infinity);
      if (sort === "stars") return Number(b.stars || 0) - Number(a.stars || 0) || Number(ao.price_pln || Infinity) - Number(bo.price_pln || Infinity);
      return new Date(b.updated_at || 0) - new Date(a.updated_at || 0);
    });
    $("offerCount").textContent = offers.length ? `Pokazano ${filtered.length} z ${offers.length}` : "";
    $("offerList").innerHTML = filtered.length ? filtered.map(renderOffer).join("") : `<div class="empty">${offers.length ? "Brak ofert pasujących do filtrów." : "Brak dopasowanych ofert. Skaner uzupełni je po uruchomieniu własnego monitoringu."}</div>`;
    document.querySelectorAll("[data-feedback]").forEach(btn => btn.onclick = () => sendFeedback(btn.dataset.feedback, btn.dataset.match));
  }
  function renderOffer(m) { const o = m.flight_offers || {}; const mins = Number(o.duration_minutes || 0); const duration = mins ? `${Math.floor(mins / 60)}h ${mins % 60}m` : "—"; const tags = (o.tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join(""); const aircraft = o.aircraft ? ` · 🛫 ${esc(o.aircraft)}` : ""; return `<article class="offer-card"><div class="card-top"><div><div class="stars">${"⭐".repeat(m.stars || 1)}</div><div class="route">${esc(routeName(o.route))}</div><div class="card-meta">✈ ${esc(o.airline_name)} · ${esc(CABINS[o.cabin] || o.cabin || "—")} · ${dateFmt(o.travel_date)}</div></div><div class="price">${Number(o.price_pln || 0).toLocaleString("pl-PL")} PLN</div></div><div class="card-meta">${duration} · ${o.stops === 0 ? "bez przesiadek" : `${o.stops ?? "?"} przes.`}${aircraft} · ${esc(o.source)}</div>${tags ? `<div class="tags">${tags}</div>` : ""}<div class="card-actions"><button data-feedback="buy" data-match="${m.id}">👍 Kupiłbym</button><button data-feedback="expensive" data-match="${m.id}">💸 Za drogo</button><button data-feedback="skip" data-match="${m.id}">🙅 Nie</button></div><a href="${safeHref(o.link)}" target="_blank" rel="noopener noreferrer">Otwórz ofertę →</a></article>`; }
  async function sendFeedback(verdict, matchId) { const { error } = await client.from("feedback").upsert({ user_id: user.id, match_id: matchId, verdict }, { onConflict: "user_id,match_id" }); if (error) { alert(error.message); return; } const updated = await client.from("user_matches").update({ feedback: verdict }).eq("id", matchId).eq("user_id", user.id); if (updated.error) { alert(updated.error.message); return; } const label = { buy: "Kupiłbym", expensive: "Za drogo", skip: "Pominięto" }[verdict] || verdict; alert(`Zapisano: ${label}`); }
  async function signInWithTelegram() {
    const button = $("telegramLoginButton");
    if (!window.Telegram?.Login?.auth) { message("Nie udało się załadować logowania Telegram. Odśwież stronę."); return; }
    const clientId = Number(cfg.telegramClientId || "8897966422");
    if (!Number.isInteger(clientId) || clientId <= 0) { message("Brak prawidłowej konfiguracji logowania Telegram."); return; }
    button.disabled = true;
    message("Potwierdź logowanie w oknie Telegrama…");
    window.Telegram.Login.auth({ client_id: clientId, scope: ["profile", "write"], lang: "pl" }, async result => {
      try {
        if (!result?.id_token) throw new Error(result?.error || "Logowanie zostało anulowane.");
        const response = await fetch(`${cfg.supabaseUrl}/functions/v1/telegram-auth`, {
          method: "POST",
          headers: { "Content-Type": "application/json", apikey: cfg.supabaseAnonKey },
          body: JSON.stringify({ id_token: result.id_token })
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || "Nie udało się zweryfikować logowania Telegram.");
        const { error } = await client.auth.signInWithPassword({ email: body.email, password: body.password });
        if (error) throw error;
      } catch (error) {
        message(`Nie udało się zalogować przez Telegram: ${error.message || "błąd logowania"}`);
      } finally {
        button.disabled = false;
      }
    });
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
    document.querySelectorAll("[data-action='toggle-user']").forEach(btn => btn.onclick = async () => { const next = btn.dataset.status === "active" ? "suspended" : "active"; const result = await client.rpc("set_profile_status", { target_id: btn.dataset.user, next_status: next }); if (result.error || result.data !== true) alert(result.error?.message || "Nie zmieniono statusu."); else await loadAdmin(); });
    document.querySelectorAll("[data-action='delete-user']").forEach(btn => btn.onclick = async () => { if (!confirm("Usunąć konto, jego monitory i alerty? Tego nie można cofnąć.")) return; const result = await client.rpc("admin_delete_profile", { target_id: btn.dataset.user }); if (result.error || result.data !== true) alert(result.error?.message || "Nie usunięto konta."); else await loadAdmin(); });
    $("inviteButton").onclick = createInvite;
  }
  async function createInvite() { const token = crypto.randomUUID().replaceAll("-", ""); const hash = await hashToken(token); const { error } = await client.rpc("create_invite", { p_token_hash: hash, p_email: null }); if (error) { $("inviteOutput").textContent = error.message; return; } const link = `${location.origin}${location.pathname}?invite=${token}`; $("inviteOutput").innerHTML = `Skopiuj: <a href="${safeHref(link)}">${esc(link)}</a>`; }
  init().catch(err => { if (configReady) { show("authView"); message(`Nie udało się uruchomić panelu: ${err.message}`); } });
})();
