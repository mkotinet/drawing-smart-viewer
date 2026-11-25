/**
 * 도면 스마트 뷰어 — PWA 프론트엔드 (Zero-Dependency Vanilla JS)
 * - 60fps 핀치 줌/팬 + 지연 WebP 타일 피라미드 + 태그 BBox 자동 포커스/교차 탐색
 * - 모바일 영구 세션(401 명시 수신 시에만 해제), BFCache/자정 복귀 감지기, PWA A2HS
 */
(() => {
  'use strict';

  const APP_VERSION = '1.4.0';
  const USER_KEY = 'dsv_auth_user';
  const THEME_KEY = 'dsv_theme';
  const TILE_CACHE_NAME = 'dsv-tiles-v1.0.0';
  const KIND_LABELS = {
    instrument: '계기',
    line_no: '배관 라인',
    equipment: '설비',
    valve: '밸브',
    continuation: '연결 화살표',
  };
  const canonicalizeTag = (s) => String(s || '').toUpperCase().replace(/[^A-Z0-9]+/g, '');
  // 구버전 localStorage 세션 토큰 잔여분 자동 제거 (HttpOnly 쿠키 일원화)
  try { localStorage.removeItem('dsv_auth_token'); } catch (_) {}

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
    user: JSON.parse(localStorage.getItem(USER_KEY) || 'null'),
    lastKstDate: getKSTDateString(),
    tab: 'drawings',
    revision: '',
    category: '',
    subcategory: '',
    tagKind: '',
    inspectorKind: '',
    q: '',
    page: 1,
    totalPages: 1,
    stats: null,
    sheet: null,
    selectedTagId: null,
    selectedTagName: '',
    showBoxes: true,
    pinMode: false,
    pendingPin: null,
    openReqSeq: 0,
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
    pointerMoved: false,
  };

  function syncUrlState() {
    const u = new URL(location.href);
    const setOrDel = (k, v) => (v ? u.searchParams.set(k, v) : u.searchParams.delete(k));
    setOrDel('sheet', state.sheet?.sheet_id || '');
    setOrDel('tag', state.selectedTagName || '');
    setOrDel('q', state.q || '');
    setOrDel('rev', state.revision || '');
    setOrDel('cat', state.category || '');
    history.replaceState(null, '', u.pathname + u.search);
  }

  function renderSnippetHtml(raw) {
    if (!raw) return '';
    return escapeHtml(raw).replace(/\[\[H\]\]/g, '<mark>').replace(/\[\[\/H\]\]/g, '</mark>');
  }

  const $ = (id) => document.getElementById(id);

  function showToast(msg, ms = 2600) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.hidden = true; }, ms);
  }

  // 4) 인증 API 래퍼 — HttpOnly 쿠키 전용, 오직 명시적 401일 때만 세션 파기 (§9)
  async function api(url, opts = {}) {
    const headers = { ...(opts.headers || {}) };
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
      try {
        const j = await res.json();
        detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail || detail);
      } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function clearSession() {
    state.user = null;
    localStorage.removeItem(USER_KEY);
    // 로그아웃/만료 시 공용 기기 브라우저 캐시에서 도면 타일 즉시 파기
    if ('caches' in window) {
      caches.delete(TILE_CACHE_NAME).catch(() => {});
    }
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

  // 6) 테마 (시스템 다크/라이트 변경 감지 포함 — modern-web-guidance: dark-mode)
  function applyTheme(theme, persist = true) {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    if (persist) localStorage.setItem(THEME_KEY, theme);
  }

  const colorSchemeMq = window.matchMedia('(prefers-color-scheme: dark)');
  colorSchemeMq.addEventListener?.('change', (e) => {
    if (!localStorage.getItem(THEME_KEY)) {
      applyTheme(e.matches ? 'dark' : 'light', false);
    }
  });

  // 7) 초기 인증 부팅
  async function boot() {
    const savedTheme = localStorage.getItem(THEME_KEY);
    applyTheme(savedTheme || (colorSchemeMq.matches ? 'dark' : 'light'), false);
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

  let initialDeepLinkHandled = false;

  async function enterApp() {
    $('authScreen').hidden = true;
    $('appShell').hidden = false;
    $('userDisplay').textContent = state.user?.username || '사용자';
    $('btnAuditLog').hidden = !state.user?.is_admin;
    $('btnSyncDrive').hidden = !state.user?.is_admin;

    let initSheet = '';
    let initTag = '';
    if (!initialDeepLinkHandled) {
      initialDeepLinkHandled = true;
      const sp = new URLSearchParams(location.search);
      state.q = (sp.get('q') || '').trim();
      state.revision = (sp.get('rev') || '').trim();
      state.category = (sp.get('cat') || '').trim();
      initSheet = (sp.get('sheet') || '').trim();
      initTag = (sp.get('tag') || '').trim();
      if (state.q) {
        $('globalSearchInput').value = state.q;
        $('btnClearSearch').hidden = false;
      }
    }

    if (window.innerWidth <= 900 && !initSheet) {
      $('sidebar').classList.remove('collapsed');
    } else if (window.innerWidth <= 900 && initSheet) {
      $('sidebar').classList.add('collapsed');
    }

    await loadStatsAndList();
    if (initSheet) {
      await openSheet(initSheet, initTag || null);
    }
  }

  // 8) 통계 & 리비전/카테고리/목록 로드
  async function loadStatsAndList() {
    try {
      const stats = await api('/api/stats');
      state.stats = stats;
      if (stats.meta?.project) {
        $('appProjectName').textContent = stats.meta.project;
        document.title = `${stats.meta.project} — 도면 스마트 뷰어`;
      }
      $('statBadge').textContent = `도면 ${stats.total_sheets.toLocaleString()}장 · 태그 ${stats.unique_tags.toLocaleString()}종`;
      renderRevisions(stats.revisions || []);
      renderCategories(stats.categories || []);
    } catch (err) {
      if (err.message !== 'AUTH_401') showToast(`통계 조회 실패: ${err.message}`);
    }
    await refreshList();
  }

  function renderRevisions(revs) {
    const strip = $('revisionStrip');
    if (!revs || revs.length <= 1) {
      strip.hidden = true;
      strip.innerHTML = '';
      return;
    }
    strip.hidden = false;
    strip.innerHTML = '';
    const mk = (label, count, active, val) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (active ? ' active' : '');
      b.innerHTML = `<span>${escapeHtml(label)}</span><span class="cnt">${count}</span>`;
      b.addEventListener('click', () => {
        state.revision = val;
        state.page = 1;
        syncUrlState();
        renderRevisions(revs);
        refreshList();
      });
      return b;
    };
    strip.appendChild(mk('전체 리비전', state.stats?.total_sheets || 0, !state.revision, ''));
    for (const r of revs) {
      strip.appendChild(mk(r.name, r.count, state.revision === r.name, r.name));
    }
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
        $('revisionStrip').hidden = true;
        $('categoryStrip').hidden = true;
        $('subCategoryStrip').hidden = true;
        pager.hidden = true;
        const kindParam = state.tagKind ? `&kind=${encodeURIComponent(state.tagKind)}` : '';
        const data = await api(`/api/tags?q=${encodeURIComponent(state.q)}${kindParam}&limit=150`);
        renderTagKindStrip(data.kind_counts || {});
        meta.textContent = `계기·배관·설비 ${data.items.length}건 표시`;
        box.innerHTML = '';
        if (!data.items.length) {
          box.innerHTML = '<div class="list-meta">일치하는 계기·배관 태그가 없습니다.</div>';
          return;
        }
        for (const item of data.items) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'tag-row';
          const klabel = KIND_LABELS[item.kind] || item.kind;
          btn.innerHTML = `
            <div class="sheet-info">
              <div class="tag-code"><span class="kind-pill kind-${escapeHtml(item.kind)}">${escapeHtml(klabel)}</span>${escapeHtml(item.tag)}</div>
              <div class="tag-meta">도면 ${item.sheet_count}장 (${item.occurrence_count}회) · ${escapeHtml(item.categories || '')}</div>
            </div>
          `;
          btn.addEventListener('click', () => openTagCrossRef(item.tag));
          box.appendChild(btn);
        }
        return;
      }

      $('tagKindStrip').hidden = true;
      $('categoryStrip').hidden = false;
      if ((state.stats?.revisions || []).length > 1) $('revisionStrip').hidden = false;
      const params = new URLSearchParams({
        q: state.q,
        revision: state.revision,
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
        const snippetHtml = item.snippet ? `<div class="search-snippet">${renderSnippetHtml(item.snippet)}</div>` : '';
        btn.innerHTML = `
          <img class="sheet-thumb" width="58" height="42" loading="lazy" decoding="async" src="/api/sheets/${encodeURIComponent(item.sheet_id)}/thumb.webp?v=${item.v}" alt="" />
          <div class="sheet-info">
            <div class="sheet-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
            <div class="sheet-sub">
              <span>${escapeHtml(item.category)}</span>
              <span>${escapeHtml(dwgLabel + pageTag)}</span>
              <span>태그 ${item.tag_count}</span>
            </div>
            ${snippetHtml}
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

  // 9) 도면 열기 + 지연 타일 피라미드 + 오버레이 + 현장 메모 핀 (경쟁 조건 방어 포함)
  async function openSheet(sheetId, focusTagName = null) {
    const seq = ++state.openReqSeq;
    $('canvasLoading').hidden = false;
    try {
      const sheet = await api(`/api/drawings/${encodeURIComponent(sheetId)}`);
      if (seq !== state.openReqSeq) return; // 더 최신 요청이 진행 중이면 구 응답 무시
      state.sheet = sheet;
      state.selectedTagId = null;
      state.selectedTagName = focusTagName || '';
      state.loadedTiles.clear();
      syncUrlState();

      // 헤더 갱신
      $('viewerHeader').hidden = false;
      $('emptyState').hidden = true;
      $('stageLayer').hidden = false;
      $('zoomControls').hidden = false;
      $('docCatBadge').textContent = sheet.category;
      $('ocrBadge').hidden = sheet.ocr_status !== 'ocr_done';
      $('shxBadge').hidden = sheet.ocr_status === 'ocr_done' || !(sheet.extra_categories || '').includes('SHX');
      $('docTitle').textContent = sheet.title;
      $('docSub').textContent = sheet.total_pages > 1
        ? `${sheet.subcategory} · ${sheet.page_no}/${sheet.total_pages}p`
        : sheet.subcategory;
      const totalItems = (sheet.tags || []).length + (sheet.continuations || []).length;
      $('sheetTagCount').textContent = String(totalItems);

      const favBtn = $('btnFavorite');
      favBtn.classList.toggle('fav-on', !!sheet.is_favorite);

      const pdfBtn = $('btnDownloadPdf');
      if (sheet.has_pdf) {
        pdfBtn.hidden = false;
        pdfBtn.href = `/api/sheets/${encodeURIComponent(sheet.sheet_id)}/pdf`;
      } else {
        pdfBtn.hidden = true;
      }

      // 형제 페이지 네비게이션 + 페이지 번호 즉시 점프
      const sibNav = $('siblingNav');
      if (sheet.siblings && sheet.siblings.length > 1) {
        const idx = sheet.siblings.findIndex((s) => s.sheet_id === sheet.sheet_id);
        sibNav.hidden = false;
        const jumpInp = $('siblingJump');
        jumpInp.max = String(sheet.siblings.length);
        jumpInp.value = String(idx + 1);
        $('siblingLabel').textContent = `/ ${sheet.siblings.length}`;
        $('btnSibPrev').disabled = idx <= 0;
        $('btnSibNext').disabled = idx >= sheet.siblings.length - 1;
        $('btnSibPrev').onclick = () => idx > 0 && openSheet(sheet.siblings[idx - 1].sheet_id);
        $('btnSibNext').onclick = () => idx < sheet.siblings.length - 1 && openSheet(sheet.siblings[idx + 1].sheet_id);
        jumpInp.onchange = () => {
          const n = Math.max(1, Math.min(sheet.siblings.length, parseInt(jumpInp.value, 10) || 1));
          jumpInp.value = String(n);
          if (n - 1 !== idx) openSheet(sheet.siblings[n - 1].sheet_id);
        };
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
        if (seq !== state.openReqSeq) return;
        $('canvasLoading').hidden = true;
        if (focusTagName) {
          const wantCanon = canonicalizeTag(focusTagName);
          const hit = sheet.tags.find((t) =>
            t.tag.toUpperCase() === focusTagName.toUpperCase() ||
            (wantCanon && (t.canonical_tag === wantCanon || canonicalizeTag(t.tag) === wantCanon))
          );
          if (hit) {
            focusTag(hit);
            return;
          }
        }
        fitToViewport();
      };
      img.onerror = () => {
        if (seq !== state.openReqSeq) return;
        $('canvasLoading').hidden = true;
        showToast('도면 이미지 로드에 실패했습니다');
      };
      img.src = `/api/sheets/${encodeURIComponent(sheet.sheet_id)}/preview.webp?v=${sheet.v}`;

      state.inspectorKind = '';
      renderOverlayBoxes(sheet.tags || [], sheet.continuations || []);
      renderContinuations(sheet.continuations || [], sheet.incoming_continuations || []);
      renderPins(sheet.pins || []);
      renderInspectorKinds(sheet.tags || []);
      renderInspector(sheet.tags || [], $('inspectorSearch')?.value || '');

      document.querySelectorAll('.sheet-row').forEach((el) => el.classList.remove('active'));
    } catch (err) {
      if (seq !== state.openReqSeq) return;
      $('canvasLoading').hidden = true;
      if (err.message !== 'AUTH_401') showToast(`도면 열기 실패: ${err.message}`);
    }
  }

  function renderPins(pins) {
    const layer = $('pinLayer');
    const box = $('pinListBox');
    layer.innerHTML = '';
    if (!pins || !pins.length) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    box.hidden = false;
    box.innerHTML = `<div class="cross-ref-title">현장 메모 핀 (${pins.length})</div>`;
    pins.forEach((p, i) => {
      const mk = document.createElement('button');
      mk.type = 'button';
      mk.className = 'pin-marker';
      mk.textContent = String(i + 1);
      mk.title = `${p.note} (${p.username})`;
      mk.style.left = `${p.x * 100}%`;
      mk.style.top = `${p.y * 100}%`;
      mk.addEventListener('click', (e) => {
        e.stopPropagation();
        $('inspector').hidden = false;
        zoomToNormalized(p.x, p.y);
      });
      layer.appendChild(mk);

      const canDel = state.user?.is_admin || state.user?.username === p.username;
      const row = document.createElement('div');
      row.className = 'pin-item';
      row.innerHTML = `
        <div class="pin-item-body">
          <div class="pin-item-note"><strong>#${i + 1}</strong> ${escapeHtml(p.note)}</div>
          <div class="pin-item-meta">${escapeHtml(p.username)} · ${escapeHtml(p.created_at)}</div>
        </div>
        ${canDel ? '<button type="button" class="icon-btn-sm" aria-label="핀 삭제" title="삭제">×</button>' : ''}
      `;
      row.querySelector('.pin-item-body').addEventListener('click', () => zoomToNormalized(p.x, p.y));
      if (canDel) {
        row.querySelector('button').addEventListener('click', async () => {
          try {
            await api(`/api/pins/${p.id}`, { method: 'DELETE' });
            state.sheet.pins = (state.sheet.pins || []).filter((x) => x.id !== p.id);
            renderPins(state.sheet.pins);
            showToast('메모 핀을 삭제했습니다');
          } catch (err) {
            if (err.message !== 'AUTH_401') showToast(`삭제 실패: ${err.message}`);
          }
        });
      }
      box.appendChild(row);
    });
  }

  function zoomToNormalized(nx, ny) {
    const vp = $('canvasViewport').getBoundingClientRect();
    const cx = nx * state.baseW;
    const cy = ny * state.baseH;
    const fitScale = Math.min(vp.width / state.baseW, vp.height / state.baseH);
    const targetScale = Math.min(Math.max(fitScale * 3.0, 0.85), 3.0);
    state.scale = targetScale;
    state.tx = vp.width / 2 - cx * targetScale;
    state.ty = vp.height / 2 - cy * targetScale;
    applyTransform();
  }

  function renderTagKindStrip(kindCounts) {
    const strip = $('tagKindStrip');
    const total = Object.values(kindCounts).reduce((a, b) => a + b, 0);
    if (!total) {
      strip.hidden = true;
      strip.innerHTML = '';
      return;
    }
    strip.hidden = false;
    strip.innerHTML = '';
    const order = [
      ['', '전체', total],
      ['instrument', '계기', kindCounts.instrument || 0],
      ['line_no', '배관 라인', kindCounts.line_no || 0],
      ['equipment', '설비', kindCounts.equipment || 0],
      ['valve', '밸브', kindCounts.valve || 0],
    ];
    for (const [val, label, cnt] of order) {
      if (val && !cnt) continue;
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (state.tagKind === val ? ' active' : '');
      b.innerHTML = `<span>${escapeHtml(label)}</span><span class="cnt">${cnt.toLocaleString()}</span>`;
      b.addEventListener('click', () => {
        state.tagKind = val;
        refreshList();
      });
      strip.appendChild(b);
    }
  }

  function renderInspectorKinds(tags) {
    const strip = $('inspectorKindStrip');
    if (!strip) return;
    const counts = { instrument: 0, line_no: 0, equipment: 0, valve: 0 };
    for (const t of tags) {
      if (counts[t.kind] !== undefined) counts[t.kind]++;
    }
    strip.innerHTML = '';
    const order = [
      ['', '전체', tags.length],
      ['instrument', '계기', counts.instrument],
      ['line_no', '배관', counts.line_no],
      ['equipment', '설비', counts.equipment],
      ['valve', '밸브', counts.valve],
    ];
    for (const [val, label, cnt] of order) {
      if (val && !cnt) continue;
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (state.inspectorKind === val ? ' active' : '');
      b.innerHTML = `<span>${escapeHtml(label)}</span><span class="cnt">${cnt}</span>`;
      b.addEventListener('click', () => {
        state.inspectorKind = val;
        renderInspectorKinds(tags);
        renderInspector(tags, $('inspectorSearch')?.value || '');
      });
      strip.appendChild(b);
    }
  }

  function renderOverlayBoxes(tags, continuations = []) {
    const layer = $('overlayLayer');
    layer.innerHTML = '';
    layer.classList.toggle('hidden-boxes', !state.showBoxes);
    for (const t of tags) {
      const div = document.createElement('div');
      div.className = `tag-box kind-${t.kind || 'instrument'}`;
      div.dataset.tagId = String(t.id);
      const klabel = KIND_LABELS[t.kind] || t.kind;
      div.title = `[${klabel}] ${t.tag} (${t.sheet_count}장 연동${t.source === 'ocr' ? ' · OCR' : ''})`;
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
    for (const c of continuations) {
      const div = document.createElement('div');
      div.className = 'tag-box kind-continuation';
      const targetTxt = c.target_title || c.target_dwg;
      div.title = `[연결 화살표] ${c.direction || ''} ${c.fluid || ''} ${c.label || ''} → ${targetTxt}${c.target_sheet_id ? ' (클릭해 이동)' : ''}`.trim();
      div.style.left = `${c.x0 * 100}%`;
      div.style.top = `${c.y0 * 100}%`;
      div.style.width = `${Math.max((c.x1 - c.x0) * 100, 1.2)}%`;
      div.style.height = `${Math.max((c.y1 - c.y0) * 100, 1.0)}%`;
      div.addEventListener('click', (e) => {
        e.stopPropagation();
        if (c.target_sheet_id) {
          showToast(`연결 도면으로 이동: ${targetTxt}`);
          openSheet(c.target_sheet_id);
        } else {
          $('inspector').hidden = false;
          showToast(`상대 도면(${c.target_dwg})이 현재 인덱스에 없습니다`);
        }
      });
      layer.appendChild(div);
    }
  }

  function renderContinuations(outgoing, incoming) {
    const box = $('continuationBox');
    if (!box) return;
    if ((!outgoing || !outgoing.length) && (!incoming || !incoming.length)) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    box.hidden = false;
    const total = (outgoing?.length || 0) + (incoming?.length || 0);
    box.innerHTML = `<div class="cross-ref-title">도면 간 연결 화살표 (${total})</div>`;
    for (const c of (outgoing || [])) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cross-ref-item';
      const dir = c.direction || '연결';
      const targetLabel = c.target_title ? `${c.target_title}${c.target_page_no > 1 ? ` (p.${c.target_page_no})` : ''}` : c.target_dwg;
      btn.innerHTML = `
        <span><span class="cont-dir">${escapeHtml(dir)}</span><strong>${escapeHtml(c.fluid || 'LINE')}</strong> ${escapeHtml(c.label || targetLabel)}</span>
        <span class="tag-meta">${c.target_sheet_id ? '이동 →' : '위치'}</span>
      `;
      btn.addEventListener('click', () => {
        if (c.target_sheet_id) {
          openSheet(c.target_sheet_id);
        } else {
          zoomToNormalized((c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2);
        }
      });
      box.appendChild(btn);
    }
    for (const inc of (incoming || [])) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cross-ref-item';
      btn.innerHTML = `
        <span><span class="cont-dir">유입</span><strong>${escapeHtml(inc.fluid || 'LINE')}</strong> ${escapeHtml(inc.from_title)}${inc.from_page_no > 1 ? ` (p.${inc.from_page_no})` : ''}</span>
        <span class="tag-meta">← 이동</span>
      `;
      btn.addEventListener('click', () => openSheet(inc.from_sheet_id));
      box.appendChild(btn);
    }
  }

  function renderInspector(tags, filterText = '') {
    const list = $('inspectorList');
    const q = filterText.trim().toUpperCase();
    const qCanon = canonicalizeTag(q);
    const filtered = tags.filter((t) => {
      if (state.inspectorKind && t.kind !== state.inspectorKind) return false;
      if (!q) return true;
      return (
        t.tag.toUpperCase().includes(q) ||
        (qCanon && (t.canonical_tag || '').includes(qCanon)) ||
        (t.func || '').includes(q) ||
        (t.loop_key || '').includes(q)
      );
    });
    $('inspectorTitle').textContent = `시트 내 개체 (${filtered.length})`;
    list.innerHTML = '';
    if (!filtered.length) {
      list.innerHTML = '<div class="list-meta">추출된 계기·배관 태그가 없습니다.</div>';
      return;
    }
    for (const t of filtered) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tag-row' + (state.selectedTagId === t.id ? ' active' : '');
      const klabel = KIND_LABELS[t.kind] || t.kind;
      const ocrFlag = t.source === 'ocr' ? ' · OCR' : '';
      btn.innerHTML = `
        <div class="sheet-info">
          <div class="tag-code"><span class="kind-pill kind-${escapeHtml(t.kind)}">${escapeHtml(klabel)}</span>${escapeHtml(t.tag)}</div>
          <div class="tag-meta">${t.sheet_count > 1 ? `연관 도면 ${t.sheet_count}장` : '단독 등장'}${ocrFlag}</div>
        </div>
      `;
      btn.addEventListener('click', () => focusTag(t));
      list.appendChild(btn);
    }
  }

  async function focusTag(tagObj) {
    state.selectedTagId = tagObj.id;
    state.selectedTagName = tagObj.tag || '';
    syncUrlState();
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
    if (!data || (!data.occurrences.length && !(data.family || []).length)) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.innerHTML = `<div class="cross-ref-title">${escapeHtml(data.tag)} — 공종별 추적 ${data.occurrences.length}곳</div>`;
    for (const occ of data.occurrences) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cross-ref-item';
      const isCurrent = state.sheet?.sheet_id === occ.sheet_id;
      const revTag = occ.revision ? `[${occ.revision}] ` : '';
      btn.innerHTML = `
        <span><strong>${escapeHtml(occ.category)}</strong> ${escapeHtml(revTag + occ.title)}${occ.page_no > 1 ? ` (p.${occ.page_no})` : ''}</span>
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
    if (data.family && data.family.length) {
      const fam = document.createElement('div');
      fam.className = 'family-strip';
      fam.innerHTML = `<div class="family-label">동일 루프/배관 계통 (${escapeHtml(data.loop_key || '연관')} · ${data.family.length}종)</div>`;
      const chips = document.createElement('div');
      chips.className = 'family-chips';
      for (const f of data.family) {
        const cb = document.createElement('button');
        cb.type = 'button';
        cb.className = 'family-chip';
        cb.title = `${KIND_LABELS[f.kind] || f.kind} · 도면 ${f.sheet_count}장`;
        cb.textContent = f.tag;
        cb.addEventListener('click', () => openTagCrossRef(f.tag));
        chips.appendChild(cb);
      }
      fam.appendChild(chips);
      box.appendChild(fam);
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
      if (!state.sheet || e.target.closest('.zoom-controls, .pin-marker, .tag-box')) return;
      vp.setPointerCapture(e.pointerId);
      state.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      state.pointerMoved = false;
      if (state.pointers.size === 1) {
        state.dragStart = { x: e.clientX, y: e.clientY, tx: state.tx, ty: state.ty };
        vp.classList.add('dragging');
      } else if (state.pointers.size === 2) {
        state.pointerMoved = true;
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
        const dx = e.clientX - state.dragStart.x;
        const dy = e.clientY - state.dragStart.y;
        if (Math.hypot(dx, dy) > 6) state.pointerMoved = true;
        state.tx = state.dragStart.tx + dx;
        state.ty = state.dragStart.ty + dy;
        applyTransform();
      } else if (state.pointers.size === 2 && state.pinchCenter) {
        const [a, b] = [...state.pointers.values()];
        const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        const factor = (state.pinchStartScale * (dist / state.pinchStartDist)) / state.scale;
        zoomAt(factor, state.pinchCenter.x, state.pinchCenter.y);
      }
    });

    const endPtr = (e) => {
      const wasSingleClick = state.pointers.size === 1 && !state.pointerMoved && state.pinMode && state.sheet;
      state.pointers.delete(e.pointerId);
      if (state.pointers.size < 2) state.pinchCenter = null;
      if (state.pointers.size === 0) {
        state.dragStart = null;
        vp.classList.remove('dragging');
      }
      if (wasSingleClick && e.type === 'pointerup') {
        const r = vp.getBoundingClientRect();
        const nx = (e.clientX - r.left - state.tx) / (state.scale * state.baseW);
        const ny = (e.clientY - r.top - state.ty) / (state.scale * state.baseH);
        if (nx >= 0 && nx <= 1 && ny >= 0 && ny <= 1) {
          state.pendingPin = { x: nx, y: ny };
          $('pinNoteInput').value = '';
          $('pinDialog').showModal();
          $('pinNoteInput').focus();
        }
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
        state.user = { username: data.username, role: data.role, is_admin: data.role === 'admin' };
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
        $('sidebar').scrollTop = 0;
        $('listContainer').scrollTop = 0;
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
        syncUrlState();
        refreshList();
      }, 220);
    });
    $('globalSearchForm').addEventListener('submit', (e) => {
      e.preventDefault();
      state.q = input.value.trim();
      state.page = 1;
      syncUrlState();
      refreshList();
    });
    $('btnClearSearch').addEventListener('click', () => {
      input.value = '';
      $('btnClearSearch').hidden = true;
      state.q = '';
      state.page = 1;
      syncUrlState();
      refreshList();
    });

    const scrollListToTop = () => {
      if (window.innerWidth <= 900) {
        $('listMeta')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
      } else {
        $('listContainer').scrollTop = 0;
      }
    };
    $('btnPrevPage').addEventListener('click', () => {
      if (state.page > 1) { state.page--; refreshList().then(scrollListToTop); }
    });
    $('btnNextPage').addEventListener('click', () => {
      if (state.page < state.totalPages) { state.page++; refreshList().then(scrollListToTop); }
    });

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

    $('btnCopyLink').addEventListener('click', async () => {
      if (!state.sheet) return;
      syncUrlState();
      try {
        await navigator.clipboard.writeText(location.href);
        showToast('도면 공유 링크를 복사했습니다');
      } catch (_) {
        showToast(location.href);
      }
    });

    $('btnPinMode').addEventListener('click', () => {
      state.pinMode = !state.pinMode;
      $('btnPinMode').classList.toggle('active', state.pinMode);
      $('canvasViewport').classList.toggle('pin-mode', state.pinMode);
      showToast(state.pinMode ? '메모 핀 모드: 도면 위 원하는 위치를 클릭하세요' : '메모 핀 모드를 종료했습니다');
    });

    $('pinForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!state.sheet || !state.pendingPin) return;
      const note = $('pinNoteInput').value.trim();
      if (!note) return;
      try {
        const created = await api(`/api/sheets/${encodeURIComponent(state.sheet.sheet_id)}/pins`, {
          method: 'POST',
          body: JSON.stringify({ x: state.pendingPin.x, y: state.pendingPin.y, note }),
        });
        state.sheet.pins = [...(state.sheet.pins || []), created];
        renderPins(state.sheet.pins);
        $('inspector').hidden = false;
        $('pinDialog').close();
        state.pendingPin = null;
        state.pinMode = false;
        $('btnPinMode').classList.remove('active');
        $('canvasViewport').classList.remove('pin-mode');
        showToast('현장 메모 핀을 저장했습니다');
      } catch (err) {
        if (err.message !== 'AUTH_401') showToast(`핀 저장 실패: ${err.message}`);
      }
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

    async function loadAdminUsers() {
      const sec = $('adminUsersSection');
      if (!state.user?.is_admin) {
        sec.hidden = true;
        return;
      }
      sec.hidden = false;
      sec.innerHTML = '<h2>팀원 계정 관리</h2><p>불러오는 중…</p>';
      try {
        const data = await api('/api/admin/users');
        const rowsHtml = data.items.map((u) => {
          const isSelf = u.username === state.user.username;
          return `
            <div class="admin-user-row">
              <span><strong>${escapeHtml(u.username)}</strong> (${escapeHtml(u.role)})</span>
              ${isSelf ? '<span class="tag-meta">현재 관리자</span>' : `<button type="button" class="btn btn-ghost btn-sm" data-del-user="${escapeHtml(u.username)}">삭제</button>`}
            </div>
          `;
        }).join('');
        sec.innerHTML = `
          <h2>팀원 계정 관리 (${data.items.length})</h2>
          <div>${rowsHtml}</div>
          <form id="addUserForm" class="admin-user-form">
            <input id="newUserName" type="text" required placeholder="새 아이디" autocomplete="off" />
            <input id="newUserPass" type="password" required minlength="4" placeholder="비밀번호 (4자+)" autocomplete="new-password" />
            <select id="newUserRole" aria-label="권한">
              <option value="engineer">엔지니어</option>
              <option value="admin">관리자</option>
            </select>
            <button type="submit" class="btn btn-primary btn-sm">추가</button>
          </form>
        `;
        sec.querySelectorAll('[data-del-user]').forEach((btn) => {
          btn.addEventListener('click', async () => {
            const target = btn.dataset.delUser;
            if (!confirm(`계정 "${target}"을(를) 삭제하시겠습니까?`)) return;
            try {
              await api(`/api/admin/users/${encodeURIComponent(target)}`, { method: 'DELETE' });
              showToast(`계정 ${target} 삭제 완료`);
              loadAdminUsers();
            } catch (err) {
              if (err.message !== 'AUTH_401') showToast(`삭제 실패: ${err.message}`);
            }
          });
        });
        $('addUserForm').addEventListener('submit', async (e) => {
          e.preventDefault();
          try {
            await api('/api/admin/users', {
              method: 'POST',
              body: JSON.stringify({
                username: $('newUserName').value.trim(),
                password: $('newUserPass').value,
                role: $('newUserRole').value,
              }),
            });
            showToast('새 계정을 등록했습니다');
            loadAdminUsers();
          } catch (err) {
            if (err.message !== 'AUTH_401') showToast(`등록 실패: ${err.message}`);
          }
        });
      } catch (err) {
        sec.innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
      }
    }

    $('btnAccount').addEventListener('click', () => {
      const m = state.stats?.meta || {};
      $('accountBody').innerHTML = `
        <p><strong>로그인 계정</strong>: ${escapeHtml(state.user?.username || '-')} (${escapeHtml(state.user?.role || '-')})</p>
        <p><strong>앱 버전</strong>: v${APP_VERSION}</p>
        <p><strong>인덱스 빌드</strong>: ${escapeHtml(m.built_at_kst || '동기화 대기 중')}</p>
        <p><strong>총 도면 / 고유 태그</strong>: ${state.stats?.total_sheets || 0}장 / ${state.stats?.unique_tags || 0}종</p>
      `;
      $('accountDialog').showModal();
      loadAdminUsers();
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

    // 구글 드라이브 자동 동기화 패널 (관리자 전용)
    let syncPollTimer = null;
    let wasSyncing = false;
    const stopSyncPoll = () => {
      if (syncPollTimer) { clearInterval(syncPollTimer); syncPollTimer = null; }
    };

    async function loadSyncPanel() {
      try {
        const s = await api('/api/admin/sync/status');
        const running = s.state && s.state !== 'idle';
        const stateLabel = !s.worker_enabled
          ? '워커 미연결'
          : !s.worker_online
            ? '워커 오프라인'
            : s.state === 'syncing_drive'
              ? '구글 드라이브 변경 확인 중...'
              : s.state === 'indexing'
                ? (s.progress?.phase || '신규 도면 타일링 중...')
                : '대기 중 (자동 감시 활성)';
        const pillCls = s.last_error ? 'error' : (running ? 'running' : '');
        const r = s.last_result || {};
        const alertInfo = s.last_alert
          ? (s.last_alert.sent
              ? `전송 성공 (${s.last_alert.at_kst}, HTTP ${s.last_alert.http_status})`
              : `전송 실패 (${s.last_alert.error || s.last_alert.reason || '-'})`)
          : (s.n8n_configured ? '대기 중 (변경 감지 시 자동 전송)' : '미설정');
        const addedHtml = (r.added_files && r.added_files.length)
          ? `<div><strong>최근 추가/변경 파일 (${r.added_pdfs}건):</strong><ul class="sync-files">${r.added_files.slice(0, 12).map((f) => `<li>${escapeHtml(f)}</li>`).join('')}</ul></div>`
          : '';
        $('syncBody').innerHTML = `
          <div class="sync-meta-list">
            <div><span class="sync-pill ${pillCls}">${escapeHtml(stateLabel)}</span></div>
            <div><strong>감시 대상</strong>: <code>${escapeHtml(s.remote_path || '-')}</code></div>
            <div><strong>자동 감시 주기</strong>: ${escapeHtml(String(s.interval_min || 15))}분마다 증분 확인</div>
            <div><strong>최근 점검 (KST)</strong>: ${escapeHtml(s.last_checked_kst || '기록 없음')}</div>
            <div><strong>최근 반영 (KST)</strong>: ${escapeHtml(s.last_updated_kst || '변경 없음')}</div>
            <div><strong>최근 결과</strong>: 전체 ${Number(r.total_sheets ?? state.stats?.total_sheets ?? 0).toLocaleString()}장 (추가 +${Number(r.added_sheets || 0)}장 · 삭제 -${Number(r.removed_sheets || 0)}장)</div>
            <div><strong>n8n 실시간 알림</strong>: ${escapeHtml(alertInfo)}</div>
            ${s.last_error ? `<p class="form-error">최근 오류: ${escapeHtml(s.last_error)}</p>` : ''}
            ${addedHtml}
          </div>
        `;
        $('btnTriggerSync').disabled = !s.worker_online || running;
        $('btnTestAlert').disabled = !s.worker_online || !s.n8n_configured;

        if (wasSyncing && !running) {
          wasSyncing = false;
          stopSyncPoll();
          loadStatsAndList();
          showToast(r.status === 'updated' ? `도면 동기화 완료 (+${r.added_sheets || 0}장)` : '드라이브 점검 완료 (변경 없음)');
        } else if (running) {
          wasSyncing = true;
          if (!syncPollTimer && $('syncDialog').open) {
            syncPollTimer = setInterval(loadSyncPanel, 3000);
          }
        }
      } catch (err) {
        $('syncBody').innerHTML = `<p class="form-error">${escapeHtml(err.message)}</p>`;
      }
    }

    $('btnSyncDrive').addEventListener('click', () => {
      $('syncBody').innerHTML = '<p>동기화 상태를 불러오는 중...</p>';
      $('syncDialog').showModal();
      loadSyncPanel();
    });
    $('syncDialog').addEventListener('close', stopSyncPoll);

    $('btnTriggerSync').addEventListener('click', async () => {
      $('btnTriggerSync').disabled = true;
      try {
        const res = await api('/api/admin/sync/trigger', { method: 'POST' });
        showToast(res.status === 'already_running' ? '이미 동기화가 진행 중입니다' : '구글 드라이브 동기화를 시작했습니다');
        wasSyncing = true;
        loadSyncPanel();
        if (!syncPollTimer) syncPollTimer = setInterval(loadSyncPanel, 3000);
      } catch (err) {
        showToast(`동기화 요청 실패: ${err.message}`);
        $('btnTriggerSync').disabled = false;
      }
    });

    $('btnTestAlert').addEventListener('click', async () => {
      $('btnTestAlert').disabled = true;
      try {
        const res = await api('/api/admin/sync/test-alert', { method: 'POST' });
        showToast(res.sent ? 'n8n 실시간 알림 테스트 전송 완료' : `알림 전송 실패: ${res.error || res.reason}`);
        loadSyncPanel();
      } catch (err) {
        showToast(`알림 테스트 실패: ${err.message}`);
      } finally {
        $('btnTestAlert').disabled = false;
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
