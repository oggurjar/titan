document.addEventListener('DOMContentLoaded', () => {

// ─────────────────────────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────────────────────────
function cleanMC(str) {
    if (!str) return '';
    return str.replace(/[&§][0-9a-fk-or]/gi, '').trim();
}

function formatLastSeen(ts) {
    if (!ts) return { text: 'Never', cls: 'offline', abs: 'No data' };
    const date = new Date(ts);
    const abs = date.toLocaleString(undefined, {
        weekday: 'short', year: 'numeric', month: 'short',
        day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
    const diff = Date.now() - ts;
    if (diff >= 0 && diff < 4 * 60000) return { text: 'Online Now', cls: 'online', abs };
    const mins = Math.floor(diff / 60000);
    const hrs  = Math.floor(mins / 60);
    const days = Math.floor(hrs / 24);
    if (mins < 60)  return { text: `${mins}m ago`,  cls: 'recent', abs };
    if (hrs  < 24)  return { text: `${hrs}h ago`,   cls: 'recent', abs };
    if (days === 1) return { text: 'Yesterday',      cls: 'offline', abs };
    if (days < 30)  return { text: `${days}d ago`,  cls: 'offline', abs };
    const months = Math.floor(days / 30);
    return { text: `${months}mo ago`, cls: 'offline', abs };
}

function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.remove('hidden');
    t.style.opacity = '1';
    t.style.transform = 'translateY(0)';
    clearTimeout(t._timer);
    t._timer = setTimeout(() => {
        t.style.opacity = '0';
        t.style.transform = 'translateY(10px)';
        setTimeout(() => t.classList.add('hidden'), 300);
    }, 2500);
}

function buildRanksHTML(ranks, pikaResult, nameStatus) {
    let html = '';
    const selectedLower = (selectedRanks || []).map(r => r.toLowerCase().trim());
    if (ranks && ranks.length) {
        ranks.forEach(r => {
            const name = cleanMC(r.displayName);
            const isTarget = selectedLower.includes(name.toLowerCase().trim());
            html += `<span class="badge rank-badge ${isTarget ? 'titan' : ''}">${name}</span>`;
        });
    }
    if (nameStatus === 'dropping') {
        html += `<span class="badge name-dropping-badge">⏰ Dropping Soon</span>`;
    } else if (nameStatus === 'free') {
        html += `<span class="badge name-free-badge">✓ Available</span>`;
    }
    // Only show premium badge — pending/unknown are not shown to user
    if (pikaResult === 'premium') {
        html += `<span class="badge pika-premium-badge">✅ Premium</span>`;
    }
    return html;
}

// ─────────────────────────────────────────────────────────────────────────────
// NAME DROP COUNTDOWN
// Fetches NameMC data and renders a live ticking countdown on the card.
// ─────────────────────────────────────────────────────────────────────────────
const _namedropCache = {};   // username -> { dropsAt, changedAt, available }
const _countdownTimers = {}; // username -> interval id

async function fetchNamedrop(username) {
    if (_namedropCache[username]) return _namedropCache[username];
    try {
        const r = await fetch(`/api/namedrop/${encodeURIComponent(username)}`);
        const d = await r.json();
        _namedropCache[username] = d;
        return d;
    } catch {
        return null;
    }
}

function formatCountdown(ms) {
    if (ms <= 0) return 'Available now!';
    const totalSecs = Math.floor(ms / 1000);
    const days  = Math.floor(totalSecs / 86400);
    const hrs   = Math.floor((totalSecs % 86400) / 3600);
    const mins  = Math.floor((totalSecs % 3600) / 60);
    const secs  = totalSecs % 60;
    if (days > 0)  return `${days}d ${hrs}h ${mins}m`;
    if (hrs > 0)   return `${hrs}h ${mins}m ${secs}s`;
    return `${mins}m ${secs}s`;
}

function startCountdown(username, dropsAtISO, el) {
    // clear existing timer for this username
    if (_countdownTimers[username]) clearInterval(_countdownTimers[username]);

    const target = new Date(dropsAtISO).getTime();
    function tick() {
        const remaining = target - Date.now();
        el.textContent = formatCountdown(remaining);
        if (remaining <= 0) {
            clearInterval(_countdownTimers[username]);
            delete _countdownTimers[username];
            el.textContent = '🟢 Claimable now!';
            el.style.color = 'var(--accent-green)';
        }
    }
    tick();
    _countdownTimers[username] = setInterval(tick, 1000);
}

async function loadNamedropOnCard(card, username) {
    const dropEl    = card.querySelector('.namedrop-time');
    const changedEl = card.querySelector('.namechange-time');
    if (!dropEl) return;

    dropEl.textContent = 'Loading...';
    const data = await fetchNamedrop(username);
    if (!data || data.error) {
        dropEl.textContent = 'N/A';
        return;
    }

    if (data.available) {
        dropEl.textContent = '🟢 Available now';
        dropEl.style.color = 'var(--accent-green)';
    } else if (data.dropsAt) {
        startCountdown(username, data.dropsAt, dropEl);
    } else {
        // No drop date and not flagged available — name has no known history
        dropEl.textContent = '🟢 Never taken';
        dropEl.style.color = 'var(--accent-green)';
    }

    if (changedEl) {
        if (data.changedAt) {
            const d = new Date(data.changedAt);
            changedEl.textContent = d.toLocaleDateString(undefined, {
                year: 'numeric', month: 'short', day: 'numeric'
            });
            changedEl.title = d.toLocaleString();
        } else {
            changedEl.textContent = '–';
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// RANK FILTER for results grid
// ─────────────────────────────────────────────────────────────────────────────
let activeRankFilter = null;   // null = show all

function applyRankFilter() {
    const cards = crawlTitansGrid.querySelectorAll('.friend-card');
    let visible = 0;
    cards.forEach(card => {
        const cardRanks = (card.dataset.foundForRanks || '').toLowerCase().split(',');
        const show = !activeRankFilter ||
            cardRanks.some(r => r.trim() === activeRankFilter.toLowerCase());
        card.style.display = show ? '' : 'none';
        if (show) visible++;
    });
    crawlTitansCount.textContent = `${visible} found`;
    crawlEmptyState.classList.toggle('hidden', visible > 0);
}

function renderRankFilterBar() {
    const bar = document.getElementById('rank-filter-bar');
    if (!bar) return;
    // collect all unique ranks from visible cards
    const rankSet = new Set();
    crawlTitansGrid.querySelectorAll('.friend-card').forEach(card => {
        (card.dataset.foundForRanks || '').split(',').forEach(r => {
            if (r.trim()) rankSet.add(r.trim());
        });
    });
    bar.innerHTML = '';
    if (rankSet.size <= 1) { bar.style.display = 'none'; return; }
    bar.style.display = '';
    const allBtn = document.createElement('button');
    allBtn.className = `rank-filter-pill ${!activeRankFilter ? 'active' : ''}`;
    allBtn.textContent = 'All';
    allBtn.onclick = () => { activeRankFilter = null; renderRankFilterBar(); applyRankFilter(); };
    bar.appendChild(allBtn);
    rankSet.forEach(rank => {
        const btn = document.createElement('button');
        btn.className = `rank-filter-pill ${activeRankFilter === rank ? 'active' : ''}`;
        btn.textContent = rank;
        btn.onclick = () => { activeRankFilter = rank; renderRankFilterBar(); applyRankFilter(); };
        bar.appendChild(btn);
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// CARD BUILDER
// ─────────────────────────────────────────────────────────────────────────────
function createFriendCard(friend, isTarget) {
    const card = document.createElement('div');
    card.className = `friend-card ${isTarget ? 'titan-card' : 'other-card'}`;
    if (isTarget && friend.alreadySeen) card.classList.add('already-seen');

    // Store rank info for filter
    const foundRanks = friend.foundForRanks
        ? (Array.isArray(friend.foundForRanks)
            ? friend.foundForRanks.join(',')
            : String(friend.foundForRanks))
        : (friend.ranks || []).map(r => cleanMC(r.displayName)).join(',');
    card.dataset.foundForRanks = foundRanks;

    const ls = formatLastSeen(friend.lastSeen);
    const ranksHTML = buildRanksHTML(friend.ranks, friend.pikaResult, friend.nameStatus);

    const seenBadge = (isTarget && friend.alreadySeen)
        ? `<span class="seen-badge" title="You've seen this account before">👁 Seen</span>`
        : '';

    const neverPlayedBadge = (isTarget && !friend.lastSeen)
        ? `<span class="never-played-badge" title="This account has never logged into Pika">⚠️ Never played</span>`
        : '';

    card.innerHTML = `
        <div class="friend-header">
            <a href="/?username=${friend.username}" class="friend-avatar-link" data-username="${friend.username}">
                <div class="friend-avatar">
                    <img src="https://mc-heads.net/avatar/${friend.username}" alt="${friend.username}" loading="lazy" crossorigin="anonymous">
                </div>
            </a>
            <div class="friend-identity">
                <div class="friend-name-row-inner">
                    <a href="/?username=${friend.username}" class="friend-name-link" data-username="${friend.username}">${friend.username}</a>
                    ${seenBadge}
                </div>
                <div class="friend-meta">
                    <span class="friend-level">LVL ${friend.rank?.level || 0}</span>
                    ${friend.clan?.name ? `<span class="badge clan-badge" style="font-size:.6rem;padding:.1rem .35rem;">${friend.clan.name}</span>` : ''}
                </div>
            </div>
        </div>
        <div class="player-ranks" style="flex-wrap:wrap;">${ranksHTML}</div>
        <div class="friend-status-area">
            <span class="status-label">Last Connection</span>
            <div class="status-indicator">
                <span class="dot ${ls.cls}"></span>
                <span class="status-time" title="${ls.abs}">${ls.text}</span>
            </div>
        </div>
        ${isTarget ? `
        <div class="namedrop-area">
            <div class="namedrop-row">
                <span class="namedrop-label">🕒 Name changed</span>
                <span class="namechange-time namedrop-val">–</span>
            </div>
            <div class="namedrop-row">
                <span class="namedrop-label">⏳ Claimable in</span>
                <span class="namedrop-time namedrop-val">–</span>
            </div>
        </div>` : ''}
        <div class="card-actions">
            <button class="btn-secondary copy-btn" data-username="${friend.username}">📋 Copy</button>
            <button class="btn-secondary skin-btn" data-username="${friend.username}">🔍 Skin</button>
            ${isTarget ? `<button class="btn-secondary premium-btn" data-username="${friend.username}">🔑 Check Pika</button>` : ''}
            ${isTarget ? `<button class="btn-secondary dismiss-btn" data-username="${friend.username}" title="Mark as seen">✖ Dismiss</button>` : ''}
            <a href="https://stats.pika-network.net/player/${friend.username}" target="_blank" rel="noopener noreferrer" class="btn-secondary link-btn">🔗 Stats</a>
            ${isTarget ? `<a href="https://namemc.com/name/${friend.username}" target="_blank" rel="noopener noreferrer" class="btn-secondary link-btn">🏷 NameMC</a>` : ''}
        </div>`;
    return card;
}

function attachCardEvents(container, onNameClick) {
    container.querySelectorAll('.copy-btn').forEach(btn => {
        btn.addEventListener('click', e => {
            const u = e.currentTarget.dataset.username;
            navigator.clipboard.writeText(u).then(() => showToast(`Copied ${u}!`)).catch(() => {});
        });
    });
    container.querySelectorAll('.skin-btn').forEach(btn => {
        btn.addEventListener('click', e => openSkinPreview(e.currentTarget.dataset.username));
    });
    container.querySelectorAll('.premium-btn').forEach(btn => {
        btn.addEventListener('click', e => checkPremium(e.currentTarget.dataset.username, e.currentTarget));
    });
    container.querySelectorAll('.dismiss-btn').forEach(btn => {
        btn.addEventListener('click', e => dismissTitan(e.currentTarget.dataset.username, e.currentTarget));
    });
    container.querySelectorAll('.friend-name-link, .friend-avatar-link').forEach(link => {
        link.addEventListener('click', e => {
            if (e.button === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
                e.preventDefault();
                if (onNameClick) onNameClick(e.currentTarget.dataset.username);
            }
        });
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// TARGET RANKS MULTI-SELECT
// ─────────────────────────────────────────────────────────────────────────────
let currentTargetRank = 'Titan';
let availableRanks    = [];
let selectedRanks     = ['Titan'];

const rankSelectBtn        = document.getElementById('rank-select-btn');
const rankSelectBtnText    = document.getElementById('rank-select-btn-text');
const rankDropdownMenu     = document.getElementById('rank-dropdown-menu');
const rankSearchInput      = document.getElementById('rank-search-input');
const rankBtnSelectAll     = document.getElementById('rank-btn-select-all');
const rankBtnClearAll      = document.getElementById('rank-btn-clear-all');
const rankOptionsContainer = document.getElementById('rank-options-container');
const selectedRanksPills   = document.getElementById('selected-ranks-pills');
const rankPickerLabel      = document.getElementById('rank-picker-label');

if (rankSelectBtn) {
    rankSelectBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        rankDropdownMenu.classList.toggle('hidden');
        rankSelectBtn.parentElement.classList.toggle('open');
        if (!rankDropdownMenu.classList.contains('hidden') && rankSearchInput) rankSearchInput.focus();
    });
}
document.addEventListener('click', (e) => {
    if (rankDropdownMenu && !rankDropdownMenu.contains(e.target) && rankSelectBtn && !rankSelectBtn.contains(e.target)) {
        rankDropdownMenu.classList.add('hidden');
        if (rankSelectBtn) rankSelectBtn.parentElement.classList.remove('open');
    }
});
if (rankSearchInput) {
    rankSearchInput.addEventListener('input', () => {
        const query = rankSearchInput.value.toLowerCase().trim();
        rankOptionsContainer.querySelectorAll('.multi-select-option').forEach(opt => {
            opt.style.display = opt.textContent.toLowerCase().trim().includes(query) ? '' : 'none';
        });
    });
}

const customRankInput  = document.getElementById('custom-rank-input');
const customRankAddBtn = document.getElementById('custom-rank-add-btn');

function addCustomRank() {
    if (!customRankInput) return;
    const newRank = customRankInput.value.trim();
    if (!newRank) return;
    const formattedRank = newRank.charAt(0).toUpperCase() + newRank.slice(1);
    if (!availableRanks.some(r => r.toLowerCase() === formattedRank.toLowerCase())) {
        availableRanks.push(formattedRank);
        availableRanks.sort((a, b) => a.localeCompare(b));
    }
    if (!selectedRanks.some(r => r.toLowerCase() === formattedRank.toLowerCase())) selectedRanks.push(formattedRank);
    customRankInput.value = '';
    renderRankOptions();
    updateMultiSelectUI();
    showToast(`Added "${formattedRank}" to target ranks`);
}
customRankAddBtn?.addEventListener('click', addCustomRank);
customRankInput?.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addCustomRank(); } });

rankBtnSelectAll?.addEventListener('click', () => { selectedRanks = [...availableRanks]; updateMultiSelectUI(); });
rankBtnClearAll?.addEventListener('click',  () => { selectedRanks = []; updateMultiSelectUI(); });

fetch('/api/ranks').then(r => r.json()).then(data => {
    if (data.ranks && data.ranks.length) { availableRanks = data.ranks; renderRankOptions(); updateMultiSelectUI(); }
}).catch(() => {});

function renderRankOptions() {
    if (!rankOptionsContainer) return;
    rankOptionsContainer.innerHTML = '';
    availableRanks.forEach(rank => {
        const label = document.createElement('label');
        label.className = 'multi-select-option';
        label.innerHTML = `<input type="checkbox" value="${rank}" ${selectedRanks.includes(rank) ? 'checked' : ''}><span>${rank}</span>`;
        label.querySelector('input').addEventListener('change', cb => {
            if (cb.target.checked) { if (!selectedRanks.includes(rank)) selectedRanks.push(rank); }
            else selectedRanks = selectedRanks.filter(r => r !== rank);
            updateMultiSelectUI();
        });
        rankOptionsContainer.appendChild(label);
    });
}

function updateMultiSelectUI() {
    rankOptionsContainer?.querySelectorAll('input[type="checkbox"]').forEach(cb => { cb.checked = selectedRanks.includes(cb.value); });
    if (rankSelectBtnText) {
        if (selectedRanks.length === 0) rankSelectBtnText.textContent = 'Select Ranks...';
        else if (selectedRanks.length === 1) rankSelectBtnText.textContent = selectedRanks[0];
        else if (selectedRanks.length === availableRanks.length && availableRanks.length > 0) rankSelectBtnText.textContent = 'All Ranks Selected';
        else rankSelectBtnText.textContent = `${selectedRanks.length} Ranks Selected`;
    }
    if (rankPickerLabel) rankPickerLabel.textContent = `${selectedRanks.length} Selected`;
    if (selectedRanksPills) {
        selectedRanksPills.innerHTML = '';
        selectedRanks.forEach(rank => {
            const pill = document.createElement('span');
            pill.className = 'rank-pill';
            pill.innerHTML = `<span>${rank}</span><button type="button" class="rank-pill-remove" data-rank="${rank}">&times;</button>`;
            pill.querySelector('.rank-pill-remove').addEventListener('click', () => { selectedRanks = selectedRanks.filter(r => r !== rank); updateMultiSelectUI(); });
            selectedRanksPills.appendChild(pill);
        });
    }
    currentTargetRank = selectedRanks.join(', ');
}

function syncRankTarget(rank, ranksList) {
    if (ranksList && Array.isArray(ranksList) && ranksList.length) selectedRanks = [...ranksList];
    else if (rank) selectedRanks = rank.split(',').map(r => r.trim()).filter(Boolean);
    updateMultiSelectUI();
}

// ─────────────────────────────────────────────────────────────────────────────
// SKIN MODAL
// ─────────────────────────────────────────────────────────────────────────────
const skinModal            = document.getElementById('skin-modal');
const modalBackdrop        = document.getElementById('modal-backdrop');
const modalClose           = document.getElementById('modal-close');
const modalSkinBody        = document.getElementById('modal-skin-body');
const modalSkinUsername    = document.getElementById('modal-skin-username');
const modalSkinModel       = document.getElementById('modal-skin-model');
const modalSkinModelDesc   = document.getElementById('modal-skin-model-desc');
const modalSkinRaw         = document.getElementById('modal-skin-raw');
const modalDownloadBtn     = document.getElementById('modal-download-btn');
const modalSubtitle        = document.getElementById('modal-subtitle');

modalClose.addEventListener('click',    () => skinModal.classList.add('hidden'));
modalBackdrop.addEventListener('click', () => skinModal.classList.add('hidden'));

function detectSkinModel(img) {
    if (img.height === 32) return { model: 'Classic (Steve)', description: 'Legacy 64×32 format.' };
    try {
        const c = document.createElement('canvas'); c.width = 64; c.height = 64;
        const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0);
        const data = ctx.getImageData(54, 20, 1, 10).data;
        const slim = [...data].every((v, i) => i % 4 !== 3 || v === 0);
        return slim ? { model: 'Slim (Alex)', description: 'Slim 64×64. Arms are 3px wide.' }
                    : { model: 'Classic (Steve)', description: 'Standard 64×64. Arms are 4px wide.' };
    } catch { return { model: 'Classic (Steve) [Guessed]', description: 'Canvas blocked by CORS.' }; }
}

function openSkinPreview(username) {
    skinModal.classList.remove('hidden');
    modalSkinUsername.textContent = username;
    modalSkinModel.textContent = 'Analyzing...';
    modalSkinModel.classList.remove('accent');
    modalSkinModelDesc.textContent = 'Downloading skin...';
    modalSubtitle.textContent = `Analyzing ${username}...`;
    modalSkinBody.src = `https://mc-heads.net/body/${username}/right`;
    modalSkinRaw.src  = `https://mc-heads.net/skin/${username}`;
    modalDownloadBtn.href = `https://mc-heads.net/skin/${username}`;
    const checker = new Image(); checker.crossOrigin = 'anonymous';
    checker.src = `https://mc-heads.net/skin/${username}`;
    checker.onload = () => {
        const r = detectSkinModel(checker);
        modalSkinModel.textContent = r.model; modalSkinModel.classList.add('accent');
        modalSkinModelDesc.textContent = r.description; modalSubtitle.textContent = 'Skin verified!';
    };
    checker.onerror = () => {
        modalSkinModel.textContent = 'Classic (Steve)'; modalSkinModel.classList.add('accent');
        modalSkinModelDesc.textContent = 'Could not fetch skin — default assumed.'; modalSubtitle.textContent = 'Using default skin.';
    };
}

// ─────────────────────────────────────────────────────────────────────────────
// PREMIUM / CRACKED CHECK (manual card button)
// ─────────────────────────────────────────────────────────────────────────────
async function checkPremium(username, btn) {
    btn.disabled = true;
    btn.textContent = '⏳ Joining Pika...';
    try {
        const res  = await fetch(`/api/check/premium?username=${encodeURIComponent(username)}`);
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        const result = data.result;
        if (result === 'premium') {
            btn.textContent = '✅ Premium'; btn.classList.add('premium-confirmed'); btn.title = data.label;
        } else if (result === 'cracked') {
            btn.textContent = '🔓 Cracked'; btn.classList.add('cracked-confirmed'); btn.title = data.label;
        } else {
            btn.textContent = '❓ Unknown'; btn.title = data.reason || 'Could not determine';
        }
        btn.disabled = false;
        showToast(`${username}: ${data.label}`);
    } catch (err) {
        btn.textContent = '❌ Error'; btn.disabled = false; btn.title = err.message;
        showToast(`Check failed for ${username}: ${err.message}`);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// DISMISS / SEEN LIST
// ─────────────────────────────────────────────────────────────────────────────
let hideSeen = false;

async function dismissTitan(username, btn) {
    btn.disabled = true;
    try {
        await fetch('/api/seen/add', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ usernames: [username] })
        });
        const card = btn.closest('.friend-card');
        if (card) {
            card.classList.add('already-seen');
            const nameRow = card.querySelector('.friend-name-row-inner');
            if (nameRow && !nameRow.querySelector('.seen-badge')) {
                const badge = document.createElement('span');
                badge.className = 'seen-badge'; badge.title = "You've seen this account before"; badge.textContent = '👁 Seen';
                nameRow.appendChild(badge);
            }
            if (hideSeen) {
                card.style.transition = 'opacity 0.3s, transform 0.3s';
                card.style.opacity = '0'; card.style.transform = 'scale(0.95)';
                setTimeout(() => card.remove(), 300);
            }
        }
        btn.textContent = '✔ Dismissed'; btn.classList.add('dismissed');
        showToast(`${username} dismissed`);
    } catch { btn.disabled = false; showToast(`Failed to dismiss ${username}`); }
}

// ─────────────────────────────────────────────────────────────────────────────
// TAB SWITCHING
// ─────────────────────────────────────────────────────────────────────────────
const tabCrawl    = document.getElementById('tab-crawl');
const tabSingle   = document.getElementById('tab-single');
const panelCrawl  = document.getElementById('panel-crawl');
const panelSingle = document.getElementById('panel-single');

function switchTab(tab) {
    if (tab === 'crawl') {
        tabCrawl.classList.add('active'); tabSingle.classList.remove('active');
        panelCrawl.style.display = ''; panelSingle.style.display = 'none';
    } else {
        tabSingle.classList.add('active'); tabCrawl.classList.remove('active');
        panelSingle.style.display = ''; panelCrawl.style.display = 'none';
    }
}
tabCrawl.addEventListener('click',  () => switchTab('crawl'));
tabSingle.addEventListener('click', () => switchTab('single'));

// ─────────────────────────────────────────────────────────────────────────────
// CRAWL ENGINE — client side
// ─────────────────────────────────────────────────────────────────────────────
const crawlSeedInput = document.getElementById('crawl-seed-input');
const crawlStartBtn  = document.getElementById('crawl-start-btn');
const crawlStopBtn   = document.getElementById('crawl-stop-btn');
const crawlResumeBtn = document.getElementById('crawl-resume-btn');
const resumeBanner   = document.getElementById('resume-banner');
const resumeText     = document.getElementById('resume-text');

const csScanned  = document.getElementById('cs-scanned');
const csQueued   = document.getElementById('cs-queued');
const csVisited  = document.getElementById('cs-visited');
const csTitans   = document.getElementById('cs-titans');
const csDot      = document.getElementById('cs-dot');
const csStatus   = document.getElementById('cs-status-label');

const liveTicker    = document.getElementById('live-feed-ticker');
const liveSub       = document.getElementById('live-feed-sub');
const liveIndicator = document.getElementById('live-indicator');

const crawlTitansGrid  = document.getElementById('crawl-titans-grid');
const crawlEmptyState  = document.getElementById('crawl-empty-state');
const crawlTitansCount = document.getElementById('crawl-titans-count');
const crawlExportBtn   = document.getElementById('crawl-export-btn');

let evtSource    = null;
let crawlRunning = false;
const crawlTitanSet = new Set();  // tracks all titans (including pending/hidden)

function updateCrawlStatus(running) {
    crawlRunning = running;
    if (running) {
        csDot.className = 'status-dot online'; csStatus.textContent = 'Running';
        crawlStartBtn.classList.add('hidden'); crawlStopBtn.classList.remove('hidden');
        liveIndicator.classList.add('live-pulse');
    } else {
        csDot.className = 'status-dot offline'; csStatus.textContent = 'Idle';
        crawlStartBtn.classList.remove('hidden'); crawlStopBtn.classList.add('hidden');
        liveIndicator.classList.remove('live-pulse');
    }
}

function updateStats(data) {
    if (data.scanned         !== undefined) csScanned.textContent = data.scanned.toLocaleString();
    if (data.queued          !== undefined) csQueued.textContent  = data.queued.toLocaleString();
    if (data.visited         !== undefined) csVisited.textContent = data.visited.toLocaleString();
    if (data.playerCacheSize !== undefined) {
        const el = document.getElementById('cs-pcache');
        if (el) el.textContent = data.playerCacheSize.toLocaleString();
    }
    if (data.watchlistSize !== undefined) {
        const wl  = document.getElementById('cs-watchlist');
        const wlc = document.getElementById('watchlist-recheck-count');
        if (wl)  wl.textContent  = data.watchlistSize;
        if (wlc) wlc.textContent = data.watchlistSize;
    }
    if (data.titanCount !== undefined) {
        csTitans.textContent = data.titanCount;
        // count will be re-set by applyRankFilter after cards are rendered
    }
}

function addTickerLine(html) {
    document.querySelector('.ticker-idle')?.remove();
    const line = document.createElement('div');
    line.className = 'ticker-line'; line.innerHTML = html;
    liveTicker.insertBefore(line, liveTicker.firstChild);
    while (liveTicker.children.length > 60) liveTicker.removeChild(liveTicker.lastChild);
}

// ── Add a confirmed-premium card to the grid ────────────────────────────────
function addPremiumCard(player) {
    const uLower = player.username.toLowerCase();
    // Remove any existing placeholder card (shouldn't exist but safety)
    const existing = [...crawlTitansGrid.querySelectorAll('.friend-card')]
        .find(c => c.querySelector('.friend-name-link')?.dataset.username?.toLowerCase() === uLower);
    if (existing) existing.remove();

    crawlEmptyState.classList.add('hidden');
    const card = createFriendCard(player, true);
    crawlTitansGrid.insertBefore(card, crawlTitansGrid.firstChild);
    attachCardEvents(card, u => { switchTab('single'); document.getElementById('username-input').value = u; performSingleScan(u); });
    loadNamedropOnCard(card, player.username);
    renderRankFilterBar();
    applyRankFilter();
    card.classList.add('card-new');
    setTimeout(() => card.classList.remove('card-new'), 2000);
}

function connectSSE() {
    if (evtSource) { evtSource.close(); evtSource = null; }
    evtSource = new EventSource('/api/crawl/stream');

    evtSource.onmessage = e => {
        let msg;
        try { msg = JSON.parse(e.data); } catch { return; }

        switch (msg.type) {
            case 'init':
                updateStats(msg);
                updateCrawlStatus(msg.running);
                syncRankTarget(msg.targetRank, msg.targetRanks);
                liveSub.textContent = msg.seed ? `Seed: ${msg.seed}` : 'No active session';
                if (msg.seed && (msg.visited > 0 || msg.queued > 0)) {
                    resumeBanner.classList.remove('hidden');
                    resumeText.textContent = `Previous session: seed "${msg.seed}", ${msg.scanned.toLocaleString()} scanned, ${msg.titanCount} found.`;
                }
                // Only render confirmed premium accounts on init
                if (msg.titans && msg.titans.length) {
                    msg.titans
                        .filter(t => t.pikaResult === 'premium')
                        .forEach(t => { crawlTitanSet.add(t.username.toLowerCase()); addPremiumCard(t); });
                }
                if (msg.pikaScanRunning) {
                    pikaScanStartBtn.classList.add('hidden');
                    pikaScanStopBtn.classList.remove('hidden');
                    pikaScanProgress.classList.remove('hidden');
                    pikaScanStatus.textContent = 'Scan in progress...';
                }
                break;

            case 'reset':
                crawlTitansGrid.innerHTML = '';
                crawlTitanSet.clear();
                crawlEmptyState.classList.remove('hidden');
                crawlTitansCount.textContent = '0 found';
                updateStats({ scanned: 0, queued: 0, visited: 0, titanCount: 0 });
                updateCrawlStatus(false);
                liveTicker.innerHTML = '<span class="ticker-idle">Session reset.</span>';
                liveSub.textContent = 'No active session';
                resumeBanner.classList.add('hidden');
                break;

            case 'bfs_reset':
                updateStats({ scanned: 0, queued: 0, visited: 0 });
                updateCrawlStatus(false);
                liveTicker.innerHTML = `<span class="ticker-idle">New crawl — seed: ${msg.seed}</span>`;
                liveSub.textContent = `Seed: ${msg.seed}`;
                resumeBanner.classList.add('hidden');
                break;

            case 'status':
                updateCrawlStatus(msg.running);
                if (msg.targetRanks || msg.targetRank) syncRankTarget(msg.targetRank, msg.targetRanks);
                if (msg.message) { addTickerLine(`<span style="color:var(--text-muted)">ℹ️ ${msg.message}</span>`); liveSub.textContent = msg.message; }
                break;

            case 'progress':
                updateStats(msg);
                liveSub.textContent = `Processed batch of ${msg.batch?.length || 0}`;
                if (msg.batch && msg.batch.length) {
                    addTickerLine(`<span class="ticker-batch">🔍 Scanned ${msg.batch.length} — total <strong>${msg.scanned.toLocaleString()}</strong>, queue <strong>${msg.queued.toLocaleString()}</strong></span>`);
                }
                break;

            case 'titan_found':
                // Account found but pika not yet verified — track it but don't show card yet
                crawlTitanSet.add(msg.player.username.toLowerCase());
                updateStats(msg);
                addTickerLine(`<span class="ticker-batch">⏳ Candidate: <strong>${msg.player.username}</strong> — verifying on Pika...</span>`);
                liveSub.textContent = `⏳ Verifying: ${msg.player.username}`;
                break;

            case 'pika_update': {
                const uLower = msg.username.toLowerCase();
                if (msg.pikaResult === 'premium') {
                    // Build card with confirmed premium status
                    const existingTitan = msg.player || null;
                    const player = existingTitan || { username: msg.username, pikaResult: 'premium', ranks: [], rank: {}, lastSeen: null, clan: null, hasCustomSkin: false, nameStatus: 'free', foundForRanks: [] };
                    player.pikaResult = 'premium';
                    if (!crawlTitanSet.has(uLower)) crawlTitanSet.add(uLower);
                    addPremiumCard(player);
                    addTickerLine(`<span class="ticker-titan">✅ PREMIUM: <strong>${msg.username}</strong> — confirmed!</span>`);
                    liveSub.textContent = `✅ Premium confirmed: ${msg.username}`;
                } else {
                    // Not premium — remove from set, remove card if somehow shown
                    crawlTitanSet.delete(uLower);
                    const card = [...crawlTitansGrid.querySelectorAll('.friend-card')]
                        .find(c => c.querySelector('.friend-name-link')?.dataset.username?.toLowerCase() === uLower);
                    if (card) {
                        card.style.transition = 'opacity 0.4s, transform 0.4s';
                        card.style.opacity = '0'; card.style.transform = 'scale(0.95)';
                        setTimeout(() => { card.remove(); renderRankFilterBar(); applyRankFilter(); }, 400);
                    }
                    const label = msg.pikaResult === 'banned' ? 'Banned' : msg.pikaResult === 'cracked' ? 'Cracked' : 'Not premium';
                    addTickerLine(`<span style="color:var(--text-muted)">🔓 ${label} — skipped: <strong>${msg.username}</strong></span>`);
                }
                updatePikaScanProgress(msg.done, msg.total);
                break;
            }

            case 'pika_scan_started':
                pikaScanStartBtn.classList.add('hidden');
                pikaScanStopBtn.classList.remove('hidden');
                pikaScanProgress.classList.remove('hidden');
                pikaScanStatus.textContent = `Scanning 0 / ${msg.total}`;
                addTickerLine(`<span>🔑 Pika scan started — ${msg.total} accounts</span>`);
                break;

            case 'pika_scan_progress':
                pikaScanStatus.textContent = `Scanning ${msg.done} / ${msg.total} — ${msg.username}...`;
                updatePikaScanProgress(msg.done, msg.total);
                break;

            case 'pika_scan_done':
                pikaScanStartBtn.classList.remove('hidden');
                pikaScanStopBtn.classList.add('hidden');
                pikaScanStatus.textContent = `Done — ${msg.done} accounts checked`;
                addTickerLine(`<span>✅ Pika scan complete — ${msg.done} checked</span>`);
                break;

            case 'pika_scan_stopped':
                pikaScanStartBtn.classList.remove('hidden');
                pikaScanStopBtn.classList.add('hidden');
                pikaScanStatus.textContent = 'Scan stopped';
                break;

            case 'watchlist_progress':
                pikaScanStatus.textContent = `Rechecking ${msg.username}... (${msg.status})`;
                break;

            case 'watchlist_done':
                pikaScanStatus.textContent = `Watchlist recheck done — ${msg.promoted} promoted, ${msg.watchlistSize} remaining`;
                updateStats({ watchlistSize: msg.watchlistSize });
                addTickerLine(`<span class="ticker-titan">👁 Watchlist: <strong>${msg.promoted}</strong> promoted</span>`);
                break;
        }
    };

    evtSource.onerror = () => { setTimeout(connectSSE, 3000); };
}

connectSSE();

// Controls
crawlStartBtn.addEventListener('click', async () => {
    const seed = crawlSeedInput.value.trim();
    if (!seed) { showToast('Enter a seed username first.'); return; }
    if (selectedRanks.length === 0) { showToast('Select at least one rank.'); return; }
    await fetch('/api/crawl/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seed, ranks: selectedRanks })
    });
    liveSub.textContent = `Seed: ${seed}`;
    resumeBanner.classList.add('hidden');
});

crawlStopBtn.addEventListener('click', () => fetch('/api/crawl/stop', { method: 'POST' }));

crawlResumeBtn?.addEventListener('click', () => {
    fetch('/api/crawl/resume', { method: 'POST' });
    resumeBanner.classList.add('hidden');
});

crawlExportBtn?.addEventListener('click', () => { window.location.href = '/api/crawl/export'; });

// ─────────────────────────────────────────────────────────────────────────────
// PIKA SCAN — manual post-crawl verification
// ─────────────────────────────────────────────────────────────────────────────
const pikaScanStartBtn   = document.getElementById('pika-scan-start-btn');
const pikaScanRevisitBtn = document.getElementById('pika-scan-revisit-btn');
const pikaScanStopBtn    = document.getElementById('pika-scan-stop-btn');
const pikaScanProgress   = document.getElementById('pika-scan-progress');
const pikaScanStatus     = document.getElementById('pika-scan-status');

function updatePikaScanProgress(done, total) {
    if (!total) return;
    const pct = Math.round((done / total) * 100);
    const bar = document.getElementById('pika-scan-bar');
    if (bar) bar.style.width = pct + '%';
    if (pikaScanStatus) pikaScanStatus.textContent = `${done} / ${total} checked`;
}

pikaScanStartBtn?.addEventListener('click', () => {
    fetch('/api/pika/scan/start', { method: 'POST' }).then(r => r.json()).then(d => { if (d.error) showToast(d.error); });
});
pikaScanRevisitBtn?.addEventListener('click', () => {
    if (!confirm('Re-check ALL found accounts on Pika? Non-premium accounts will be removed.')) return;
    fetch('/api/pika/scan/revisit', { method: 'POST' }).then(r => r.json()).then(d => { if (d.error) showToast(d.error); else showToast(`Revisiting ${d.total} accounts...`); });
});
pikaScanStopBtn?.addEventListener('click', () => { fetch('/api/pika/scan/stop', { method: 'POST' }); });

const watchlistRecheckBtn = document.getElementById('watchlist-recheck-btn');
watchlistRecheckBtn?.addEventListener('click', () => {
    const count = parseInt(document.getElementById('watchlist-recheck-count')?.textContent || '0');
    if (count === 0) { showToast('Watchlist is empty.'); return; }
    if (!confirm(`Recheck ${count} watchlist accounts?`)) return;
    watchlistRecheckBtn.disabled = true;
    pikaScanStatus.textContent = `Rechecking ${count} watchlist accounts...`;
    fetch('/api/watchlist/recheck', { method: 'POST' }).then(r => r.json()).then(d => {
        watchlistRecheckBtn.disabled = false;
        if (d.error) { showToast(d.error); pikaScanStatus.textContent = d.error; }
        else showToast(`Rechecking ${d.checking} accounts...`);
    }).catch(() => { watchlistRecheckBtn.disabled = false; });
});

// Hide Seen toggle
const hideSeenBtn  = document.getElementById('hide-seen-btn');
const clearSeenBtn = document.getElementById('clear-seen-btn');

hideSeenBtn?.addEventListener('click', () => {
    hideSeen = !hideSeen;
    hideSeenBtn.textContent = hideSeen ? '👁 Show Seen' : '👁 Hide Seen';
    hideSeenBtn.classList.toggle('active-toggle', hideSeen);
    document.querySelectorAll('#crawl-titans-grid .friend-card.already-seen').forEach(card => { card.style.display = hideSeen ? 'none' : ''; });
});

clearSeenBtn?.addEventListener('click', async () => {
    if (!confirm('Clear your entire seen list?')) return;
    await fetch('/api/seen/clear', { method: 'POST' });
    document.querySelectorAll('.friend-card.already-seen').forEach(card => {
        card.classList.remove('already-seen'); card.style.display = '';
        card.querySelector('.seen-badge')?.remove();
        const btn = card.querySelector('.dismiss-btn');
        if (btn) { btn.textContent = '✖ Dismiss'; btn.disabled = false; btn.classList.remove('dismissed'); }
    });
    showToast('Seen list cleared');
});

// ─────────────────────────────────────────────────────────────────────────────
// SINGLE PLAYER SCAN
// ─────────────────────────────────────────────────────────────────────────────
const searchForm    = document.getElementById('search-form');
const usernameInput = document.getElementById('username-input');
const searchBtn     = document.getElementById('search-btn');
const loadingSection       = document.getElementById('loading-section');
const loadingStatus        = document.getElementById('loading-status');
const loadingDetail        = document.getElementById('loading-detail');
const errorSection         = document.getElementById('error-section');
const errorTitle           = document.getElementById('error-title');
const errorDesc            = document.getElementById('error-desc');
const errorCloseBtn        = document.getElementById('error-close-btn');
const playerProfileSection = document.getElementById('player-profile-section');
const playerAvatar         = document.getElementById('player-avatar');
const playerUsername       = document.getElementById('player-username');
const playerClanBadge      = document.getElementById('player-clan-badge');
const playerRanksContainer = document.getElementById('player-ranks-container');
const playerLevel          = document.getElementById('player-level');
const playerLastSeen       = document.getElementById('player-last-seen');
const statTotalFriends     = document.getElementById('stat-total-friends');
const statTitansFound      = document.getElementById('stat-titans-found');
const controlsSection      = document.getElementById('controls-section');
const friendFilter         = document.getElementById('friend-filter');
const friendSort           = document.getElementById('friend-sort');
const resultsSection       = document.getElementById('results-section');
const titansCount          = document.getElementById('titans-count');
const friendsGrid          = document.getElementById('friends-grid');
const othersGrid           = document.getElementById('others-grid');
const emptyState           = document.getElementById('empty-state');
const emptyStateOthers     = document.getElementById('empty-state-others');
const othersCount          = document.getElementById('others-count');

let currentTitans = [], currentOthers = [], isScanning = false;

errorCloseBtn.addEventListener('click', () => errorSection.classList.add('hidden'));
playerAvatar.parentElement.addEventListener('click', () => { const u = playerUsername.textContent; if (u && u !== 'PlayerName') openSkinPreview(u); });
friendFilter.addEventListener('input',  renderFriendCards);
friendSort.addEventListener('change',   renderFriendCards);

const urlParams = new URLSearchParams(window.location.search);
const queryUser = urlParams.get('username');
if (queryUser) { usernameInput.value = queryUser; switchTab('single'); performSingleScan(queryUser, false); }

window.addEventListener('popstate', () => {
    const u = new URLSearchParams(window.location.search).get('username');
    if (u) { usernameInput.value = u; performSingleScan(u, false); }
    else { [playerProfileSection, controlsSection, resultsSection, errorSection].forEach(el => el.classList.add('hidden')); usernameInput.value = ''; }
});

searchForm.addEventListener('submit', e => { e.preventDefault(); const u = usernameInput.value.trim(); if (u) performSingleScan(u); });

function showError(title, desc) {
    errorTitle.textContent = title; errorDesc.textContent = desc;
    errorSection.classList.remove('hidden'); errorSection.scrollIntoView({ behavior: 'smooth' });
}

async function performSingleScan(username, push = true) {
    if (isScanning) return;
    isScanning = true;
    usernameInput.disabled = true;
    searchBtn.querySelector('span').textContent = 'Scanning...';
    searchBtn.disabled = true;
    [playerProfileSection, controlsSection, resultsSection, errorSection].forEach(el => el.classList.add('hidden'));
    loadingSection.classList.remove('hidden');
    loadingStatus.textContent = `Fetching ${username}'s profile...`;
    loadingDetail.textContent = 'Connecting to Pika Network Stats API...';
    const fill = document.querySelector('.progress-bar-fill');
    fill.style.animation = 'loading-bar 2.5s infinite ease-in-out';

    const steps = [
        { s: 'Inspecting profile...', d: 'Extracting level and stats.' },
        { s: 'Accessing friend lists...', d: 'Locating connected players.' },
        { s: 'Scanning concurrently...', d: 'Parallel profile lookups.' },
        { s: 'Analyzing ranks...', d: `Filtering for "${currentTargetRank}".` },
        { s: 'Checking skins...', d: 'Verifying skin hashes.' },
    ];
    let si = 0;
    const stepTimer = setInterval(() => { if (si < steps.length) { loadingStatus.textContent = steps[si].s; loadingDetail.textContent = steps[si].d; si++; } }, 1200);

    try {
        const res = await fetch(`/api/scan?username=${encodeURIComponent(username)}`);
        clearInterval(stepTimer);
        if (!res.ok) { const e = await res.json(); throw new Error(e.error || 'Scan failed.'); }
        const data = await res.json();
        if (data.targetRank) syncRankTarget(data.targetRank);
        displayScannedPlayer(data.target, data.friendCount, data.titanFriends.length);
        currentTitans = data.titanFriends;
        currentOthers = data.otherFriends || [];
        friendFilter.value = '';
        loadingSection.classList.add('hidden');
        playerProfileSection.classList.remove('hidden');
        controlsSection.classList.remove('hidden');
        resultsSection.classList.remove('hidden');
        if (push) window.history.pushState({ username }, '', `?username=${encodeURIComponent(username)}`);
        renderFriendCards();
    } catch (err) {
        clearInterval(stepTimer);
        loadingSection.classList.add('hidden');
        showError('Scan Failed', err.message);
    } finally {
        isScanning = false;
        usernameInput.disabled = false;
        searchBtn.disabled = false;
        searchBtn.querySelector('span').textContent = 'Scan Friends';
    }
}

function displayScannedPlayer(player, totalFriends, matchesFound) {
    playerAvatar.src = `https://mc-heads.net/avatar/${player.username}`;
    playerUsername.textContent = player.username;
    if (player.clan?.name) { playerClanBadge.textContent = `Clan: ${player.clan.name}`; playerClanBadge.classList.remove('hidden'); }
    else playerClanBadge.classList.add('hidden');
    playerRanksContainer.innerHTML = buildRanksHTML(player.ranks);
    playerLevel.textContent = `Level ${player.rank?.level || 0}`;
    const ls = formatLastSeen(player.lastSeen);
    playerLastSeen.textContent = `Last seen: ${ls.text}`; playerLastSeen.title = ls.abs;
    statTotalFriends.textContent = totalFriends;
    statTitansFound.textContent  = matchesFound;
}

function renderFriendCards() {
    friendsGrid.innerHTML = ''; othersGrid.innerHTML = '';
    const filter = friendFilter.value.toLowerCase().trim();
    const sort   = friendSort.value;

    const sortFn = list => list.sort((a, b) => {
        if (sort === 'no-skin-first') { if (a.hasCustomSkin !== b.hasCustomSkin) return a.hasCustomSkin ? 1 : -1; return a.username.localeCompare(b.username); }
        if (sort === 'name-asc')  return a.username.localeCompare(b.username);
        if (sort === 'name-desc') return b.username.localeCompare(a.username);
        if (sort === 'seen-new')  return (b.lastSeen || 0) - (a.lastSeen || 0);
        if (sort === 'seen-old')  return (a.lastSeen || Infinity) - (b.lastSeen || Infinity);
        return 0;
    });

    const ft = sortFn(currentTitans.filter(f => f.username.toLowerCase().includes(filter)));
    const fo = sortFn(currentOthers.filter(f => f.username.toLowerCase().includes(filter)));

    titansCount.textContent = `${ft.length} found`;
    othersCount.textContent = `${fo.length} found`;

    if (ft.length) { friendsGrid.classList.remove('hidden'); emptyState.classList.add('hidden'); }
    else           { friendsGrid.classList.add('hidden');    emptyState.classList.remove('hidden'); }
    if (fo.length) { othersGrid.classList.remove('hidden'); emptyStateOthers.classList.add('hidden'); }
    else           { othersGrid.classList.add('hidden');    emptyStateOthers.classList.remove('hidden'); }

    ft.forEach(f => friendsGrid.appendChild(createFriendCard(f, true)));
    fo.forEach(f => othersGrid.appendChild(createFriendCard(f, false)));

    attachCardEvents(resultsSection, u => { usernameInput.value = u; performSingleScan(u); });

    // Load namedrop info for all rank-match cards
    ft.forEach(f => {
        const card = [...friendsGrid.querySelectorAll('.friend-card')]
            .find(c => c.querySelector('.friend-name-link')?.dataset.username === f.username);
        if (card) loadNamedropOnCard(card, f.username);
    });
}

}); // end DOMContentLoaded
