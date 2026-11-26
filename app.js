(() => {
  'use strict';

  const DATA_URL = './data/prices.json';
  const PAGE_SIZE = 60;
  const DEBOUNCE_MS = 150;
  const DEFAULT_CATEGORY = 'Egyéb';

  const STORAGE_KEYS = {
    theme: 'dohany_theme',
    favorites: 'dohany_favorites',
    view: 'dohany_view',
  };

  const huCollator = new Intl.Collator('hu', { sensitivity: 'base', numeric: true });
  const huNumber = new Intl.NumberFormat('hu-HU');
  const huDateTime = new Intl.DateTimeFormat('hu-HU', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
  const desktopMq = window.matchMedia('(min-width: 1025px)');

  const memoryStorage = new Map();

  function readStorage(key, fallback) {
    try {
      const value = window.localStorage.getItem(key);
      return value !== null ? value : fallback;
    } catch (_) {
      return memoryStorage.has(key) ? memoryStorage.get(key) : fallback;
    }
  }

  function writeStorage(key, value) {
    memoryStorage.set(key, value);
    try {
      window.localStorage.setItem(key, value);
    } catch (_) {
      // Ignore SecurityError / QuotaExceededError in restricted environments
    }
  }

  function loadTheme() {
    const raw = readStorage(STORAGE_KEYS.theme, 'dark');
    return raw === 'light' || raw === 'dark' ? raw : 'dark';
  }

  function loadViewMode() {
    const raw = readStorage(STORAGE_KEYS.view, 'grid');
    return raw === 'grid' || raw === 'table' ? raw : 'grid';
  }

  function loadFavorites() {
    const raw = readStorage(STORAGE_KEYS.favorites, '[]');
    try {
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return new Set();
      return new Set(parsed.filter((id) => typeof id === 'string' && id.length > 0));
    } catch (_) {
      return new Set();
    }
  }

  function saveFavorites(favSet) {
    writeStorage(STORAGE_KEYS.favorites, JSON.stringify(Array.from(favSet)));
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function normalizeWithMap(str) {
    const text = String(str || '');
    let norm = '';
    const map = [];
    let i = 0;

    while (i < text.length) {
      const cp = text.codePointAt(i);
      const charLen = cp > 0xffff ? 2 : 1;
      const slice = text.slice(i, i + charLen);
      const stripped = slice.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

      if (!stripped) {
        if (map.length > 0) {
          map[map.length - 1].end = i + charLen;
        }
      } else {
        let j = 0;
        while (j < stripped.length) {
          const ncp = stripped.codePointAt(j);
          const nLen = ncp > 0xffff ? 2 : 1;
          norm += stripped.slice(j, j + nLen);
          map.push({ start: i, end: i + charLen });
          j += nLen;
        }
      }

      i += charLen;
    }

    return { text, norm, map };
  }

  function normalizeText(str) {
    return String(str || '')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .toLowerCase();
  }

  function tokenizeQuery(query) {
    const raw = normalizeText(query).trim().split(/\s+/).filter(Boolean);
    if (raw.length <= 1) return raw;
    const seen = new Set();
    const deduped = [];
    for (const tok of raw) {
      if (!seen.has(tok)) {
        seen.add(tok);
        deduped.push(tok);
      }
    }
    return deduped;
  }

  function highlightNameHtml(originalName, queryTokens) {
    const { text, norm, map } = normalizeWithMap(originalName);
    if (!queryTokens.length || !text) {
      return escapeHtml(text);
    }

    const ranges = [];
    for (const tok of queryTokens) {
      if (!tok || tok.length > norm.length) continue;
      let pos = 0;
      while (pos <= norm.length - tok.length) {
        const idx = norm.indexOf(tok, pos);
        if (idx === -1) break;
        const start = map[idx].start;
        const end = map[idx + tok.length - 1].end;
        ranges.push([start, end]);
        pos = idx + 1;
      }
    }

    if (!ranges.length) {
      return escapeHtml(text);
    }

    ranges.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const merged = [ranges[0].slice()];
    for (let idx = 1; idx < ranges.length; idx++) {
      const prev = merged[merged.length - 1];
      const cur = ranges[idx];
      if (cur[0] <= prev[1]) {
        if (cur[1] > prev[1]) {
          prev[1] = cur[1];
        }
      } else {
        merged.push(cur.slice());
      }
    }

    let html = '';
    let cursor = 0;
    for (const [start, end] of merged) {
      if (start > cursor) {
        html += escapeHtml(text.slice(cursor, start));
      }
      html += `<mark class="search-mark">${escapeHtml(text.slice(start, end))}</mark>`;
      cursor = end;
    }
    if (cursor < text.length) {
      html += escapeHtml(text.slice(cursor));
    }
    return html;
  }

  function formatTimestamp(value) {
    if (!value) return '—';
    const str = String(value).trim();
    if (!str) return '—';
    if (/^\d{4}-\d{2}-\d{2}$/.test(str)) {
      return str;
    }
    const parsed = new Date(str);
    if (!Number.isNaN(parsed.getTime())) {
      return huDateTime.format(parsed);
    }
    return str;
  }

  function formatPrice(value) {
    return `${huNumber.format(value)} Ft`;
  }

  function isSafeHttpUrl(url) {
    if (typeof url !== 'string') return false;
    return /^https?:\/\//i.test(url.trim());
  }

  const dom = {
    themeToggle: document.getElementById('theme-toggle'),
    metaLastUpdated: document.getElementById('meta-last-updated'),
    metaTotalCount: document.getElementById('meta-total-count'),
    searchInput: document.getElementById('search-input'),
    searchClear: document.getElementById('search-clear'),
    categoryChips: document.getElementById('category-chips'),
    favoritesToggle: document.getElementById('favorites-toggle'),
    favoritesCount: document.getElementById('favorites-count'),
    filterChanged: document.getElementById('filter-changed'),
    changedCount: document.getElementById('changed-count'),
    resultCounter: document.getElementById('result-counter'),
    sortSelect: document.getElementById('sort-select'),
    viewToggle: document.getElementById('view-toggle'),
    viewGridBtn: document.getElementById('view-grid-btn'),
    viewTableBtn: document.getElementById('view-table-btn'),
    mainContent: document.getElementById('main-content'),
    loadingState: document.getElementById('loading-state'),
    errorState: document.getElementById('error-state'),
    errorMessage: document.getElementById('error-message'),
    retryBtn: document.getElementById('retry-btn'),
    emptyState: document.getElementById('empty-state'),
    emptyMessage: document.getElementById('empty-message'),
    resetFiltersBtn: document.getElementById('reset-filters-btn'),
    resultsRegion: document.getElementById('results-region'),
    productsGrid: document.getElementById('products-grid'),
    productsTableWrap: document.getElementById('products-table-wrap'),
    productsTableBody: document.getElementById('products-table-body'),
    paginationControls: document.getElementById('pagination-controls'),
    paginationInfo: document.getElementById('pagination-info'),
    loadMoreBtn: document.getElementById('load-more-btn'),
    sztfhSourceLink: document.getElementById('sztfh-source-link'),
    footerLastUpdated: document.getElementById('footer-last-updated'),
    footerSourceFile: document.getElementById('footer-source-file'),
    footerTotalCount: document.getElementById('footer-total-count'),
  };

  const state = {
    theme: loadTheme(),
    viewMode: loadViewMode(),
    favorites: loadFavorites(),
    items: [],
    itemIds: new Set(),
    filteredItems: [],
    queryTokens: [],
    query: '',
    category: 'all',
    onlyFavorites: false,
    onlyChanged: false,
    sortBy: 'name-asc',
    visibleCount: PAGE_SIZE,
    debounceTimer: null,
    isLoaded: false,
  };

  function applyTheme(theme) {
    const valid = theme === 'light' ? 'light' : 'dark';
    state.theme = valid;
    document.documentElement.setAttribute('data-theme', valid);
    writeStorage(STORAGE_KEYS.theme, valid);

    if (dom.themeToggle) {
      const isLight = valid === 'light';
      dom.themeToggle.setAttribute('aria-pressed', String(isLight));
      const iconEl = dom.themeToggle.querySelector('.theme-icon');
      const labelEl = dom.themeToggle.querySelector('.theme-label');
      if (iconEl) iconEl.textContent = isLight ? '☾' : '☀';
      if (labelEl) labelEl.textContent = isLight ? 'Sötét mód' : 'Világos mód';
    }
  }

  function getEffectiveView() {
    return desktopMq.matches && state.viewMode === 'table' ? 'table' : 'grid';
  }

  function syncViewButtons() {
    const isTable = state.viewMode === 'table';
    if (dom.viewGridBtn) {
      dom.viewGridBtn.classList.toggle('is-active', !isTable);
      dom.viewGridBtn.setAttribute('aria-pressed', String(!isTable));
    }
    if (dom.viewTableBtn) {
      dom.viewTableBtn.classList.toggle('is-active', isTable);
      dom.viewTableBtn.setAttribute('aria-pressed', String(isTable));
    }
    if (dom.resultsRegion) {
      dom.resultsRegion.dataset.view = getEffectiveView();
    }
  }

  function getActiveFavoritesCount() {
    if (!state.items.length) return state.favorites.size;
    let count = 0;
    for (const id of state.favorites) {
      if (state.itemIds.has(id)) count += 1;
    }
    return count;
  }

  function updateCounts() {
    if (dom.favoritesCount) {
      dom.favoritesCount.textContent = huNumber.format(getActiveFavoritesCount());
    }
    if (dom.changedCount) {
      let changed = 0;
      for (const item of state.items) {
        if (item._hasChange) changed += 1;
      }
      dom.changedCount.textContent = huNumber.format(changed);
    }
  }

  function renderDiffBadgesMarkup(item) {
    const parts = [];
    if (item.is_new) {
      parts.push('<span class="badge badge-new">Új</span>');
    }
    if (item.price_diff !== null && item.price_diff > 0) {
      parts.push(`<span class="badge badge-up">+${huNumber.format(item.price_diff)} Ft</span>`);
    } else if (item.price_diff !== null && item.price_diff < 0) {
      parts.push(`<span class="badge badge-down">-${huNumber.format(Math.abs(item.price_diff))} Ft</span>`);
    }
    return parts.join('');
  }

  function renderPrevPriceMarkup(item) {
    if (item.previous_price !== null && item.price_diff !== null && item.price_diff !== 0) {
      return `<del class="prev-price">${formatPrice(item.previous_price)}</del>`;
    }
    return '';
  }

  function createCardElement(item) {
    const isFav = state.favorites.has(item.id);
    const article = document.createElement('article');
    article.className = 'product-card';
    article.dataset.id = item.id;

    const highlightedName = highlightNameHtml(item.name, state.queryTokens);
    const badgesHtml = renderDiffBadgesMarkup(item);
    const prevPriceHtml = renderPrevPriceMarkup(item);
    const validFromHtml = item.valid_from
      ? `<span class="valid-from">Utolsó árváltozás: ${escapeHtml(item.valid_from)}</span>`
      : '';
    const unitHtml = item.unit
      ? `<span class="product-unit">${escapeHtml(item.unit)}</span>`
      : '';

    article.innerHTML = `
      <div class="card-top">
        <div class="card-meta">
          <span class="product-category">${escapeHtml(item.category)}</span>
          ${unitHtml}
        </div>
        <button
          type="button"
          class="fav-btn${isFav ? ' is-favorite' : ''}"
          data-id="${escapeHtml(item.id)}"
          aria-pressed="${isFav ? 'true' : 'false'}"
          aria-label="${isFav ? 'Eltávolítás a kedvencekből' : 'Hozzáadás a kedvencekhez'}: ${escapeHtml(item.name)}"
          title="${isFav ? 'Eltávolítás a kedvencekből' : 'Hozzáadás a kedvencekhez'}"
        >${isFav ? '★' : '☆'}</button>
      </div>
      <h2 class="product-name">${highlightedName}</h2>
      <div class="card-bottom">
        <div class="card-pricing">
          <span class="product-price">${formatPrice(item.price)}</span>
          ${prevPriceHtml}
          ${badgesHtml}
        </div>
        ${validFromHtml ? `<div class="card-validity">${validFromHtml}</div>` : ''}
      </div>
    `;

    return article;
  }

  function createTableRowElement(item) {
    const isFav = state.favorites.has(item.id);
    const tr = document.createElement('tr');
    tr.className = 'product-row';
    tr.dataset.id = item.id;

    const highlightedName = highlightNameHtml(item.name, state.queryTokens);
    const badgesHtml = renderDiffBadgesMarkup(item);
    const prevPriceHtml = renderPrevPriceMarkup(item);

    tr.innerHTML = `
      <td class="cell-fav">
        <button
          type="button"
          class="fav-btn${isFav ? ' is-favorite' : ''}"
          data-id="${escapeHtml(item.id)}"
          aria-pressed="${isFav ? 'true' : 'false'}"
          aria-label="${isFav ? 'Eltávolítás a kedvencekből' : 'Hozzáadás a kedvencekhez'}: ${escapeHtml(item.name)}"
          title="${isFav ? 'Eltávolítás a kedvencekből' : 'Hozzáadás a kedvencekhez'}"
        >${isFav ? '★' : '☆'}</button>
      </td>
      <td class="cell-name"><span class="table-product-name">${highlightedName}</span></td>
      <td class="cell-cat"><span class="product-category">${escapeHtml(item.category)}</span></td>
      <td class="cell-unit"><span class="product-unit">${escapeHtml(item.unit || '—')}</span></td>
      <td class="cell-price"><span class="table-price">${formatPrice(item.price)}</span></td>
      <td class="cell-diff">
        <div class="cell-diff-wrap">
          ${badgesHtml || '<span class="valid-from">—</span>'}
          ${prevPriceHtml}
        </div>
      </td>
      <td class="cell-valid"><span class="valid-from">${escapeHtml(item.valid_from || '—')}</span></td>
    `;

    return tr;
  }

  function renderBatch(startIdx, endIdx, replaceExisting) {
    const effectiveView = getEffectiveView();
    if (dom.resultsRegion) {
      dom.resultsRegion.dataset.view = effectiveView;
    }

    const slice = state.filteredItems.slice(startIdx, endIdx);
    const frag = document.createDocumentFragment();

    if (effectiveView === 'table') {
      dom.productsGrid.hidden = true;
      dom.productsGrid.textContent = '';
      dom.productsTableWrap.hidden = false;
      if (replaceExisting) {
        dom.productsTableBody.textContent = '';
      }
      for (const item of slice) {
        frag.appendChild(createTableRowElement(item));
      }
      dom.productsTableBody.appendChild(frag);
    } else {
      dom.productsTableWrap.hidden = true;
      dom.productsTableBody.textContent = '';
      dom.productsGrid.hidden = false;
      if (replaceExisting) {
        dom.productsGrid.textContent = '';
      }
      for (const item of slice) {
        frag.appendChild(createCardElement(item));
      }
      dom.productsGrid.appendChild(frag);
    }
  }

  function updatePaginationUi() {
    const totalFiltered = state.filteredItems.length;
    const shownCount = Math.min(state.visibleCount, totalFiltered);

    if (totalFiltered > shownCount) {
      const remaining = totalFiltered - shownCount;
      dom.paginationControls.hidden = false;
      dom.paginationInfo.textContent = `Megjelenítve: ${huNumber.format(shownCount)} / ${huNumber.format(totalFiltered)} termék`;
      dom.loadMoreBtn.textContent = `Továbbiak betöltése (még ${huNumber.format(remaining)} tétel)`;
    } else {
      dom.paginationControls.hidden = true;
    }
  }

  function renderCurrentResults() {
    if (!state.isLoaded) return;

    const total = state.items.length;
    const matched = state.filteredItems.length;

    dom.resultCounter.textContent = `${huNumber.format(matched)} / ${huNumber.format(total)} termék`;

    if (matched === 0) {
      dom.productsGrid.hidden = true;
      dom.productsTableWrap.hidden = true;
      dom.paginationControls.hidden = true;

      if (state.onlyFavorites && getActiveFavoritesCount() === 0) {
        dom.emptyMessage.textContent = 'Még nincs kedvenc terméked. Jelölj meg termékeket a ★ csillaggal!';
      } else if (total === 0) {
        dom.emptyMessage.textContent = 'Az árlista jelenleg nem tartalmaz megjeleníthető terméket.';
      } else {
        dom.emptyMessage.textContent = 'Nincs a keresési vagy szűrési feltételeknek megfelelő termék.';
      }
      dom.emptyState.hidden = false;
      return;
    }

    dom.emptyState.hidden = true;
    const endIdx = Math.min(state.visibleCount, matched);
    renderBatch(0, endIdx, true);
    updatePaginationUi();
  }

  function sortItems(items, sortBy) {
    const sorted = items.slice();
    switch (sortBy) {
      case 'name-desc':
        sorted.sort((a, b) => b._azIndex - a._azIndex);
        break;
      case 'price-asc':
        sorted.sort((a, b) => (a.price - b.price) || (a._azIndex - b._azIndex));
        break;
      case 'price-desc':
        sorted.sort((a, b) => (b.price - a.price) || (a._azIndex - b._azIndex));
        break;
      case 'changed-desc':
        sorted.sort((a, b) => {
          const changeCmp = Number(b._hasChange) - Number(a._hasChange);
          if (changeCmp !== 0) return changeCmp;
          const da = a.changed_at || '';
          const db = b.changed_at || '';
          if (da !== db) return db > da ? 1 : -1;
          const va = a.valid_from || '';
          const vb = b.valid_from || '';
          if (va !== vb) return vb > va ? 1 : -1;
          const absCmp = Math.abs(b.price_diff || 0) - Math.abs(a.price_diff || 0);
          if (absCmp !== 0) return absCmp;
          return a._azIndex - b._azIndex;
        });
        break;
      case 'name-asc':
      default:
        sorted.sort((a, b) => a._azIndex - b._azIndex);
        break;
    }
    return sorted;
  }

  function applyFiltersAndRender(resetPagination = true) {
    if (!state.isLoaded) return;

    const tokens = tokenizeQuery(state.query);
    state.queryTokens = tokens;

    const filtered = [];
    for (const item of state.items) {
      if (state.onlyFavorites && !state.favorites.has(item.id)) continue;
      if (state.onlyChanged && !item._hasChange) continue;
      if (state.category !== 'all' && item.category !== state.category) continue;
      if (tokens.length > 0) {
        let allMatch = true;
        for (const tok of tokens) {
          if (!item._search.includes(tok)) {
            allMatch = false;
            break;
          }
        }
        if (!allMatch) continue;
      }
      filtered.push(item);
    }

    state.filteredItems = sortItems(filtered, state.sortBy);
    if (resetPagination) {
      state.visibleCount = PAGE_SIZE;
    }

    renderCurrentResults();
  }

  function buildCategoryChips() {
    const counts = new Map();
    for (const item of state.items) {
      counts.set(item.category, (counts.get(item.category) || 0) + 1);
    }

    if (state.category !== 'all' && !counts.has(state.category)) {
      state.category = 'all';
    }

    const categories = Array.from(counts.entries()).sort((a, b) => {
      return (b[1] - a[1]) || huCollator.compare(a[0], b[0]);
    });

    const frag = document.createDocumentFragment();

    const allBtn = document.createElement('button');
    allBtn.type = 'button';
    allBtn.className = `chip${state.category === 'all' ? ' is-active' : ''}`;
    allBtn.dataset.category = 'all';
    allBtn.setAttribute('role', 'tab');
    allBtn.setAttribute('aria-selected', state.category === 'all' ? 'true' : 'false');
    allBtn.innerHTML = `Összes <span class="chip-count">${huNumber.format(state.items.length)}</span>`;
    frag.appendChild(allBtn);

    for (const [catName, count] of categories) {
      const active = state.category === catName;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `chip${active ? ' is-active' : ''}`;
      btn.dataset.category = catName;
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
      btn.innerHTML = `${escapeHtml(catName)} <span class="chip-count">${huNumber.format(count)}</span>`;
      frag.appendChild(btn);
    }

    dom.categoryChips.textContent = '';
    dom.categoryChips.appendChild(frag);
  }

  function populateMetadata(meta, totalCount) {
    const lastUpdated = formatTimestamp(meta && meta.last_updated);
    const totalFormatted = `${huNumber.format(totalCount)} db`;
    const sourceFile = (meta && meta.source_file) ? String(meta.source_file) : '—';

    dom.metaLastUpdated.textContent = lastUpdated;
    dom.metaTotalCount.textContent = totalFormatted;
    dom.footerLastUpdated.textContent = lastUpdated;
    dom.footerSourceFile.textContent = sourceFile;
    dom.footerTotalCount.textContent = totalFormatted;

    if (meta && isSafeHttpUrl(meta.page_url)) {
      dom.sztfhSourceLink.href = meta.page_url.trim();
    }
  }

  function normalizeDataset(rawItems) {
    const items = rawItems.map((raw) => {
      const id = raw && raw.id != null ? String(raw.id) : '';
      const name = raw && raw.name != null ? String(raw.name) : '';
      const categoryRaw = raw && raw.category != null ? String(raw.category).trim() : '';
      const category = categoryRaw || DEFAULT_CATEGORY;
      const unit = raw && raw.unit != null ? String(raw.unit).trim() : '';
      const priceNum = Number(raw && raw.price);
      const price = Number.isFinite(priceNum) ? Math.round(priceNum) : 0;
      const validFrom = raw && raw.valid_from ? String(raw.valid_from).trim() : '';
      const prevRaw = raw && raw.previous_price;
      const prevPrice = typeof prevRaw === 'number' && Number.isFinite(prevRaw) ? Math.round(prevRaw) : null;
      const diffRaw = raw && raw.price_diff;
      const priceDiff = typeof diffRaw === 'number' && Number.isFinite(diffRaw) ? Math.round(diffRaw) : null;
      const changedAt = raw && raw.changed_at ? String(raw.changed_at).trim() : null;
      const isNew = Boolean(raw && raw.is_new);
      const hasChange = (priceDiff !== null && priceDiff !== 0) || isNew;

      return {
        id,
        name,
        category,
        unit,
        price,
        valid_from: validFrom,
        previous_price: prevPrice,
        price_diff: priceDiff,
        changed_at: changedAt,
        is_new: isNew,
        _hasChange: hasChange,
        _search: `${normalizeText(name)} ${normalizeText(category)}`,
        _azIndex: 0,
      };
    });

    const sortedRefs = items.slice().sort((a, b) => {
      const cmp = huCollator.compare(a.name, b.name);
      if (cmp !== 0) return cmp;
      if (a.id < b.id) return -1;
      if (a.id > b.id) return 1;
      return 0;
    });
    for (let idx = 0; idx < sortedRefs.length; idx++) {
      sortedRefs[idx]._azIndex = idx;
    }

    return items;
  }

  async function loadPrices() {
    state.isLoaded = false;
    dom.loadingState.hidden = false;
    dom.errorState.hidden = true;
    dom.emptyState.hidden = true;
    dom.productsGrid.hidden = true;
    dom.productsTableWrap.hidden = true;
    dom.paginationControls.hidden = true;

    try {
      const res = await fetch(DATA_URL, { cache: 'no-cache' });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      if (!data || !Array.isArray(data.items)) {
        throw new Error('Érvénytelen JSON struktúra');
      }

      const items = normalizeDataset(data.items);
      state.items = items;
      state.itemIds = new Set(items.map((item) => item.id));
      state.isLoaded = true;

      const meta = data.meta && typeof data.meta === 'object' ? data.meta : {};
      const reportedCount = typeof meta.total_count === 'number' && Number.isFinite(meta.total_count)
        ? meta.total_count
        : items.length;

      populateMetadata(meta, reportedCount);
      buildCategoryChips();
      updateCounts();

      dom.loadingState.hidden = true;
      applyFiltersAndRender(true);
    } catch (err) {
      console.warn('Nem sikerült betölteni az árlistát:', err);
      dom.loadingState.hidden = true;
      dom.errorMessage.textContent = 'Nem sikerült betölteni a hivatalos árlistát. Kérjük, próbálja újra.';
      dom.errorState.hidden = false;
    }
  }

  function syncCategoryChipsUi() {
    const buttons = dom.categoryChips.querySelectorAll('.chip[data-category]');
    for (const btn of buttons) {
      const isActive = btn.dataset.category === state.category;
      btn.classList.toggle('is-active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    }
  }

  function clearSearchImmediate() {
    if (state.debounceTimer !== null) {
      clearTimeout(state.debounceTimer);
      state.debounceTimer = null;
    }
    dom.searchInput.value = '';
    dom.searchClear.hidden = true;
    if (state.query !== '') {
      state.query = '';
      applyFiltersAndRender(true);
    }
  }

  function resetAllFilters() {
    if (state.debounceTimer !== null) {
      clearTimeout(state.debounceTimer);
      state.debounceTimer = null;
    }
    state.query = '';
    state.category = 'all';
    state.onlyFavorites = false;
    state.onlyChanged = false;

    dom.searchInput.value = '';
    dom.searchClear.hidden = true;
    dom.favoritesToggle.classList.remove('is-active');
    dom.favoritesToggle.setAttribute('aria-pressed', 'false');
    dom.filterChanged.classList.remove('is-active');
    dom.filterChanged.setAttribute('aria-pressed', 'false');

    syncCategoryChipsUi();
    applyFiltersAndRender(true);
  }

  function toggleFavoriteById(id) {
    if (!id) return;
    const isNowFav = !state.favorites.has(id);
    if (isNowFav) {
      state.favorites.add(id);
    } else {
      state.favorites.delete(id);
    }
    saveFavorites(state.favorites);
    updateCounts();

    if (state.onlyFavorites) {
      applyFiltersAndRender(false);
      return;
    }

    const buttons = dom.mainContent.querySelectorAll('.fav-btn[data-id]');
    for (const btn of buttons) {
      if (btn.dataset.id === id) {
        btn.classList.toggle('is-favorite', isNowFav);
        btn.setAttribute('aria-pressed', isNowFav ? 'true' : 'false');
        btn.textContent = isNowFav ? '★' : '☆';
        const labelPrefix = isNowFav ? 'Eltávolítás a kedvencekből' : 'Hozzáadás a kedvencekhez';
        const item = state.items.find((entry) => entry.id === id);
        if (item) {
          btn.setAttribute('aria-label', `${labelPrefix}: ${item.name}`);
        }
        btn.title = labelPrefix;
      }
    }
  }

  function bindEvents() {
    dom.themeToggle.addEventListener('click', () => {
      applyTheme(state.theme === 'dark' ? 'light' : 'dark');
    });

    dom.searchInput.addEventListener('input', () => {
      const val = dom.searchInput.value;
      dom.searchClear.hidden = val.length === 0;
      if (state.debounceTimer !== null) {
        clearTimeout(state.debounceTimer);
      }
      state.debounceTimer = setTimeout(() => {
        state.debounceTimer = null;
        state.query = dom.searchInput.value;
        applyFiltersAndRender(true);
      }, DEBOUNCE_MS);
    });

    dom.searchInput.addEventListener('search', () => {
      const val = dom.searchInput.value;
      dom.searchClear.hidden = val.length === 0;
      if (val === '' && state.query !== '') {
        clearSearchImmediate();
      }
    });

    dom.searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && dom.searchInput.value.length > 0) {
        event.preventDefault();
        clearSearchImmediate();
      }
    });

    dom.searchClear.addEventListener('click', () => {
      clearSearchImmediate();
      dom.searchInput.focus();
    });

    dom.categoryChips.addEventListener('click', (event) => {
      const chip = event.target.closest('.chip[data-category]');
      if (!chip) return;
      const nextCategory = chip.dataset.category || 'all';
      if (nextCategory === state.category) return;
      state.category = nextCategory;
      syncCategoryChipsUi();
      applyFiltersAndRender(true);
    });

    dom.favoritesToggle.addEventListener('click', () => {
      state.onlyFavorites = !state.onlyFavorites;
      dom.favoritesToggle.classList.toggle('is-active', state.onlyFavorites);
      dom.favoritesToggle.setAttribute('aria-pressed', state.onlyFavorites ? 'true' : 'false');
      applyFiltersAndRender(true);
    });

    dom.filterChanged.addEventListener('click', () => {
      state.onlyChanged = !state.onlyChanged;
      dom.filterChanged.classList.toggle('is-active', state.onlyChanged);
      dom.filterChanged.setAttribute('aria-pressed', state.onlyChanged ? 'true' : 'false');
      applyFiltersAndRender(true);
    });

    dom.sortSelect.addEventListener('change', () => {
      state.sortBy = dom.sortSelect.value || 'name-asc';
      applyFiltersAndRender(true);
    });

    dom.viewToggle.addEventListener('click', (event) => {
      const btn = event.target.closest('.view-btn[data-view]');
      if (!btn) return;
      const nextView = btn.dataset.view === 'table' ? 'table' : 'grid';
      if (nextView === state.viewMode) return;
      state.viewMode = nextView;
      writeStorage(STORAGE_KEYS.view, nextView);
      syncViewButtons();
      renderCurrentResults();
    });

    const handleBreakpointChange = () => {
      if (!state.isLoaded) {
        syncViewButtons();
        return;
      }
      const nextEffective = getEffectiveView();
      if (dom.resultsRegion && dom.resultsRegion.dataset.view !== nextEffective) {
        syncViewButtons();
        renderCurrentResults();
      }
    };
    if (typeof desktopMq.addEventListener === 'function') {
      desktopMq.addEventListener('change', handleBreakpointChange);
    } else if (typeof desktopMq.addListener === 'function') {
      desktopMq.addListener(handleBreakpointChange);
    }

    dom.mainContent.addEventListener('click', (event) => {
      const favBtn = event.target.closest('.fav-btn[data-id]');
      if (!favBtn) return;
      toggleFavoriteById(favBtn.dataset.id);
    });

    dom.loadMoreBtn.addEventListener('click', () => {
      if (!state.isLoaded) return;
      const prevCount = Math.min(state.visibleCount, state.filteredItems.length);
      state.visibleCount += PAGE_SIZE;
      const nextCount = Math.min(state.visibleCount, state.filteredItems.length);
      if (nextCount > prevCount) {
        renderBatch(prevCount, nextCount, false);
      }
      updatePaginationUi();
    });

    dom.resetFiltersBtn.addEventListener('click', () => {
      resetAllFilters();
    });

    dom.retryBtn.addEventListener('click', () => {
      loadPrices();
    });
  }

  function init() {
    applyTheme(state.theme);
    syncViewButtons();
    bindEvents();
    loadPrices();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
