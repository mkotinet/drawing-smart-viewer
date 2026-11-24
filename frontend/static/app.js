/**
 * 도면 스마트 뷰어 — PWA 프론트엔드 (Zero-Dependency Vanilla JS)
 * - 60fps 핀치 줌/팬 + 지연 WebP 타일 피라미드 + 태그 BBox 자동 포커스/교차 탐색
 * - 모바일 영구 세션(401 명시 수신 시에만 해제), BFCache/자정 복귀 감지기, PWA A2HS
 */
(() => {
  'use strict';

  const APP_VERSION = '1.0.0';
  const TOKEN_KEY = 'dsv_auth_token';
  const USER_KEY = 'dsv_auth_user';
  const THEME_KEY = 'dsv_theme';

  // 1) PWA beforeinstallprompt 최우선 캡처 (§15)
  let deferredPrompt = null;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    const btn = document.getElementById('btnInstallPwa');
    if (btn) btn.hidden = false;
  });

  // 2) KST 날짜 헬퍼 (§4)
  function getKSTDateString() {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Seoul',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date());
  }

  // 3) 상태
  const state = {
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: JSON.parse(localStorage.getItem(USER_KEY) || 'null'),
    lastKstDate: getKSTDateString(),
    tab: 'drawings',
    category: '',
    subcategory: '',
    q: '',
    page: 1,
    totalPages: 1,
    stats: null,
    sheet: null,
    selectedTagId: null,
    showBoxes: true,
    // 뷰포트 변환
    scale: 1,
    tx: 0,
    ty: 0,
    baseW: 1600,
    baseH: 1130,
    loadedTiles: new Set(),
    pointers: new Map(),
    pinchStartDist: 0,
    pinchStartScale: 1,
    pinchCenter: null,
    dragStart: null,
  };

  const $ = (id) => document.getElementById(id);

  function showToast(msg, ms = 2600) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.hidden = true; }, ms);
  }

  // 4) 인증 API 래퍼 — 오직 명시적 401일 때만 토큰 파기 (§9)
  async function api(url, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
    if (opts.body && !(opts.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(url, { ...opts, headers, credentials: 'same-origin' });
    if (res.status === 401) {
      clearSession();
      throw new Error('AUTH_401');
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function clearSession() {
    state.token = '';
    state.user = null;
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    $('appShell').hidden = true;
    $('authScreen').hidden = false;
  }

  // 5) Safari용 <dialog closedby="any"> 폴백 (modern-web-guidance)
  function initDialogLightDismiss() {
    if (!('closedBy' in HTMLDialogElement.prototype)) {
      const outside = (e, dlg) => {
        const r = dlg.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      for (const dlg of document.querySelectorAll('dialog[closedby="any"]')) {
        let downOut = false;
        dlg.addEventListener('pointerdown', (e) => { downOut = outside(e, dlg); });
        dlg.addEventListener('pointerup', (e) => { if (downOut && outside(e, dlg)) dlg.close(); });
      }
    }
    document.querySelectorAll('[data-close-dialog]').forEach((btn) => {
      btn.addEventListener('click', () => btn.closest('dialog')?.close());
    });
  }

  // 6) 테마
  function applyTheme(theme) {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem(THEME_KEY, theme);
  }

  // 7) 초기 인증 부팅
  async function boot() {
    applyTheme(localStorage.getItem(THEME_KEY) || 'dark');
    initDialogLightDismiss();
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    }

    // 쿠키 또는 저장 토큰으로 세션 확인
    try {
      const me = await api('/api/auth/me');
      state.user = me;
      localStorage.setItem(USER_KEY, JSON.stringify(me));
      enterApp();
    } catch (err) {
      if (err.message === 'AUTH_401') {
        $('authScreen').hidden = false;
        $('appShell').hidden = true;
      } else if (state.user) {
        // 일시적 오프라인/네트워크 지연 시 캐시 세션 유지 (§9)
        enterApp();
        showToast('오프라인 모드 — 캐시된 세션으로 연결 유지 중');
      } else {
        $('authScreen').hidden = false;
      }
    }
  }

  function enterApp() {
    $('authScreen').hidden = true;
    $('appShell').hidden = false;
    $('userDisplay').textContent = state.user?.username || '사용자';
    $('btnAuditLog').hidden = !state.user?.is_admin;
    if (window.innerWidth <= 900) {
      // 모바일에서는 도면 목록을 먼저 보여 선택 유도
      $('sidebar').classList.remove('collapsed');
    }
    loadStatsAndList();
  }

  // 8) 통계 & 카테고리/목록 로드
  async function loadStatsAndList() {
    try {
      const stats = await api('/api/stats');
      state.stats = stats;
      if (stats.meta?.project) {
        $('appProjectName').textContent = stats.meta.project;
        document.title = `${stats.meta.project} — 도면 스마트 뷰어`;
      }
      $('statBadge').textContent = `도면 ${stats.total_sheets.toLocaleString()}장 · 태그 ${stats.unique_tags.toLocaleString()}종`;
      renderCategories(stats.categories || []);
    } catch (err) {
      if (err.message !== 'AUTH_401') showToast(`통계 조회 실패: ${err.message}`);
    }
    await refreshList();
  }

  function renderCategories(cats) {
    const strip = $('categoryStrip');
    strip.innerHTML = '';
    const mkChip = (label, count, active, onClick) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (active ? ' active' : '');
      b.innerHTML = `<span>${escapeHtml(label)}</span><span class="cnt">${count}</span>`;
      b.addEventListener('click', onClick);
      return b;
    };

    const total = state.stats?.total_sheets || 0;
    strip.appendChild(mkChip('전체', total, !state.category, () => {
      state.category = '';
      state.subcategory = '';
      state.page = 1;
      renderCategories(cats);
      renderSubcategories();
      refreshList();
    }));

    for (const c of cats) {
      strip.appendChild(mkChip(c.category, c.count, state.category === c.category, () => {
        state.category = c.category;
        state.subcategory = '';
        state.page = 1;
        renderCategories(cats);
        renderSubcategories();
        refreshList();
      }));
    }
    renderSubcategories();
  }

  function renderSubcategories() {
    const sub = $('subCategoryStrip');
    const found = (state.stats?.categories || []).find((c) => c.category === state.category);
    if (!found || found.subcategories.length <= 1) {
      sub.hidden = true;
      sub.innerHTML = '';
      return;
    }
    sub.hidden = false;
    sub.innerHTML = '';
    const add = (label, cnt, active, val) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (active ? ' active' : '');
      b.innerHTML = `<span>${escapeHtml(label)}</span><span class="cnt">${cnt}</span>`;
      b.addEventListener('click', () => {
        state.subcategory = val;
        state.page = 1;
        renderSubcategories();
        refreshList();
      });
      sub.appendChild(b);
    };
    add('세부 전체', found.count, !state.subcategory, '');
    for (const s of found.subcategories) {
      add(s.name, s.count, state.subcategory === s.name, s.name);
    }
  }

  async function refreshList() {
    const box = $('listContainer');
    const meta = $('listMeta');
    const pager = $('listPager');
    box.innerHTML = '<div class="list-meta">불러오는 중...</div>';

    try {
      if (state.tab === 'tags') {
        $('categoryStrip').hidden = true;
        $('subCategoryStrip').hidden = true;
        pager.hidden = true;
        const data = await api(`/api/tags?q=${encodeURIComponent(state.q)}&limit=120`);
        meta.textContent = `계기·라인 태그 ${data.items.length}건 표시`;
        box.innerHTML = '';
        if (!data.items.length) {
          box.innerHTML = '<div class="list-meta">일치하는 태그가 없습니다.</div>';
          return;
        }
        for (const item of data.items) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'tag-row';
          btn.innerHTML = `
            <div class="sheet-info">
              <div class="tag-code">${escapeHtml(item.tag)}</div>
              <div class="tag-meta">도면 ${item.sheet_count}장 · ${escapeHtml(item.categories || '')}</div>
            </div>
          `;
          btn.addEventListener('click', () => openTagCrossRef(item.tag));
          box.appendChild(btn);
        }
        return;
      }

      $('categoryStrip').hidden = false;
      const params = new URLSearchParams({
        q: state.q,
        category: state.category,
        subcategory: state.subcategory,
        favorites_only: state.tab === 'favorites' ? 'true' : 'false',
        page: String(state.page),
        limit: '40',
      });
      const data = await api(`/api/drawings?${params}`);
      state.totalPages = Math.max(1, Math.ceil(data.total / data.limit));
      meta.textContent = `검색 결과 ${data.total.toLocaleString()}장`;
      box.innerHTML = '';

      if (!data.items.length) {
        box.innerHTML = '<div class="list-meta">조건에 맞는 도면이 없습니다.</div>';
        pager.hidden = true;
        return;
      }

      for (const item of data.items) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'sheet-row' + (state.sheet?.sheet_id === item.sheet_id ? ' active' : '');
        const pageTag = item.total_pages > 1 ? `p.${item.page_no}/${item.total_pages}` : '';
        const dwgLabel = item.drawing_no && item.drawing_no !== item.title ? `${item.drawing_no} · ` : '';
        btn.innerHTML = `
          <img class="sheet-thumb" loading="lazy" decoding="async" src="/api/sheets/${encodeURIComponent(item.sheet_id)}/thumb.webp?v=${item.v}" alt="" />
          <div class="sheet-info">
            <div class="sheet-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
            <div class="sheet-sub">
              <span>${escapeHtml(item.category)}</span>
              <span>${escapeHtml(dwgLabel + pageTag)}</span>
              <span>태그 ${item.tag_count}</span>
            </div>
          </div>
        `;
        btn.addEventListener('click', () => {
          openSheet(item.sheet_id);
          if (window.innerWidth <= 900) $('sidebar').classList.add('collapsed');
        });
        box.appendChild(btn);
      }

      pager.hidden = state.totalPages <= 1;
      $('pagerInfo').textContent = `${state.page} / ${state.totalPages}`;
      $('btnPrevPage').disabled = state.page <= 1;
      $('btnNextPage').disabled = state.page >= state.totalPages;
    } catch (err) {
      if (err.message !== 'AUTH_401') {
        box.innerHTML = `<div class="list-meta">오류: ${escapeHtml(err.message)}</div>`;
      }
    }
  }

  // 9) 도면 열기 + 지연 타일 피라미드 + 오버레이
  async function openSheet(sheetId, focusTagName = null) {
    $('canvasLoading').hidden = false;
    try {
      const sheet = await api(`/api/drawings/${encodeURIComponent(sheetId)}`);
      state.sheet = sheet;
      state.selectedTagId = null;
      state.loadedTiles.clear();

      // 헤더 갱신
      $('viewerHeader').hidden = false;
      $('emptyState').hidden = true;
      $('stageLayer').hidden = false;
      $('zoomControls').hidden = false;
      $('docCatBadge').textContent = sheet.category;
      $('docTitle').textContent = sheet.title;
      $('docSub').textContent = sheet.total_pages > 1
        ? `${sheet.subcategory} · ${sheet.page_no}/${sheet.total_pages}p`
        : sheet.subcategory;
      $('sheetTagCount').textContent = String(sheet.tags.length);

      const favBtn = $('btnFavorite');
      favBtn.classList.toggle('fav-on', !!sheet.is_favorite);

      const pdfBtn = $('btnDownloadPdf');
      if (sheet.has_pdf) {
        pdfBtn.hidden = false;
        pdfBtn.href = `/api/sheets/${encodeURIComponent(sheet.sheet_id)}/pdf`;
      } else {
        pdfBtn.hidden = true;
      }

      // 형제 페이지 네비게이션 (다중 페이지 P&ID 등)
      const sibNav = $('siblingNav');
      if (sheet.siblings && sheet.siblings.length > 1) {
        const idx = sheet.siblings.findIndex((s) => s.sheet_id === sheet.sheet_id);
        sibNav.hidden = false;
        $('siblingLabel').textContent = `${idx + 1} / ${sheet.siblings.length}`;
        $('btnSibPrev').disabled = idx <= 0;
        $('btnSibNext').disabled = idx >= sheet.siblings.length - 1;
        $('btnSibPrev').onclick = () => idx > 0 && openSheet(sheet.siblings[idx - 1].sheet_id);
        $('btnSibNext').onclick = () => idx < sheet.siblings.length - 1 && openSheet(sheet.siblings[idx + 1].sheet_id);
      } else {
        sibNav.hidden = true;
      }

      // 캔버스 기본 크기 설정
      state.baseW = sheet.width_px || 1600;
      state.baseH = sheet.height_px || 1130;
      const stage = $('stageLayer');
      stage.style.width = `${state.baseW}px`;
      stage.style.height = `${state.baseH}px`;

      $('tileGrid').innerHTML = '';
      const img = $('previewImg');
      img.onload = () => {
        $('canvasLoading').hidden = true;
        if (focusTagName) {
          const hit = sheet.tags.find((t) => t.tag.toUpperCase() === focusTagName.toUpperCase());
          if (hit) {
            focusTag(hit);
            return;
          }
        }
        fitToViewport();
      };
      img.onerror = () => {
        $('canvasLoading').hidden = true;
        showToast('도면 이미지 로드에 실패했습니다');
      };
      img.src = `/api/sheets/${encodeURIComponent(sheet.sheet_id)}/preview.webp?v=${sheet.v}`;

      renderOverlayBoxes(sheet.tags);
      renderInspector(sheet.tags);

      // 활성 행 강조
      document.querySelectorAll('.sheet-row').forEach((el) => el.classList.remove('active'));
    } catch (err) {
      $('canvasLoading').hidden = true;
      if (err.message !== 'AUTH_401') showToast(`도면 열기 실패: ${err.message}`);
    }
  }

  function renderOverlayBoxes(tags) {
    const layer = $('overlayLayer');
    layer.innerHTML = '';
    layer.classList.toggle('hidden-boxes', !state.showBoxes);
    for (const t of tags) {
      const div = document.createElement('div');
      div.className = 'tag-box';
      div.dataset.tagId = String(t.id);
      div.title = `${t.tag} (${t.sheet_count}장 연동)`;
      div.style.left = `${t.x0 * 100}%`;
      div.style.top = `${t.y0 * 100}%`;
      div.style.width = `${Math.max((t.x1 - t.x0) * 100, 0.8)}%`;
      div.style.height = `${Math.max((t.y1 - t.y0) * 100, 0.6)}%`;
      div.addEventListener('click', (e) => {
        e.stopPropagation();
        focusTag(t);
      });
      layer.appendChild(div);
    }
  }

  function renderInspector(tags, filterText = '') {
    const list = $('inspectorList');
    const q = filterText.trim().toUpperCase();
    const filtered = q ? tags.filter((t) => t.tag.toUpperCase().includes(q) || (t.func || '').includes(q)) : tags;
    $('inspectorTitle').textContent = `시트 내 태그 (${filtered.length})`;
    list.innerHTML = '';
    if (!filtered.length) {
      list.innerHTML = '<div class="list-meta">추출된 태그가 없습니다.</div>';
      return;
    }
    for (const t of filtered) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tag-row' + (state.selectedTagId === t.id ? ' active' : '');
      btn.innerHTML = `
        <div class="sheet-info">
          <div class="tag-code">${escapeHtml(t.tag)}</div>
          <div class="tag-meta">${escapeHtml(t.kind)}${t.sheet_count > 1 ? ` · 다른 도면 ${t.sheet_count - 1}장` : ''}</div>
        </div>
      `;
      btn.addEventListener('click', () => focusTag(t));
      list.appendChild(btn);
    }
  }

  async function focusTag(tagObj) {
    state.selectedTagId = tagObj.id;
    document.querySelectorAll('.tag-box').forEach((el) => {
      el.classList.toggle('selected', el.dataset.tagId === String(tagObj.id));
    });

    // 해당 태그 좌표로 자동 줌 + 중앙 정렬
    const vp = $('canvasViewport').getBoundingClientRect();
    const cx = ((tagObj.x0 + tagObj.x1) / 2) * state.baseW;
    const cy = ((tagObj.y0 + tagObj.y1) / 2) * state.baseH;
    const fitScale = Math.min(vp.width / state.baseW, vp.height / state.baseH);
    const targetScale = Math.min(Math.max(fitScale * 3.2, 0.85), 3.0);
    state.scale = targetScale;
    state.tx = vp.width / 2 - cx * targetScale;
    state.ty = vp.height / 2 - cy * targetScale;
    applyTransform();

    // 교차 참조 표시
    await loadCrossRefsForTag(tagObj.tag);
  }

  async function openTagCrossRef(tagName) {
    try {
      const data = await api(`/api/tags/${encodeURIComponent(tagName)}/sheets`);
      if (!data.occurrences.length) {
        showToast('해당 태그가 포함된 도면이 없습니다');
        return;
      }
      const first = data.occurrences[0];
      await openSheet(first.sheet_id, tagName);
      $('inspector').hidden = false;
      renderCrossRefBox(data);
      if (window.innerWidth <= 900) $('sidebar').classList.add('collapsed');
    } catch (err) {
      if (err.message !== 'AUTH_401') showToast(`교차 참조 조회 실패: ${err.message}`);
    }
  }

  async function loadCrossRefsForTag(tagName) {
    try {
      const data = await api(`/api/tags/${encodeURIComponent(tagName)}/sheets`);
      renderCrossRefBox(data);
    } catch (_) {}
  }

  function renderCrossRefBox(data) {
    const box = $('crossRefBox');
    if (!data || !data.occurrences.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.innerHTML = `<div class="cross-ref-title">${escapeHtml(data.tag)} — 연관 도면 ${data.occurrences.length}곳</div>`;
    for (const occ of data.occurrences) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cross-ref-item';
      const isCurrent = state.sheet?.sheet_id === occ.sheet_id;
      btn.innerHTML = `
        <span><strong>${escapeHtml(occ.category)}</strong> ${escapeHtml(occ.title)}${occ.page_no > 1 ? ` (p.${occ.page_no})` : ''}</span>
        <span class="tag-meta">${isCurrent ? '현재' : '이동'}</span>
      `;
      btn.addEventListener('click', () => {
        if (isCurrent) {
          focusTag(occ);
        } else {
          openSheet(occ.sheet_id, occ.tag);
        }
      });
      box.appendChild(btn);
    }
  }

  // 10) 60fps 팬/줌 + 고해상도 WebP 타일 지연 로드
  function fitToViewport() {
    const vp = $('canvasViewport').getBoundingClientRect();
    if (!vp.width || !vp.height) return;
    const pad = 20;
    const s = Math.min((vp.width - pad) / state.baseW, (vp.height - pad) / state.baseH);
    state.scale = Math.max(0.08, s);
    state.tx = (vp.width - state.baseW * state.scale) / 2;
    state.ty = (vp.height - state.baseH * state.scale) / 2;
    applyTransform();
  }

  function zoomAt(factor, clientX, clientY) {
    const vp = $('canvasViewport').getBoundingClientRect();
    const ox = clientX - vp.left;
    const oy = clientY - vp.top;
    const next = Math.min(Math.max(state.scale * factor, 0.08), 6.0);
    const ratio = next / state.scale;
    state.tx = ox - (ox - state.tx) * ratio;
    state.ty = oy - (oy - state.ty) * ratio;
    state.scale = next;
    applyTransform();
  }

  function applyTransform() {
    const stage = $('stageLayer');
    stage.style.transform = `translate3d(${state.tx}px, ${state.ty}px, 0) scale(${state.scale})`;
    const vp = $('canvasViewport').getBoundingClientRect();
    const fitScale = Math.min(vp.width / state.baseW, vp.height / state.baseH) || 1;
    $('btnZoomFit').textContent = `${Math.round((state.scale / fitScale) * 100)}%`;
    scheduleVisibleTiles();
  }

  let tileTimer = null;
  function scheduleVisibleTiles() {
    clearTimeout(tileTimer);
    tileTimer = setTimeout(loadVisibleTiles, 70);
  }

  function loadVisibleTiles() {
    const s = state.sheet;
    if (!s || !s.tile_cols || !s.tile_rows || !s.tile_size) return;
    const vp = $('canvasViewport').getBoundingClientRect();
    const fitScale = Math.min(vp.width / state.baseW, vp.height / state.baseH) || 1;
    // 기본 보기보다 1.25배 이상 확대했을 때만 고해상도 WebP 타일 로드 (DS920+ 트래픽 절약)
    if (state.scale < fitScale * 1.25) return;

    const visX0 = Math.max(0, -state.tx / state.scale);
    const visY0 = Math.max(0, -state.ty / state.scale);
    const visX1 = Math.min(state.baseW, (vp.width - state.tx) / state.scale);
    const visY1 = Math.min(state.baseH, (vp.height - state.ty) / state.scale);

    const tileW = s.tile_size;
    const grid = $('tileGrid');
    const c0 = Math.max(0, Math.floor(visX0 / tileW));
    const c1 = Math.min(s.tile_cols - 1, Math.floor(visX1 / tileW));
    const r0 = Math.max(0, Math.floor(visY0 / tileW));
    const r1 = Math.min(s.tile_rows - 1, Math.floor(visY1 / tileW));

    for (let r = r0; r <= r1; r++) {
      for (let c = c0; c <= c1; c++) {
        const key = `${c}_${r}`;
        if (state.loadedTiles.has(key)) continue;
        state.loadedTiles.add(key);
        const im = document.createElement('img');
        im.className = 'tile-img';
        im.decoding = 'async';
        im.draggable = false;
        im.style.left = `${c * tileW}px`;
        im.style.top = `${r * tileW}px`;
        im.style.width = `${Math.min(tileW, state.baseW - c * tileW)}px`;
        im.style.height = `${Math.min(tileW, state.baseH - r * tileW)}px`;
        im.src = `/api/sheets/${encodeURIComponent(s.sheet_id)}/tile/${c}_${r}.webp?v=${s.v}`;
        grid.appendChild(im);
      }
    }
  }

  function initPointerControls() {
    const vp = $('canvasViewport');

    vp.addEventListener('wheel', (e) => {
      if (!state.sheet) return;
      e.preventDefault();
      zoomAt(e.deltaY < 0 ? 1.18 : 1 / 1.18, e.clientX, e.clientY);
    }, { passive: false });

    vp.addEventListener('dblclick', (e) => {
      if (!state.sheet) return;
      zoomAt(1.8, e.clientX, e.clientY);
    });

    vp.addEventListener('pointerdown', (e) => {
      if (!state.sheet || e.target.closest('.zoom-controls')) return;
      vp.setPointerCapture(e.pointerId);
      state.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (state.pointers.size === 1) {
        state.dragStart = { x: e.clientX, y: e.clientY, tx: state.tx, ty: state.ty };
        vp.classList.add('dragging');
      } else if (state.pointers.size === 2) {
        const [a, b] = [...state.pointers.values()];
        state.pinchStartDist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        state.pinchStartScale = state.scale;
        state.pinchCenter = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      }
    });

    vp.addEventListener('pointermove', (e) => {
      if (!state.pointers.has(e.pointerId)) return;
      state.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (state.pointers.size === 1 && state.dragStart) {
        state.tx = state.dragStart.tx + (e.clientX - state.dragStart.x);
        state.ty = state.dragStart.ty + (e.clientY - state.dragStart.y);
        applyTransform();
      } else if (state.pointers.size === 2 && state.pinchCenter) {
        const [a, b] = [...state.pointers.values()];
        const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        const factor = (state.pinchStartScale * (dist / state.pinchStartDist)) / state.scale;
        zoomAt(factor, state.pinchCenter.x, state.pinchCenter.y);
      }
    });

    const endPtr = (e) => {
      state.pointers.delete(e.pointerId);
      if (state.pointers.size < 2) state.pinchCenter = null;
      if (state.pointers.size === 0) {
        state.dragStart = null;
        vp.classList.remove('dragging');
      }
    };
    vp.addEventListener('pointerup', endPtr);
    vp.addEventListener('pointercancel', endPtr);
  }

  // 11) 모바일 백그라운드 복귀 & 버전 감지기 (§5)
  async function checkLifecycleAndVersion() {
    const today = getKSTDateString();
    if (today !== state.lastKstDate) {
      state.lastKstDate = today;
      if (state.user) loadStatsAndList();
    }
    try {
      const v = await fetch('/api/version', { cache: 'no-store' }).then((r) => r.json());
      if (v.version && v.version !== APP_VERSION) {
        const lastReload = Number(sessionStorage.getItem('dsv_reload_at') || 0);
        if (Date.now() - lastReload > 30_000) {
          sessionStorage.setItem('dsv_reload_at', String(Date.now()));
          showToast(`새 버전(v${v.version}) 반영 중...`);
          setTimeout(() => location.reload(), 900);
        }
      }
    } catch (_) {}
  }

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkLifecycleAndVersion();
  });
  window.addEventListener('pageshow', (e) => {
    if (e.persisted) checkLifecycleAndVersion();
  });
  window.addEventListener('online', () => {
    showToast('네트워크 연결이 복구되었습니다');
    if (state.user) loadStatsAndList();
  });

  // 12) 이벤트 바인딩
  function bindEvents() {
    $('loginForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const errEl = $('loginError');
      errEl.textContent = '';
      const btn = $('loginSubmit');
      btn.disabled = true;
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({
            username: $('loginUser').value.trim(),
            password: $('loginPass').value,
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || '로그인 실패');
        state.token = data.token;
        state.user = { username: data.username, role: data.role, is_admin: data.role === 'admin' };
        localStorage.setItem(TOKEN_KEY, data.token);
        localStorage.setItem(USER_KEY, JSON.stringify(state.user));
        $('loginPass').value = '';
        enterApp();
      } catch (err) {
        errEl.textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    });

    $('btnToggleSidebar').addEventListener('click', () => {
      $('sidebar').classList.toggle('collapsed');
      setTimeout(() => state.sheet && applyTransform(), 50);
    });

    $('btnToggleTheme').addEventListener('click', () => {
      const isLight = document.documentElement.getAttribute('data-theme') === 'light';
      applyTheme(isLight ? 'dark' : 'light');
    });

    $('btnInstallPwa').addEventListener('click', async () => {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        await deferredPrompt.userChoice;
        deferredPrompt = null;
        $('btnInstallPwa').hidden = true;
      } else {
        $('pwaDialog').showModal();
      }
    });

    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach((b) => {
          b.classList.toggle('active', b === btn);
          b.setAttribute('aria-selected', String(b === btn));
        });
        state.tab = btn.dataset.tab;
        state.page = 1;
        refreshList();
      });
    });

    let searchDebounce = null;
    const input = $('globalSearchInput');
    input.addEventListener('input', () => {
      $('btnClearSearch').hidden = !input.value;
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => {
        state.q = input.value.trim();
        state.page = 1;
        refreshList();
      }, 220);
    });
    $('globalSearchForm').addEventListener('submit', (e) => {
      e.preventDefault();
      state.q = input.value.trim();
      state.page = 1;
      refreshList();
    });
    $('btnClearSearch').addEventListener('click', () => {
      input.value = '';
      $('btnClearSearch').hidden = true;
      state.q = '';
      state.page = 1;
      refreshList();
    });

    $('btnPrevPage').addEventListener('click', () => { if (state.page > 1) { state.page--; refreshList(); } });
    $('btnNextPage').addEventListener('click', () => { if (state.page < state.totalPages) { state.page++; refreshList(); } });

    $('btnZoomIn').addEventListener('click', () => {
      const r = $('canvasViewport').getBoundingClientRect();
      zoomAt(1.35, r.left + r.width / 2, r.top + r.height / 2);
    });
    $('btnZoomOut').addEventListener('click', () => {
      const r = $('canvasViewport').getBoundingClientRect();
      zoomAt(1 / 1.35, r.left + r.width / 2, r.top + r.height / 2);
    });
    $('btnZoomFit').addEventListener('click', fitToViewport);

    $('btnToggleOverlay').addEventListener('click', () => {
      state.showBoxes = !state.showBoxes;
      $('btnToggleOverlay').classList.toggle('active', state.showBoxes);
      $('overlayLayer').classList.toggle('hidden-boxes', !state.showBoxes);
    });

    $('btnInvertColors').addEventListener('click', () => {
      const on = $('canvasViewport').classList.toggle('inverted');
      $('btnInvertColors').classList.toggle('active', on);
    });

    $('btnFavorite').addEventListener('click', async () => {
      if (!state.sheet) return;
      try {
        const res = await api(`/api/favorites/${encodeURIComponent(state.sheet.sheet_id)}`, { method: 'POST' });
        state.sheet.is_favorite = res.is_favorite;
        $('btnFavorite').classList.toggle('fav-on', res.is_favorite);
        showToast(res.is_favorite ? '즐겨찾기에 추가했습니다' : '즐겨찾기를 해제했습니다');
      } catch (err) {
        if (err.message !== 'AUTH_401') showToast(err.message);
      }
    });

    $('btnToggleInspector').addEventListener('click', () => {
      $('inspector').hidden = !$('inspector').hidden;
    });
    $('btnCloseInspector').addEventListener('click', () => {
      $('inspector').hidden = true;
    });
    $('inspectorSearch').addEventListener('input', (e) => {
      if (state.sheet) renderInspector(state.sheet.tags, e.target.value);
    });

    $('btnAccount').addEventListener('click', () => {
      const m = state.stats?.meta || {};
      $('accountBody').innerHTML = `
        <p><strong>로그인 계정</strong>: ${escapeHtml(state.user?.username || '-')} (${escapeHtml(state.user?.role || '-')})</p>
        <p><strong>앱 버전</strong>: v${APP_VERSION}</p>
        <p><strong>인덱스 빌드</strong>: ${escapeHtml(m.built_at_kst || '동기화 대기 중')}</p>
        <p><strong>총 도면 / 고유 태그</strong>: ${state.stats?.total_sheets || 0}장 / ${state.stats?.unique_tags || 0}종</p>
      `;
      $('accountDialog').showModal();
    });

    $('btnLogout').addEventListener('click', async () => {
      try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
      $('accountDialog').close();
      clearSession();
    });

    $('btnAuditLog').addEventListener('click', async () => {
      $('auditBody').innerHTML = '<p>감사 로그를 불러오는 중...</p>';
      $('auditDialog').showModal();
      try {
        const data = await api('/api/admin/audit-logs?limit=100');
        const rows = data.items.map((r) => `
          <tr>
            <td>${escapeHtml(r.at_kst)}</td>
            <td>${escapeHtml(r.username || '-')}</td>
            <td>${escapeHtml(r.action)}</td>
            <td>${escapeHtml(r.target || '-')}</td>
            <td>${escapeHtml(r.ip || '-')}</td>
          </tr>
        `).join('');
        $('auditBody').innerHTML = `
          <table class="audit-table">
            <thead><tr><th>시각(KST)</th><th>계정</th><th>작업</th><th>대상</th><th>IP</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="5">기록이 없습니다.</td></tr>'}</tbody>
          </table>
        `;
      } catch (err) {
        $('auditBody').innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
      }
    });

    // 키보드 단축키
    window.addEventListener('keydown', (e) => {
      if (e.target.matches('input, textarea')) return;
      if (e.key === '/') { e.preventDefault(); $('globalSearchInput').focus(); }
      else if (e.key === '0' || e.key.toLowerCase() === 'f') { if (state.sheet) fitToViewport(); }
      else if (e.key === '+' || e.key === '=') { $('btnZoomIn').click(); }
      else if (e.key === '-') { $('btnZoomOut').click(); }
    });

    initPointerControls();
  }

  function escapeHtml(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  bindEvents();
  boot();
})();
