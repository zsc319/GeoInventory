/*
 * Project-center safety net.
 *
 * The workspace shell must remain usable even if an unrelated analytical
 * module fails while the large main enhancement bundle is starting.  This
 * script deliberately has no dependency on app.js/enhancements.js globals.
 * It waits briefly for the normal renderer, then performs a small independent
 * local request only when the project list is still in its initial loading
 * state.
 */
(() => {
  'use strict';

  const hub = document.querySelector('#project-hub');
  const list = document.querySelector('#hub-project-list');
  const overview = document.querySelector('#hub-overview');
  if (!hub || !list || !overview) return;

  const format = value => Number(value || 0).toLocaleString('zh-CN');
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[char]));
  const size = project => {
    const bytes = Number(project.total_bytes || 0);
    if (!bytes) return project.total_gb ? `${project.total_gb} GB` : '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
  };
  const date = value => {
    if (!value) return '尚未记录';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value).replace('T', ' ').slice(0, 16) : parsed.toLocaleString('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false
    });
  };
  const isStillLoading = () => !list.querySelector('[data-hub-project]') && !list.querySelector('#hub-add-project');

  function setError(message) {
    list.innerHTML = `<div class="hub-loading"><span class="warning-text">${escapeHtml(message)}</span><button class="secondary compact" type="button" id="hub-fallback-retry">重新读取</button></div>`;
    list.querySelector('#hub-fallback-retry')?.addEventListener('click', () => load(true));
  }

  function leaveHub() {
    hub.classList.add('closed');
    document.body.classList.remove('project-hub-mode');
    window.dispatchEvent(new Event('resize'));
  }

  async function openProject(project, button) {
    if (!project || project.exists === false) return;
    if (project.is_active || !project.path) { leaveHub(); return; }
    const label = button?.querySelector('.hub-project-enter');
    if (button) button.disabled = true;
    if (label) label.textContent = '正在打开…';
    try {
      const response = await fetch('/api/workspace/open', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: project.path })
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || `打开失败 ${response.status}`);
      window.location.reload();
    } catch (error) {
      if (button) button.disabled = false;
      if (label) label.textContent = '打开项目 →';
      window.alert(error.message || '无法打开该工区');
    }
  }

  function render(payload) {
    const projects = Array.isArray(payload.projects) ? payload.projects : [];
    const app = payload.app || {};
    const files = projects.reduce((sum, project) => sum + Number(project.total_files || 0), 0);
    const bytes = projects.reduce((sum, project) => sum + Number(project.total_bytes || 0), 0);
    const active = projects.find(project => project.is_active) || projects[0];
    const appVersion = document.querySelector('#hub-app-version');
    const versionLabel = document.querySelector('#software-version-label');
    if (appVersion) appVersion.textContent = `GeoInventory ${app.version || '—'}`;
    if (versionLabel) versionLabel.textContent = `GeoInventory ${app.version || '—'} · 工区格式 v${app.workspace_format || '—'}`;

    overview.innerHTML = [
      ['项目历史', projects.length, '个分析工区'],
      ['索引资料', format(files), '个文件'],
      ['原始数据', bytes ? size({ total_bytes: bytes }) : '—', '本地引用'],
      ['工区格式', `v${app.workspace_format || '—'}`, '自动原位迭代']
    ].map(row => `<div><span>${row[0]}</span><b>${row[1]}</b><small>${row[2]}</small></div>`).join('');

    const cards = projects.map((project, index) => `<button class="hub-project-card ${project.is_active ? 'active' : ''} ${project.exists === false ? 'missing' : ''}" data-hub-project="${index}" ${project.exists === false ? 'disabled' : ''}>
      <div class="hub-project-visual"><span>${project.is_active ? 'CURRENT WORKSPACE' : project.exists === false ? 'PATH OFFLINE' : 'PROJECT HISTORY'}</span></div>
      <div class="hub-project-content"><header><div><h3>${escapeHtml(project.project_title || project.name || '未命名工区')}</h3><p title="${escapeHtml(project.source_root || project.path || '')}">${escapeHtml(project.source_root || project.path || '临时分析状态')}</p></div><span class="hub-project-state ${project.is_active ? '' : 'history'}">${project.is_active ? '当前工区' : project.version_current ? '可打开' : '待升级'}</span></header>
      <div class="hub-project-metrics"><div><span>文件索引</span><b>${format(project.total_files)}</b></div><div><span>原始容量</span><b>${size(project)}</b></div><div><span>代表样本</span><b>${format(project.representative_count)}</b></div></div>
      <footer><b>.nvt v${escapeHtml(project.format_version || '—')} · App ${escapeHtml(project.app_version || '—')}</b><span>${date(project.scanned_at || project.updated_at)}</span><strong class="hub-project-enter">${project.is_active ? '进入项目 →' : '打开项目 →'}</strong></footer></div></button>`).join('');
    list.innerHTML = `${cards}<button class="hub-project-card add-project" id="hub-fallback-open-workspace"><span>＋</span><b>打开其他分析工区</b><small>选择一个本机 .nvt 文件夹</small></button>`;
    list.querySelectorAll('[data-hub-project]').forEach(button => button.addEventListener('click', () => openProject(projects[Number(button.dataset.hubProject)], button)));
    list.querySelector('#hub-fallback-open-workspace')?.addEventListener('click', () => {
      const workspace = document.querySelector('#workspace-dialog');
      if (workspace?.showModal) workspace.showModal();
      else window.alert('请在项目工作区右上角点击“工区”，选择已有 .nvt 工区。');
    });

    const versionCard = document.querySelector('#hub-version-card');
    if (versionCard) versionCard.innerHTML = active
      ? `<header><b>${escapeHtml(active.project_title || active.name || '当前工区')}</b><span class="hub-version-badge">${active.version_current ? '最新版本' : '打开时升级'}</span></header><div><div><span>工区格式</span><strong>v${escapeHtml(active.format_version || '—')}</strong><small>当前支持 v${escapeHtml(app.workspace_format || '—')}</small></div><div><span>软件版本</span><strong>${escapeHtml(active.app_version || '—')}</strong><small>当前 ${escapeHtml(app.version || '—')}</small></div></div>`
      : '<div class="empty-state">暂无工区版本</div>';
    const timeline = document.querySelector('#hub-timeline');
    if (timeline) timeline.innerHTML = active ? `<div class="hub-timeline-row current"><i></i><div><b>最近打开</b><span>${date(active.last_opened_at || active.updated_at)}</span></div></div><div class="hub-timeline-row"><i></i><div><b>最近目录快照</b><span>${date(active.scanned_at)}</span></div></div><div class="hub-timeline-row"><i></i><div><b>创建分析工区</b><span>${date(active.created_at)}</span></div></div>` : '<div class="empty-state">暂无项目活动</div>';
  }

  let loading = false;
  async function load(force = false) {
    if (loading) return;
    if (!force && !isStillLoading()) return;
    loading = true;
    try {
      const response = await fetch(`/api/projects?_hub=${Date.now()}`, { cache: 'no-store', headers: { 'Cache-Control': 'no-cache' } });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `项目历史读取失败 (${response.status})`);
      render(payload);
    } catch (error) {
      setError(error.message || '项目历史读取失败，请重新读取。');
    } finally {
      loading = false;
    }
  }

  document.querySelector('#hub-refresh-projects')?.addEventListener('click', () => load(true));
  document.querySelector('#back-project-hub')?.addEventListener('click', () => { hub.classList.remove('closed'); document.body.classList.add('project-hub-mode'); load(true); });
  document.querySelector('#hub-brand-info')?.addEventListener('click', () => document.querySelector('#software-about-dialog')?.showModal?.());

  // Let the normal renderer win when it starts correctly.  A stalled initial
  // placeholder is replaced shortly afterwards by this independent request.
  window.setTimeout(() => load(false), 450);
})();
