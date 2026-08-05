(() => {
  try {
    if (window.top !== window.self) {
      document.documentElement.innerHTML = '<body><main><section class="panel"><h1>Otwórz Flight Radar w osobnej karcie</h1><p>Ta aplikacja nie działa wewnątrz osadzonego okna.</p></section></main></body>';
      return;
    }
  } catch (_error) {
    return;
  }
  const cfg = window.ASIA_RADAR_CONFIG || {};
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));
  const safeHref = value => /^https?:\/\//i.test(String(value || "")) ? esc(value) : "#";
  const configReady = cfg.supabaseUrl && cfg.supabaseAnonKey && !String(cfg.supabaseUrl).includes("YOUR_") && !String(cfg.supabaseAnonKey).includes("YOUR_");
  let client = null, user = null, profile = null, monitors = [], offers = [], priceHistory = {}, monitorProgress = {};
  let telegramConnectionReady = false;
  let editingMonitorId = null, airportDataReady = false, offerOffset = 0, offersHaveMore = false, offersLoading = false, offerReloadTimer = null;
  const airportSelections = { origins: [], destinations: [] };
  const OFFER_PAGE_SIZE = 40;
  const MAX_MONITOR_COMBINATIONS = 5000;
  const MAX_MONITOR_DATE_WINDOW_DAYS = 14;

  const AIRPORT_OVERRIDES = { GDN: "Gdańsk", WAW: "Warszawa", POZ: "Poznań", OSL: "Oslo", ARN: "Sztokholm", CPH: "Kopenhaga", VIE: "Wiedeń", BUD: "Budapeszt", MXP: "Mediolan", IST: "Stambuł", BKK: "Bangkok", SIN: "Singapur", KUL: "Kuala Lumpur", HKG: "Hongkong", HAN: "Hanoi", SGN: "Ho Chi Minh", HND: "Tokio", NRT: "Tokio", ICN: "Seul" };
  const AIRPORTS = { ...AIRPORT_OVERRIDES };
  const CABINS = { BUSINESS: "Business", FIRST: "First", PREMIUM_ECONOMY: "Premium Economy", "PREMIUM-ECONOMY": "Premium Economy", ECONOMY: "Economy" };

  function show(id, on = true) { $(id).classList.toggle("hidden", !on); }
  function message(text, good = false) { const el = $("authMessage"); el.textContent = text; el.className = "message" + (good ? " good" : ""); }
  function hashToken(value) {
    return crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)).then(buf => [...new Uint8Array(buf)].map(x => x.toString(16).padStart(2, "0")).join(""));
  }
  const csv = value => value.split(",").map(x => x.trim().toUpperCase()).filter(Boolean);
  const dateFmt = value => value ? new Date(value + "T12:00:00").toLocaleDateString("pl-PL") : "—";
  const dateTimeFmt = value => value ? new Date(value).toLocaleString("pl-PL", { dateStyle: "short", timeStyle: "short" }) : "jeszcze nie sprawdzono";
  const isoDate = value => {
    const date = value ? new Date(`${value}T12:00:00`) : new Date();
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  };
  const addDays = (value, days) => {
    const date = new Date(`${value}T12:00:00`);
    date.setDate(date.getDate() + days);
    return isoDate(date.toISOString().slice(0, 10));
  };
  const todayIso = () => isoDate();
  function validRoundTripPairCount(from, to, returnFrom, returnTo) {
    if (!from || !to || !returnFrom || !returnTo || from > to || returnFrom > returnTo) return 0;
    let total = 0;
    for (let departure = new Date(`${from}T12:00:00`); departure <= new Date(`${to}T12:00:00`); departure.setDate(departure.getDate() + 1)) {
      const departureIso = isoDate(departure.toISOString().slice(0, 10));
      const firstValidReturn = returnFrom > departureIso ? returnFrom : addDays(departureIso, 1);
      if (firstValidReturn <= returnTo) {
        total += Math.round((new Date(`${returnTo}T12:00:00`) - new Date(`${firstValidReturn}T12:00:00`)) / 86400000) + 1;
      }
    }
    return total;
  }
  const airportName = value => AIRPORTS[String(value || "").toUpperCase()] || String(value || "");
  const airportLabel = value => { const code = String(value || "").trim().toUpperCase(); const city = airportName(code); return city && city.toUpperCase() !== code ? `${city} (${code})` : code; };
  const routeName = value => String(value || "").split(/\s*→\s*/).map(airportLabel).join(" → ");
  const monitorCabins = filters => Array.isArray(filters?.cabins) && filters.cabins.length ? filters.cabins : (filters?.cabin ? [filters.cabin] : []);
  const offerData = match => { const relation = match?.flight_offers; return Array.isArray(relation) ? (relation[0] || {}) : (relation || {}); };

  async function loadAirportData() {
    try {
      const response = await fetch("airports.json", { cache: "force-cache" });
      if (!response.ok) return;
      const data = await response.json();
      if (data && typeof data === "object" && Object.keys(data).length > 5000) {
        Object.assign(AIRPORTS, data, AIRPORT_OVERRIDES);
        airportDataReady = true;
      }
    } catch (_error) {
      // Wbudowana lista najczęstszych lotnisk pozostaje bezpiecznym fallbackiem.
    }
  }

  function airportSearchResults(query, selected) {
    const needle = String(query || "").trim().toLocaleLowerCase("pl-PL");
    if (!needle) return [];
    const selectedCodes = new Set(selected);
    return Object.entries(AIRPORTS).filter(([code, city]) => {
      if (selectedCodes.has(code)) return false;
      return `${code} ${city}`.toLocaleLowerCase("pl-PL").includes(needle);
    }).sort(([codeA, cityA], [codeB, cityB]) => {
      const rank = ([code, city]) => {
        const codeValue = code.toLocaleLowerCase("pl-PL"), cityValue = city.toLocaleLowerCase("pl-PL");
        return codeValue === needle ? 0 : cityValue.startsWith(needle) ? 1 : codeValue.startsWith(needle) ? 2 : 3;
      };
      return rank([codeA, cityA]) - rank([codeB, cityB]) || cityA.localeCompare(cityB, "pl");
    }).slice(0, 8);
  }

  function renderAirportPicker(kind, query = "") {
    const input = $(`monitor${kind === "origins" ? "Origins" : "Destinations"}`);
    const suggestions = $(`monitor${kind === "origins" ? "Origins" : "Destinations"}Suggestions`);
    const selected = airportSelections[kind];
    $(`monitor${kind === "origins" ? "Origins" : "Destinations"}Selected`).innerHTML = selected.map(code => `<span class="airport-chip">${esc(airportLabel(code))}<button type="button" data-remove-airport="${esc(code)}" aria-label="Usuń ${esc(airportLabel(code))}">×</button></span>`).join("");
    const results = airportSearchResults(query, selected);
    suggestions.innerHTML = results.map(([code, city]) => `<button type="button" role="option" data-airport-code="${esc(code)}"><strong>${esc(city)}</strong><span>${esc(code)}</span></button>`).join("");
    suggestions.classList.toggle("hidden", !results.length);
    input.setAttribute("aria-expanded", results.length ? "true" : "false");
  }

  function selectAirport(kind, code) {
    if (!airportSelections[kind].includes(code) && airportSelections[kind].length < 5) airportSelections[kind].push(code);
    const input = $(`monitor${kind === "origins" ? "Origins" : "Destinations"}`);
    input.value = "";
    renderAirportPicker(kind);
    updateMonitorEstimate();
  }

  function setupAirportPicker(kind) {
    const suffix = kind === "origins" ? "Origins" : "Destinations";
    const input = $(`monitor${suffix}`), suggestions = $(`monitor${suffix}Suggestions`), selected = $(`monitor${suffix}Selected`);
    const chooseSuggestion = event => {
      const option = event.target.closest("[data-airport-code]");
      if (!option || !suggestions.contains(option)) return;
      // Select before the input loses focus.  This is important in Brave and
      // on touch screens, where blur can hide the list before click fires.
      event.preventDefault();
      selectAirport(kind, option.dataset.airportCode);
    };
    input.oninput = () => renderAirportPicker(kind, input.value);
    input.onfocus = () => renderAirportPicker(kind, input.value);
    input.onkeydown = event => {
      if (event.key === "Enter") {
        const first = suggestions.querySelector("[data-airport-code]");
        if (first) { event.preventDefault(); selectAirport(kind, first.dataset.airportCode); }
      }
    };
    input.onblur = () => window.setTimeout(() => suggestions.classList.add("hidden"), 120);
    // pointerdown covers mouse, touch and pen and runs before blur/click.
    // Keep click as a compatibility fallback for browsers without Pointer Events.
    suggestions.onpointerdown = chooseSuggestion;
    suggestions.onclick = chooseSuggestion;
    selected.onclick = event => {
      const button = event.target.closest("[data-remove-airport]");
      if (!button) return;
      airportSelections[kind] = airportSelections[kind].filter(code => code !== button.dataset.removeAirport);
      renderAirportPicker(kind);
      updateMonitorEstimate();
    };
  }

  function setAirportSelections(kind, values) {
    airportSelections[kind] = [...new Set((values || []).map(value => String(value).trim().toUpperCase()).filter(value => /^[A-Z]{3}$/.test(value) && (!airportDataReady || AIRPORTS[value])))].slice(0, 5);
    renderAirportPicker(kind);
  }

  async function init() {
    $("themeButton").onclick = () => { document.body.classList.toggle("dark"); localStorage.setItem("afr-theme", document.body.classList.contains("dark") ? "dark" : "light"); };
    if (localStorage.getItem("afr-theme") === "dark") document.body.classList.add("dark");
    $("signOutButton").onclick = () => client?.auth.signOut();
    $("telegramLoginButton").onclick = signInWithTelegram;
    await loadAirportData();
    setupAirportPicker("origins"); setupAirportPicker("destinations");
    if (!configReady || !window.supabase) { show("setupView"); return; }
    client = window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey);
    const { data, error } = await client.auth.getSession();
    if (error) throw error;
    user = data.session?.user || null;
    await loadApp();
    client.auth.onAuthStateChange((event, session) => {
      // getSession() above already rendered the initial state. Ignoring the
      // subscription's duplicate initial event prevents two overlapping loads.
      if (event === "INITIAL_SESSION") return;
      user = session?.user || null;
      loadApp().catch(err => message(err.message));
    });
  }

  async function loadApp() {
    if (!user) { show("authView"); show("appView", false); show("appTabs", false); show("signOutButton", false); return; }
    show("authView", false); show("appView"); show("signOutButton"); $("userBadge").textContent = user.user_metadata?.preferred_username ? `@${user.user_metadata.preferred_username}` : "Telegram";
    const { data, error } = await client.from("profiles").select("*").eq("id", user.id).single();
    if (error) throw error; profile = data;
    if (new URLSearchParams(location.search).get("invite")) {
      const token = new URLSearchParams(location.search).get("invite");
      const claimed = await client.rpc("claim_invite", { invite_token: token });
      history.replaceState({}, "", location.pathname);
      if (claimed.error || claimed.data !== true) profile.invite_error = "Link zaproszenia jest nieprawidłowy, wygasł albo został już wykorzystany.";
      else { profile.status = "active"; delete profile.invite_error; }
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
    await loadMonitors();
    await loadOffers(true);
    if (profile.role === "admin") await loadAdmin();
    $("newMonitorButton").onclick = () => openMonitorDialog();
    $("closeDialog").onclick = $("cancelDialog").onclick = () => $("monitorDialog").close();
    $("monitorForm").onsubmit = saveMonitor;
    $("monitorTripType").onchange = updateRoundTripFields;
    ["monitorFrom", "monitorTo", "monitorReturnFrom", "monitorReturnTo"].forEach(id => $(id).oninput = () => { updateDateConstraints(); updateMonitorEstimate(); });
    $("refreshButton").onclick = async () => { await loadMonitors(); await loadOffers(true); };
    $("loadMoreOffersButton").onclick = () => loadOffers(false);
    ["offerSearch", "offerCabinFilter", "offerStarsFilter", "offerFreshnessFilter", "offerSort"].forEach(id => $(id).oninput = scheduleOffersReload);
  }

  function renderBlocked() {
    const suspended = profile?.status === "suspended";
    const inviteError = profile?.invite_error ? `<span class="status-suspended">${esc(profile.invite_error)}</span>` : "";
    $("statusStrip").innerHTML = `<strong>${suspended ? "⛔ Konto zawieszone" : "⏳ Konto oczekuje na aktywację"}</strong><span>${suspended ? "Skontaktuj się z administratorem." : "Poproś administratora o zaproszenie lub sprawdź, czy użyłeś właściwego linku."}</span>${inviteError}`;
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
    const { data, error } = await client.from("monitors").select("*").eq("user_id", user.id).order("created_at", { ascending: false });
    if (error) throw error; monitors = data || [];
    monitorProgress = {};
    await Promise.all(monitors.map(async monitor => {
      const result = await client.rpc("get_monitor_scan_progress", { p_monitor_id: monitor.id });
      if (!result.error && result.data) monitorProgress[monitor.id] = result.data;
    }));
    $("monitorCount").textContent = `${monitors.length}/2 wykorzystane`;
    $("monitorList").innerHTML = monitors.length ? monitors.map(renderMonitor).join("") : `<div class="empty">Czysta karta. Ustaw własne kryteria, aby uruchomić pierwszy monitoring.</div>`;
    document.querySelectorAll("[data-action='edit']").forEach(btn => btn.onclick = () => openMonitorDialog(monitors.find(m => m.id === btn.dataset.id)));
    document.querySelectorAll("[data-action='pause']").forEach(btn => btn.onclick = () => updateMonitor(btn.dataset.id, { status: btn.dataset.status === "active" ? "paused" : "active" }));
    document.querySelectorAll("[data-action='delete']").forEach(btn => btn.onclick = () => deleteMonitor(btn.dataset.id));
  }

  function renderMonitor(m) {
    const f = m.filters || {};
    const cabins = monitorCabins(f);
    const cabinLabel = cabins.map(cabin => CABINS[cabin] || cabin).join(", ");
    const route = `${(f.origins || []).map(airportLabel).join(", ")} → ${(f.destinations || []).map(airportLabel).join(", ")}`;
    const nextScan = m.status === "active" ? `Następny skan: ${dateTimeFmt(m.next_scan_at)}` : "Skan wstrzymany";
    const progress = monitorProgress[m.id];
    const queueLabel = progress && Number(progress.total_count) ? `Kolejka: ${Number(progress.due_count || 0).toLocaleString("pl-PL")}/${Number(progress.total_count).toLocaleString("pl-PL")} oczekuje` : "Kolejka: przygotowywanie";
    const airlineRules = [(f.preferred_airlines || []).length ? `Preferowane: ${(f.preferred_airlines || []).join(", ")}` : "", (f.excluded_airlines || []).length ? `Wykluczone: ${(f.excluded_airlines || []).join(", ")}` : ""].filter(Boolean).join(" · ");
    const durationLabel = f.max_duration_h ? `maks. ${esc(f.max_duration_h)}h` : "bez limitu czasu";
    const dateLabel = f.trip_type === "round_trip" ? `${dateFmt(f.from)}–${dateFmt(f.to)} · powrót ${dateFmt(f.return_from)}–${dateFmt(f.return_to)}` : `${dateFmt(f.from)}–${dateFmt(f.to)}`;
    return `<article class="monitor-card"><div class="monitor-card-head"><div><h3>${esc(m.name)}</h3><div class="monitor-route">${esc(route)}</div></div><span class="monitor-status ${m.status === "active" ? "status-active" : "status-suspended"}">${m.status === "active" ? "Aktywny" : "Wstrzymany"}</span></div><div class="monitor-info"><div class="monitor-info-row"><span>Klasa</span><strong>${esc(cabinLabel || "—")}</strong></div><div class="monitor-info-row"><span>Daty</span><strong>${esc(dateLabel)}</strong></div><div class="monitor-info-row"><span>Budżet</span><strong>Do ${esc(f.budget_pln ?? "—")} PLN</strong></div><div class="monitor-info-row"><span>Lot</span><strong>${durationLabel} · maks. ${esc(f.max_stops ?? "—")} przesiad.</strong></div><div class="monitor-info-row"><span>Telegram</span><strong>Wszystkie oferty spełniające filtry</strong></div>${airlineRules ? `<div class="monitor-info-row monitor-info-wide"><span>Linie</span><strong>${esc(airlineRules)}</strong></div>` : ""}<div class="monitor-info-row monitor-info-wide"><span>Status skanu</span><strong>${esc(queueLabel)} · ${esc(dateTimeFmt(m.last_scanned_at))} · ${esc(nextScan)}</strong></div></div><div class="card-actions"><button data-action="edit" data-id="${m.id}">Edytuj</button><button data-action="pause" data-id="${m.id}" data-status="${m.status}">${m.status === "active" ? "Wstrzymaj" : "Wznów"}</button><button data-action="delete" data-id="${m.id}">Usuń</button></div></article>`;
  }

  function updateRoundTripFields() {
    const roundTrip = $("monitorTripType").value === "round_trip";
    show("roundTripFields", roundTrip);
    ["monitorReturnFrom", "monitorReturnTo"].forEach(id => $(id).required = roundTrip);
    updateDateConstraints();
    updateMonitorEstimate();
  }

  function updateDateConstraints() {
    const today = todayIso();
    const from = $("monitorFrom"), to = $("monitorTo");
    const returnFrom = $("monitorReturnFrom"), returnTo = $("monitorReturnTo");
    if (!from || !to || !returnFrom || !returnTo) return;

    from.min = today;
    to.min = from.value && from.value >= today ? from.value : today;
    to.max = addDays(from.value && from.value >= today ? from.value : today, MAX_MONITOR_DATE_WINDOW_DAYS - 1);

    // A value already loaded from an old monitor must not survive outside the
    // new selectable range. This also keeps the form valid when the calendar
    // rolls past an existing departure date.
    if (from.value && from.value < today) from.value = "";
    if (to.value && to.value < to.min) to.value = "";
    if (to.value && to.value > to.max) to.value = "";
    if (from.value && to.value && to.value < from.value) to.value = from.value;

    const minimumReturn = from.value ? addDays(from.value, 1) : addDays(today, 1);
    returnFrom.min = minimumReturn;
    if (returnFrom.value && returnFrom.value < minimumReturn) returnFrom.value = "";
    returnTo.min = returnFrom.value || minimumReturn;
    returnTo.max = addDays(returnFrom.value || minimumReturn, MAX_MONITOR_DATE_WINDOW_DAYS - 1);
    if (returnTo.value && returnTo.value < returnTo.min) returnTo.value = "";
    if (returnTo.value && returnTo.value > returnTo.max) returnTo.value = "";
  }

  function updateMonitorEstimate() {
    const from = $("monitorFrom")?.value, to = $("monitorTo")?.value;
    const trip = $("monitorTripType")?.value || "one_way";
    const origins = airportSelections.origins.length;
    const destinations = airportSelections.destinations.length;
    const cabins = document.querySelectorAll("input[name='monitorCabin']:checked").length || 1;
    const dateWindowHint = "Zakres dat: maksymalnie 14 dni.";
    if (!from || !to || from > to || !origins || !destinations) { $("monitorQueryEstimate").textContent = dateWindowHint; return; }
    const days = Math.round((new Date(`${to}T12:00:00`) - new Date(`${from}T12:00:00`)) / 86400000) + 1;
    let pairs = days;
    if (trip === "round_trip") {
      const returnFrom = $("monitorReturnFrom")?.value, returnTo = $("monitorReturnTo")?.value;
      if (!returnFrom || !returnTo || returnFrom > returnTo) { $("monitorQueryEstimate").textContent = `Wybierz zakres powrotu, aby zobaczyć skalę skanu. ${dateWindowHint}`; return; }
      pairs = validRoundTripPairCount(from, to, returnFrom, returnTo);
    }
    const total = pairs * origins * destinations * cabins;
    $("monitorQueryEstimate").textContent = total > MAX_MONITOR_COMBINATIONS
      ? `Za dużo kombinacji: ${total.toLocaleString("pl-PL")} (maksymalnie ${MAX_MONITOR_COMBINATIONS.toLocaleString("pl-PL")}). Zawęź lotniska, daty albo klasy.`
      : `${trip === "round_trip" ? "Kombinacji wylot/powrót" : "Dat do sprawdzenia"}: ${total.toLocaleString("pl-PL")}. Kolejka będzie rotowana między monitorami. ${dateWindowHint}`;
  }

  function openMonitorDialog(monitor = null) {
    editingMonitorId = monitor?.id || null;
    const f = monitor?.filters || {}, r = monitor?.telegram_rules || {};
    $("monitorDialogEyebrow").textContent = editingMonitorId ? "Edycja monitora" : "Nowy monitor";
    $("monitorDialogTitle").textContent = editingMonitorId ? "Zmień własne kryteria" : "Ustaw własne kryteria";
    $("monitorName").value = monitor?.name || "";
    setAirportSelections("origins", f.origins || []); setAirportSelections("destinations", f.destinations || []);
    $("monitorOrigins").value = ""; $("monitorDestinations").value = "";
    $("monitorTripType").value = f.trip_type || "one_way";
    $("monitorFrom").value = f.from || ""; $("monitorTo").value = f.to || "";
    $("monitorReturnFrom").value = f.return_from || ""; $("monitorReturnTo").value = f.return_to || "";
    const selectedCabins = monitorCabins(f); document.querySelectorAll("input[name='monitorCabin']").forEach(input => { input.checked = selectedCabins.includes(input.value); }); $("monitorBudget").value = f.budget_pln || "";
    $("monitorDuration").value = f.max_duration_h || ""; $("monitorStops").value = Number.isInteger(f.max_stops) ? f.max_stops : "";
    $("monitorPreferredAirlines").value = (f.preferred_airlines || []).join(", ");
    $("monitorExcludedAirlines").value = (f.excluded_airlines || []).join(", "); $("monitorDirectOnly").checked = Boolean(f.direct_only);
    $("telegramDrop").value = r.drop_percent || "";
    $("formMessage").textContent = ""; updateRoundTripFields(); $("monitorDialog").showModal();
  }

  async function saveMonitor(e) {
    e.preventDefault(); if (!editingMonitorId && monitors.length >= 2) { $("formMessage").textContent = "Limit dwóch monitorów na osobę."; return; }
    const name = $("monitorName").value.trim();
    const origins = [...airportSelections.origins], destinations = [...airportSelections.destinations];
    const trip = $("monitorTripType").value, from = $("monitorFrom").value, to = $("monitorTo").value;
    const returnFrom = $("monitorReturnFrom").value, returnTo = $("monitorReturnTo").value;
    const cabins = [...document.querySelectorAll("input[name='monitorCabin']:checked")].map(input => input.value), budgetRaw = $("monitorBudget").value.trim(), durationRaw = $("monitorDuration").value.trim(), stopsRaw = $("monitorStops").value.trim();
    const dropRaw = $("telegramDrop").value.trim();
    const budget = Number(budgetRaw), maxDuration = durationRaw ? Number(durationRaw) : null, maxStops = Number(stopsRaw), telegramDrop = Number(dropRaw);
    const validIata = values => values.length > 0 && values.every(value => /^[A-Z]{3}$/.test(value) && (!airportDataReady || Boolean(AIRPORTS[value])));
    if (!name) { $("formMessage").textContent = "Podaj nazwę monitoringu."; return; }
    if (!validIata(origins) || !validIata(destinations) || origins.length > 5 || destinations.length > 5) { $("formMessage").textContent = "Wybierz od 1 do 5 lotnisk wylotu i celu z podpowiedzi."; return; }
    const days = from && to ? Math.round((new Date(`${to}T12:00:00`) - new Date(`${from}T12:00:00`)) / 86400000) + 1 : 0;
    if (!from || !to || from > to || days > MAX_MONITOR_DATE_WINDOW_DAYS) { $("formMessage").textContent = "Zakres dat wylotu jest nieprawidłowy (maksymalnie 14 dni)."; return; }
    const returnDays = returnFrom && returnTo ? Math.round((new Date(`${returnTo}T12:00:00`) - new Date(`${returnFrom}T12:00:00`)) / 86400000) + 1 : 0;
    if (trip === "round_trip" && (!returnFrom || !returnTo || returnFrom > returnTo || returnDays > MAX_MONITOR_DATE_WINDOW_DAYS || returnTo <= from)) { $("formMessage").textContent = "Zakres powrotu jest nieprawidłowy (maksymalnie 14 dni i po wylocie)."; return; }
    const pairCount = trip === "round_trip"
      ? validRoundTripPairCount(from, to, returnFrom, returnTo)
      : days;
    const combinationUpperBound = pairCount * origins.length * destinations.length * cabins.length;
    if (combinationUpperBound > MAX_MONITOR_COMBINATIONS) { $("formMessage").textContent = `Monitor może wygenerować do ${combinationUpperBound.toLocaleString("pl-PL")} kombinacji. Maksymalnie można zapisać ${MAX_MONITOR_COMBINATIONS.toLocaleString("pl-PL")}.`; return; }
    if (!cabins.length || cabins.length > 4 || !budgetRaw || !stopsRaw || !dropRaw || !Number.isFinite(budget) || budget <= 0 || (durationRaw && (!Number.isFinite(maxDuration) || maxDuration <= 0)) || !Number.isInteger(maxStops) || maxStops < 0 || maxStops > 9 || !Number.isFinite(telegramDrop) || telegramDrop < 1 || telegramDrop > 50) { $("formMessage").textContent = "Wybierz co najmniej jedną klasę i uzupełnij wymagane kryteria. Maksymalny czas możesz pozostawić pusty, aby nie ograniczać długości połączenia."; return; }
    const excludedAirlines = csv($("monitorExcludedAirlines").value);
    const preferredAirlines = csv($("monitorPreferredAirlines").value);
    const f = { origins, destinations, from, to, trip_type: trip, return_from: trip === "round_trip" ? returnFrom : null, return_to: trip === "round_trip" ? returnTo : null, cabins, cabin: cabins[0], budget_pln: budget, max_duration_h: maxDuration, max_stops: maxStops, preferred_airlines: preferredAirlines, direct_only: $("monitorDirectOnly").checked, excluded_airlines: excludedAirlines };
    const r = { min_stars: 3, drop_percent: telegramDrop, immediate_new_low: true };
    const payload = { name, filters: f, app_rules: { min_stars: 1 }, telegram_rules: r, expires_at: f.to || null };
    let result;
    try {
      const refreshed = { ...payload, last_scanned_at: null, next_scan_at: new Date().toISOString() };
      result = editingMonitorId
        ? await client.from("monitors").update(refreshed).eq("id", editingMonitorId).eq("user_id", user.id).select("id").maybeSingle()
        : await client.from("monitors").insert({ ...payload, user_id: user.id }).select("id").single();
    } catch (error) {
      $("formMessage").textContent = `Nie udało się zapisać: ${error.message || "błąd połączenia"}`;
      return;
    }
    if (result.error) { $("formMessage").textContent = result.error.message; return; }
    if (editingMonitorId && !result.data?.id) { $("formMessage").textContent = "Nie znaleziono tego monitora albo nie masz do niego dostępu."; return; }
    $("formMessage").textContent = "Zapisano.";
    $("monitorDialog").close(); $("monitorForm").reset(); editingMonitorId = null; await loadMonitors(); await loadOffers(true);
  }
  async function updateMonitor(id, patch) { const update = patch.status === "active" ? { ...patch, last_scanned_at: null, next_scan_at: new Date().toISOString() } : patch; try { const { error } = await client.from("monitors").update(update).eq("id", id).eq("user_id", user.id); if (error) throw error; await loadMonitors(); } catch (error) { alert(`Nie udało się zmienić monitora: ${error.message || "błąd połączenia"}`); } }
  async function deleteMonitor(id) { if (!confirm("Usunąć ten monitoring?")) return; try { const { error } = await client.from("monitors").delete().eq("id", id).eq("user_id", user.id); if (error) throw error; await loadMonitors(); await loadOffers(true); } catch (error) { alert(`Nie udało się zmienić monitora: ${error.message || "błąd połączenia"}`); } }

  async function loadOffers(reset = true) {
    // Do not rely on PostgREST's nested relationship response here. Depending
    // on the schema cache/RLS state it can return the match while leaving the
    // related offer as null. Fetch the two tables explicitly and join in the
    // browser using the guaranteed foreign key.
    if (offersLoading) return;
    offersLoading = true;
    const button = $("loadMoreOffersButton");
    button.disabled = true;
    if (reset) { offerOffset = 0; offers = []; priceHistory = {}; }
    const from = offerOffset;
    const to = from + OFFER_PAGE_SIZE - 1;
    const displayQuery = String($("offerSearch")?.value || "").trim();
    const displayCabin = $("offerCabinFilter")?.value || "";
    const displayStars = Number($("offerStarsFilter")?.value || 0);
    const displayFreshness = $("offerFreshnessFilter")?.value || "fresh";
    const displaySort = $("offerSort")?.value || "newest";
    try {
      // The RPC applies monitor filters before pagination. The compatibility
      // path below remains available while an older database is deploying the
      // migration, but production never paginates invalid matches first.
      const current = await client.rpc("get_my_offer_matches", {
        p_limit: OFFER_PAGE_SIZE,
        p_offset: from,
        p_query: displayQuery,
        p_cabin: displayCabin,
        p_min_stars: displayStars,
        p_freshness: displayFreshness,
        p_sort: displaySort,
      });
      if (!current.error) {
        const page = (current.data || []).map(row => ({
          id: row.match_id,
          monitor_id: row.monitor_id,
          offer_id: row.offer_id,
          stars: row.stars,
          feedback: row.feedback,
          notified_at: row.notified_at,
          updated_at: row.updated_at,
          flight_offers: {
            id: row.offer_id,
            fingerprint: row.fingerprint,
            source: row.source,
            route: row.route,
            origin: row.origin,
            destination: row.destination,
            travel_date: row.travel_date,
            return_date: row.return_date,
            trip_type: row.trip_type,
            cabin: row.cabin,
            airline: row.airline,
            airline_name: row.airline_name,
            price_pln: row.price_pln,
            duration_minutes: row.duration_minutes,
            stops: row.stops,
            aircraft: row.aircraft,
            link: row.link,
            tags: row.tags,
            raw: row.raw,
            last_seen_at: row.last_seen_at,
            verification_status: row.verification_status,
            verification_note: row.verification_note,
          },
        }));
        const offerIds = [...new Set(page.map(match => match.offer_id).filter(Boolean))];
        if (offerIds.length) {
          let { data: historyRows, error: historyError } = await client.rpc("offer_price_history_for_user", { p_offer_ids: offerIds });
          if (historyError) ({ data: historyRows, error: historyError } = await client.from("offer_price_history").select("offer_id,price_pln,observed_at").in("offer_id", offerIds).order("observed_at", { ascending: false }).limit(600));
          if (!historyError) for (const row of historyRows || []) (priceHistory[row.offer_id] ||= []).push(row);
        }
        offers = reset ? page : [...offers, ...page];
        offerOffset += page.length;
        offersHaveMore = page.length === OFFER_PAGE_SIZE;
        show("loadMoreOffersButton", offersHaveMore);
        renderOffers();
        const last = offers[0]?.updated_at;
        const telegramStatus = telegramConnectionReady ? "✅ Telegram połączony" : "⚠️ Telegram niepołączony — otwórz bota i wyślij /start";
        $("statusStrip").innerHTML = `<span>🔎 <strong>Ostatnia oferta:</strong> ${last ? new Date(last).toLocaleString("pl-PL") : "brak aktualnych ofert"}</span><span>⏱ Skan Google: 4 razy na dobę</span><span>${telegramStatus}</span>`;
        return;
      }
      const { data: matches, error: matchError } = await client.from("user_matches")
        .select("id, monitor_id, offer_id, stars, feedback, notified_at, updated_at")
        .eq("user_id", user.id)
        .eq("visible", true)
        .order("updated_at", { ascending: false })
        .range(from, to);
      if (matchError) throw matchError;
      const matchRows = matches || [];
      const offerIds = [...new Set(matchRows.map(match => match.offer_id).filter(Boolean))];
      let offerRows = [];
      if (offerIds.length) {
        const modern = await client.from("flight_offers")
          .select("id,fingerprint,route,origin,destination,travel_date,return_date,trip_type,airline,airline_name,price_pln,cabin,duration_minutes,stops,aircraft,link,source,tags,raw,last_seen_at,verification_status,verification_note")
          .in("id", offerIds);
        if (modern.error && /verification_status|verification_note|column/i.test(modern.error.message || "")) {
          // Compatibility with databases that have not run the additive
          // quality migration yet.
          const legacy = await client.from("flight_offers")
            .select("id,fingerprint,route,origin,destination,travel_date,return_date,trip_type,airline,airline_name,price_pln,cabin,duration_minutes,stops,aircraft,link,source,tags,raw,last_seen_at")
            .in("id", offerIds);
          if (legacy.error) throw legacy.error;
          offerRows = legacy.data || [];
        } else {
          if (modern.error) throw modern.error;
          offerRows = modern.data || [];
        }
      }
      if (offerIds.length) {
        let { data: historyRows, error: historyError } = await client.rpc("offer_price_history_for_user", { p_offer_ids: offerIds });
        // Compatibility fallback while the additive database migration is
        // rolling out. The RPC keeps at most 30 rows per offer, unlike the
        // old global limit which favored the most frequently changing fares.
        if (historyError) ({ data: historyRows, error: historyError } = await client.from("offer_price_history").select("offer_id,price_pln,observed_at").in("offer_id", offerIds).order("observed_at", { ascending: false }).limit(600));
        if (!historyError) for (const row of historyRows || []) (priceHistory[row.offer_id] ||= []).push(row);
      }
      const byId = new Map(offerRows.map(offer => [offer.id, offer]));
      const page = matchRows.map(match => ({ ...match, flight_offers: byId.get(match.offer_id) || null }));
      offers = reset ? page : [...offers, ...page];
      offerOffset += matchRows.length;
      offersHaveMore = matchRows.length === OFFER_PAGE_SIZE;
      show("loadMoreOffersButton", offersHaveMore);
      renderOffers();
      const last = offers[0]?.updated_at;
      const telegramStatus = telegramConnectionReady ? "✅ Telegram połączony" : "⚠️ Telegram niepołączony — otwórz bota i wyślij /start";
      $("statusStrip").innerHTML = `<span>🔎 <strong>Ostatnia oferta:</strong> ${last ? new Date(last).toLocaleString("pl-PL") : "brak aktualnych ofert"}</span><span>⏱ Skan Google: 4 razy na dobę</span><span>${telegramStatus}</span>`;
    } finally {
      offersLoading = false;
      button.disabled = false;
    }
  }
  function scheduleOffersReload() {
    clearTimeout(offerReloadTimer);
    offerReloadTimer = setTimeout(() => loadOffers(true), 250);
  }
  function renderOffers() {
    const query = String($("offerSearch")?.value || "").trim().toLowerCase();
    const cabin = $("offerCabinFilter")?.value || "";
    const minStars = Number($("offerStarsFilter")?.value || 0);
    const freshness = $("offerFreshnessFilter")?.value || "fresh";
    const sort = $("offerSort")?.value || "newest";
    const filtered = offers.filter(match => {
      const offer = offerData(match);
      if (!offer.route || !offer.travel_date || !offer.airline_name || !Number.isFinite(Number(offer.price_pln)) || Number(offer.price_pln) <= 0) return false;
      const monitor = monitors.find(item => item.id === match.monitor_id);
      const budget = Number(monitor?.filters?.budget_pln);
      if (!monitor || !Number.isFinite(budget) || budget <= 0 || Number(offer.price_pln) > budget) return false;
      const stale = isOfferStale(offer);
      if (freshness === "fresh" && stale) return false;
      if (freshness === "stale" && !stale) return false;
      const haystack = `${offer.route || ""} ${routeName(offer.route)} ${offer.airline_name || ""} ${offer.source || ""}`.toLowerCase();
      const normalizedCabin = String(offer.cabin || "").replace("-", "_");
      return (!query || haystack.includes(query)) && (!cabin || normalizedCabin === cabin) && Number(match.stars || 0) >= minStars;
    }).sort((a, b) => {
      const ao = offerData(a), bo = offerData(b);
      if (sort === "price") return Number(ao.price_pln || Infinity) - Number(bo.price_pln || Infinity);
      if (sort === "stars") return Number(b.stars || 0) - Number(a.stars || 0) || Number(ao.price_pln || Infinity) - Number(bo.price_pln || Infinity);
      return new Date(b.updated_at || 0) - new Date(a.updated_at || 0);
    });
    $("offerCount").textContent = offers.length ? `Pokazano ${filtered.length} z ${offers.length}` : "";
    $("offerList").innerHTML = filtered.length ? filtered.map(renderOffer).join("") : `<div class="empty">${offers.length ? "Brak ofert mieszczących się w budżecie i pozostałych filtrach." : "Brak dopasowanych ofert. Skaner uzupełni je po uruchomieniu własnego monitoringu."}</div>`;
    document.querySelectorAll("[data-feedback]").forEach(btn => btn.onclick = () => sendFeedback(btn.dataset.feedback, btn.dataset.match));
  }
  // An old timestamp alone is not proof that a fare disappeared: the shared
  // queue may still be working through a partial run. The scanner marks an
  // offer stale only after a complete healthy source pass, so the UI must use
  // that authoritative status instead of hiding offers after 24 hours.
  function isOfferStale(offer) { return offer.verification_status === "stale"; }
  function renderPriceHistory(offer) {
    const rows = (priceHistory[offer.id] || []).filter(row => Number(row.price_pln) > 0).slice(0, 12).reverse();
    if (!rows.length) return "";
    const prices = rows.map(row => Number(row.price_pln));
    const min = Math.min(...prices), max = Math.max(...prices), span = Math.max(1, max - min);
    const bars = rows.map(row => {
      const level = Math.max(1, Math.min(10, Math.round(((Number(row.price_pln) - min) / span) * 9) + 1));
      return `<span class="price-history-bar" data-level="${level}" title="${Number(row.price_pln).toLocaleString("pl-PL")} PLN"></span>`;
    }).join("");
    const trend = prices[prices.length - 1] < prices[0] ? "↓ taniej" : prices[prices.length - 1] > prices[0] ? "↑ drożej" : "→ bez zmiany";
    return `<div class="price-history"><span>Historia ceny: ${min.toLocaleString("pl-PL")}–${max.toLocaleString("pl-PL")} PLN · ${trend}</span><span class="price-history-bars" aria-label="Historia ceny">${bars}</span></div>`;
  }
  function renderOffer(m) {
    const o = offerData(m);
    const raw = o.raw && typeof o.raw === "object" ? o.raw : {};
    const leg = value => value == null || value === "" || !Number.isFinite(Number(value)) ? "—" : `${Math.floor(Number(value))}h ${Math.round((Number(value) % 1) * 60).toString().padStart(2, "0")}m`;
    const stops = value => value == null || value === "" || !Number.isFinite(Number(value)) ? "? przes." : (Number(value) === 0 ? "bez przesiadek" : `${Number(value)} przes.`);
    const isVerifiedRoundTrip = o.return_date && raw.round_trip_verified && raw.return_duration_h != null;
    const stale = isOfferStale(o);
    const tags = [...(o.tags || []), ...(stale ? ["Cena niepotwierdzona"] : []), ...(o.verification_status === "pending_return" ? ["Powrót do potwierdzenia"] : []), ...(o.verification_status === "pending_verification" ? ["Do potwierdzenia"] : [])].filter((tag, index, all) => all.indexOf(tag) === index).map(tag => `<span class="tag">${esc(tag)}</span>`).join("");
    const dates = o.return_date ? `${dateFmt(o.travel_date)} → ${dateFmt(o.return_date)}` : dateFmt(o.travel_date);
    const journeyRows = isVerifiedRoundTrip
      ? `<div class="offer-info-row"><span>Wylot</span><strong>${esc(leg(raw.outbound_duration_h))} · ${esc(stops(raw.outbound_stops))}</strong></div><div class="offer-info-row"><span>Powrót</span><strong>${esc(leg(raw.return_duration_h))} · ${esc(stops(raw.return_stops))}</strong></div>`
      : `<div class="offer-info-row"><span>Podróż</span><strong>${esc(leg(o.duration_minutes ? Number(o.duration_minutes) / 60 : null))} · ${esc(stops(o.stops))}</strong></div>`;
    return `<article class="offer-card"><div class="offer-card-head"><div class="offer-card-title"><div class="stars" aria-label="Ocena: ${m.stars || 1} na 5">${"⭐".repeat(m.stars || 1)}</div><div class="route">${esc(routeName(o.route))}</div></div><div class="price">${Number(o.price_pln || 0).toLocaleString("pl-PL")} PLN</div></div><div class="offer-info"><div class="offer-info-row"><span>Linia</span><strong>${esc(o.airline_name || "—")}</strong></div><div class="offer-info-row"><span>Klasa</span><strong>${esc(CABINS[o.cabin] || o.cabin || "—")}</strong></div><div class="offer-info-row offer-info-wide"><span>Termin</span><strong>${esc(dates)}</strong></div>${journeyRows}<div class="offer-info-row"><span>Samolot</span><strong>${o.aircraft ? `🛫 ${esc(o.aircraft)}` : "—"}</strong></div><div class="offer-info-row"><span>Źródło</span><strong>${esc(o.source || "—")}</strong></div></div>${tags ? `<div class="tags">${tags}</div>` : ""}${renderPriceHistory(o)}<div class="card-actions"><button data-feedback="buy" data-match="${m.id}">👍 Kupiłbym</button><button data-feedback="expensive" data-match="${m.id}">💸 Za drogo</button><button data-feedback="skip" data-match="${m.id}">🙅 Nie</button></div><a href="${safeHref(o.link)}" target="_blank" rel="noopener noreferrer">Otwórz ofertę →</a></article>`;
  }
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
        const inviteToken = new URLSearchParams(location.search).get("invite") || "";
        const response = await fetch(`${cfg.supabaseUrl}/functions/v1/telegram-auth`, {
          method: "POST",
          headers: { "Content-Type": "application/json", apikey: cfg.supabaseAnonKey },
          body: JSON.stringify({ id_token: result.id_token, invite_token: inviteToken })
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
    if (!telegramId) { telegramConnectionReady = false; return false; }
    const username = String(metadata.preferred_username || metadata.username || "").trim();
    const { data, error } = await client.rpc("sync_telegram_connection", { telegram_chat_id: telegramId, telegram_username: username });
    telegramConnectionReady = !error && data === true;
    if (error) console.warn("Nie udało się zsynchronizować Telegrama", error.message);
    return telegramConnectionReady;
  }

  async function loadAdmin() {
    const { data, error } = await client.from("profiles").select("id,email,display_name,telegram_user_id,status,role,last_seen_at,created_at").order("created_at"); if (error) throw error;
    const users = data || []; $("seatCount").textContent = `${users.filter(x => x.status === "active").length}/10 aktywnych`; $("userList").innerHTML = users.map(u => `<div class="user-row"><span><strong>${esc(u.display_name || u.email || "Telegram użytkownik")}</strong><br><small class="muted">Telegram ID: ${esc(u.telegram_user_id || "—")}</small></span><span class="status-${esc(u.status)}">${esc(u.status)}</span>${u.id === user.id ? "<span class='muted'>Ty</span>" : `<button class="secondary" data-user="${u.id}" data-status="${u.status}" data-action="toggle-user">${u.status === "active" ? "Zawieś" : "Aktywuj"}</button><button class="danger" data-user="${u.id}" data-action="delete-user">Usuń</button>`}</div>`).join("");
    const delivery = await client.rpc("admin_delivery_summary");
    if (!delivery.error && delivery.data) {
      const summary = delivery.data;
      $("deliverySummary").textContent = `Telegram: ${Number(summary.pending || 0)} oczekujących · ${Number(summary.sent_24h || 0)} wysłanych w 24 h · ${Number(summary.failed || 0)} trwale nieudanych`;
    } else {
      $("deliverySummary").textContent = "Telegram: kolejka dostarczania zostanie pokazana po wdrożeniu migracji.";
    }
    document.querySelectorAll("[data-action='toggle-user']").forEach(btn => btn.onclick = async () => { const next = btn.dataset.status === "active" ? "suspended" : "active"; const result = await client.rpc("set_profile_status", { target_id: btn.dataset.user, next_status: next }); if (result.error || result.data !== true) alert(result.error?.message || "Nie zmieniono statusu."); else await loadAdmin(); });
    document.querySelectorAll("[data-action='delete-user']").forEach(btn => btn.onclick = async () => { if (!confirm("Usunąć konto, jego monitory i alerty? Tego nie można cofnąć.")) return; const result = await client.rpc("admin_delete_profile", { target_id: btn.dataset.user }); if (result.error || result.data !== true) alert(result.error?.message || "Nie usunięto konta."); else await loadAdmin(); });
    $("inviteButton").onclick = createInvite;
    $("scanDueButton").onclick = () => requestImmediateScan(false);
    $("scanFullButton").onclick = () => requestImmediateScan(true);
    await loadScanStatus();
  }
  const SCAN_STATUS_LABELS = { queued: "W kolejce", running: "W toku", ok: "OK", partial: "Częściowy", blocked: "Zablokowany", error: "Błąd" };
  const scanStatusLabel = value => SCAN_STATUS_LABELS[String(value || "")] || "Nieznany";
  const scanStatusClass = value => value === "ok" ? "active" : value === "running" || value === "queued" ? "pending" : "suspended";
  const scanErrorDetails = value => value ? `<details class="scan-error-details"><summary>Pokaż szczegóły techniczne</summary><div>${esc(value)}</div></details>` : "";
  async function loadScanStatus() {
    const fields = "id,started_at,finished_at,query_count,due_count,due_item_count,total_queue_count,selected_count,failed_count,deferred_count,coverage_percent,standard_limit,first_limit,blocked,offer_count,status,error";
    let { data, error } = await client.from("scan_runs").select(fields).order("started_at", { ascending: false }).limit(8);
    if (error && /due_count|coverage_percent|column/i.test(error.message || "")) {
      ({ data, error } = await client.from("scan_runs").select("id,started_at,finished_at,query_count,standard_limit,first_limit,blocked,offer_count,status,error").order("started_at", { ascending: false }).limit(8));
    }
    if (error) { $("scanStatus").textContent = "Brak danych o skanach."; return null; }
    const rows = data || [], latest = rows[0];
    const coverage = row => row.coverage_percent == null ? "" : ` · pokrycie: ${Number(row.coverage_percent).toLocaleString("pl-PL", { maximumFractionDigits: 2 })}%`;
    const queue = row => row.due_count == null ? "" : ` · zapytania: ${row.selected_count || 0}/${row.due_count || 0} · pozycje kolejki: ${row.due_item_count == null ? "—" : `${row.due_item_count}/${row.total_queue_count || 0}`}`;
    $("scanStatus").innerHTML = latest ? `<strong>Ostatni skan: ${esc(scanStatusLabel(latest.status))}</strong> · ${esc(dateTimeFmt(latest.started_at))} · zapytań: ${latest.query_count || 0} · ofert: ${latest.offer_count || 0}${queue(latest)}${coverage(latest)}${scanErrorDetails(latest.error)}` : "Brak uruchomionych skanów.";
    $("scanHistory").innerHTML = rows.length ? `<h3>Historia skanów</h3>${rows.map(row => `<div class="scan-row"><span class="status-${scanStatusClass(row.status)}">${esc(scanStatusLabel(row.status))}</span><span>${esc(dateTimeFmt(row.started_at))}</span><span>${row.query_count || 0} zapytań · ${row.offer_count || 0} ofert${queue(row)}${coverage(row)}</span>${scanErrorDetails(row.error)}</div>`).join("")}` : "";
    return latest;
  }
  async function requestImmediateScan(fullQueueScan = false) {
    const button = $(fullQueueScan ? "scanFullButton" : "scanDueButton"), output = $("scanNowMessage");
    button.disabled = true; output.textContent = fullQueueScan ? "Uruchamianie pełnej kolejki…" : "Uruchamianie zaległych skanów…"; output.className = "message";
    try {
      const { data: sessionData, error: sessionError } = await client.auth.getSession();
      if (sessionError || !sessionData.session?.access_token) throw new Error("Sesja administratora wygasła. Odśwież stronę.");
      const response = await fetch(`${cfg.supabaseUrl}/functions/v1/admin-scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json", apikey: cfg.supabaseAnonKey, Authorization: `Bearer ${sessionData.session.access_token}` },
        body: JSON.stringify({ full_queue_scan: fullQueueScan })
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || "Nie udało się uruchomić skanu.");
      output.textContent = fullQueueScan ? "Pełny skan kolejki jest w kolejce. Status będzie aktualizowany automatycznie." : "Zaległe skany są w kolejce. Status będzie aktualizowany automatycznie."; output.className = "message good";
      loadScanStatus();
      pollScanStatus(body.run_id || null);
    } catch (error) {
      output.textContent = error.message || "Nie udało się uruchomić skanu."; output.className = "message";
    } finally {
      window.setTimeout(() => { button.disabled = false; }, 4000);
    }
  }
  async function pollScanStatus(runId, requestedAt = Date.now()) { for (let attempt = 0; attempt < 240; attempt++) { await new Promise(resolve => window.setTimeout(resolve, 5000)); const latest = await loadScanStatus(); if (!latest || (runId && latest.id !== runId) || (!runId && new Date(latest.started_at || 0).getTime() < requestedAt - 5000)) continue; if (["ok", "partial", "blocked", "error"].includes(latest.status)) { const output = $("scanNowMessage"); output.textContent = `Skan zakończony: ${latest.status}. Zapytania: ${latest.query_count || 0}, oferty: ${latest.offer_count || 0}.`; output.className = latest.status === "ok" ? "message good" : "message"; return; } } $("scanNowMessage").textContent = "Skan nadal trwa. Status sprawdzisz w historii skanów."; }
  async function copyInviteLink(link, button) {
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(link); copied = true; }
    } catch (_error) {
      // Przeglądarki mogą zablokować Clipboard API, np. w niektórych trybach prywatnych.
    }
    if (!copied) {
      const input = document.createElement("textarea");
      input.value = link; input.setAttribute("readonly", ""); input.style.position = "fixed"; input.style.opacity = "0";
      document.body.appendChild(input); input.select();
      try { copied = document.execCommand("copy"); } catch (_error) { copied = false; }
      input.remove();
    }
    const previous = button.textContent;
    button.textContent = copied ? "Skopiowano ✓" : "Zaznacz link";
    button.classList.toggle("copy-failed", !copied);
    if (!copied) { const input = $("inviteOutput").querySelector("input"); input?.focus(); input?.select(); }
    window.setTimeout(() => { button.textContent = previous; button.classList.remove("copy-failed"); }, 2200);
  }
  async function createInvite() {
    const token = crypto.randomUUID().replaceAll("-", ""); const hash = await hashToken(token);
    const { error } = await client.rpc("create_invite", { p_token_hash: hash, p_email: null });
    if (error) { $("inviteOutput").textContent = error.message; return; }
    const link = `${location.origin}${location.pathname}?invite=${token}`;
    $("inviteOutput").innerHTML = `<span class="invite-link-row"><input class="invite-link" aria-label="Link zaproszenia" readonly value="${esc(link)}"><button type="button" class="secondary compact-button" data-copy-invite="true">Kopiuj</button></span>`;
    const copyButton = $("inviteOutput").querySelector("[data-copy-invite]");
    copyButton.onclick = () => copyInviteLink(link, copyButton);
  }
  init().catch(err => { if (configReady) { show("authView"); message(`Nie udało się uruchomić panelu: ${err.message}`); } });
})();
