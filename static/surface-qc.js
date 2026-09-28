(() => {
  const state = {quality: null, comparison: null, bound: false};
  const $ = selector => document.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const number = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:digits});
  const post = (path, body) => api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const metric = (name, value, unit = '') => `<div><span>${esc(name)}</span><b>${esc(value)}${unit}</b></div>`;

  function downloadCsv(filename, rows) {
    const cell = value => {
      let text = String(value ?? '');
      if (/^[=+@-]/.test(text) && !/^-?\d+(\.\d+)?$/.test(text)) text = `'${text}`;
      return `"${text.replace(/"/g, '""')}"`;
    };
    const content = '\ufeff' + rows.map(row => row.map(cell).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([content], {type:'text/csv;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function renderQuality(data) {
    const q = data.quality, wells = data.wells;
    const map = q.missing_map || [];
    const columns = map[0]?.length || 1;
    const tiles = map.flatMap(row => row.map(ratio => `<i title="空值 ${number(ratio * 100, 1)}%" style="background:rgba(210,105,72,${0.08 + ratio * 0.85})"></i>`)).join('');
    const status = {ok:'有效', missing:'节点空值', outside:'范围外', no_coordinates:'无坐标'};
    const rows = wells.rows.map(row => `<tr><td>${esc(row.well_name)}</td><td>${esc(status[row.status] || row.status)}</td><td>${number(row.value, 4)}</td><td>${number(row.distance, 2)}</td></tr>`).join('');
    $('#surface-qc-results').classList.remove('empty-state');
    $('#surface-qc-results').innerHTML = `
      <div class="surface-qc-metrics">
        ${metric('有效网格', number(q.valid_cells, 0))}${metric('空值比例', number(q.missing_percent), '%')}${metric('IQR 异常节点', number(q.iqr_outliers, 0))}
        ${metric('最小值', number(q.minimum, 4))}${metric('中位数', number(q.median, 4))}${metric('最大值', number(q.maximum, 4))}
      </div>
      <p class="surface-qc-note">${esc(q.name)} · ${q.rows}×${q.columns} · P05–P95：${number(q.p05, 4)}–${number(q.p95, 4)} · 均值 ${number(q.mean, 4)} · 标准差 ${number(q.standard_deviation, 4)}</p>
      <div class="surface-qc-subhead">空值分布 <small>颜色越深，空值越多</small></div>
      <div class="surface-qc-missing" style="grid-template-columns:repeat(${columns},1fr)">${tiles}</div>
      <div class="surface-qc-subhead">当前井点筛选范围：${number(wells.total_wells, 0)} 口 <button class="ghost compact" type="button" id="surface-qc-export-wells">导出井位取值 CSV</button></div>
      <p class="surface-qc-note">有效 ${wells.counts.ok} · 节点空值 ${wells.counts.missing} · 范围外 ${wells.counts.outside} · 无坐标 ${wells.counts.no_coordinates}${wells.truncated ? `；表格与导出仅含前 ${wells.display_limit} 口井` : ''}。${esc(wells.method)}</p>
      <div class="surface-qc-table-wrap"><table class="surface-qc-table"><thead><tr><th>井名</th><th>取值状态</th><th>面值</th><th>最近节点距离</th></tr></thead><tbody>${rows || '<tr><td colspan="4">当前范围没有井点</td></tr>'}</tbody></table></div>
      <p class="surface-qc-note">${esc(q.method)}请先确认井位与属性面使用同一坐标系、同一长度单位。</p>`;
    $('#surface-qc-export-wells').onclick = () => downloadCsv('属性面井位取值.csv', [
      ['井名','井标识','X','Y','面值','最近节点距离','状态'],
      ...wells.rows.map(row => [row.well_name,row.well_key,row.x,row.y,row.value,row.distance,status[row.status] || row.status]),
    ]);
  }

  function drawPca(points) {
    const canvas = $('#surface-qc-scatter'); if (!canvas || !points?.length) return;
    const width = Math.max(canvas.clientWidth, 300), height = 220, dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
    const xs = points.map(row => row[0]), ys = points.map(row => row[1]);
    const xMin = Math.min(...xs), xMax = Math.max(...xs), yMin = Math.min(...ys), yMax = Math.max(...ys);
    const px = x => 35 + (x - xMin) / (xMax - xMin || 1) * (width - 52);
    const py = y => height - 25 - (y - yMin) / (yMax - yMin || 1) * (height - 45);
    ctx.strokeStyle = '#9ca9a3'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(35, 12); ctx.lineTo(35, height - 25); ctx.lineTo(width - 12, height - 25); ctx.stroke();
    ctx.fillStyle = '#249578'; for (const [x,y] of points) {ctx.beginPath(); ctx.arc(px(x), py(y), 2.5, 0, 2 * Math.PI); ctx.fill();}
    ctx.fillStyle = '#65766f'; ctx.font = '11px Segoe UI'; ctx.fillText('PC1', width - 42, height - 7); ctx.fillText('PC2', 5, 18);
  }

  function renderComparison(data) {
    const names = data.surfaces.map(row => row.name);
    const table = `<div class="surface-qc-table-wrap"><table class="surface-qc-table"><thead><tr><th>属性</th>${names.map(name => `<th title="${esc(name)}">${esc(name)}</th>`).join('')}</tr></thead><tbody>${data.correlation.map((row, index) => `<tr><th>${esc(names[index])}</th>${row.map(value => `<td>${number(value, 3)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    const strongPairs = data.strong_pairs?.length ? data.strong_pairs.map(row => `${esc(row.first)} ↔ ${esc(row.second)}（${number(row.correlation, 3)}）`).join('；') : '没有发现 |r| ≥ 0.9 的属性对';
    const pca = data.pca ? `<div class="surface-qc-subhead">PCA 二维概览</div><p class="surface-qc-note">PC1 ${number(data.pca.explained_variance_percent[0])}% · PC2 ${number(data.pca.explained_variance_percent[1])}%；仅显示部分共同有效点。</p><canvas id="surface-qc-scatter" class="surface-qc-scatter"></canvas>` : '<p class="surface-qc-note">有效的非常数属性不足两项，无法显示 PCA。</p>';
    $('#surface-compare-results').classList.remove('empty-state');
    $('#surface-compare-results').innerHTML = `<div class="surface-qc-metrics">${metric('参考网格', number(data.reference_cells,0))}${metric('抽样节点', number(data.sampled_cells,0))}${metric('共同有效', number(data.common_valid_cells,0))}</div><p class="surface-qc-note">共同有效比例 ${number(data.common_valid_percent)}%。${esc(data.method)}</p><div class="surface-qc-subhead">高度相关属性</div><p class="surface-qc-note">${strongPairs}</p><div class="surface-qc-subhead">Pearson 相关系数 <button class="ghost compact" type="button" id="surface-compare-export">导出矩阵 CSV</button></div>${table}${pca}<p class="surface-qc-note">高度相关表示属性信息相似，不代表地质意义相同；请先核对坐标系和属性单位。</p>`;
    $('#surface-compare-export').onclick = () => downloadCsv('属性相关矩阵.csv', [['属性',...names], ...data.correlation.map((row,index) => [names[index],...row])]);
    drawPca(data.pca?.points);
  }

  async function runQuality() {
    const surfaceId = $('#surface-qc-surface').value;
    if (!surfaceId) {toast('请先选择属性面', true); return;}
    const button = $('#surface-qc-run'); button.disabled = true; button.textContent = '检查中…';
    try {const result = await post('/api/reserves/surface-quality', {surface_id:surfaceId}); state.quality = result; renderQuality(result);}
    catch (error) {$('#surface-qc-results').textContent = error.message; toast(error.message, true);}
    finally {button.disabled = false; button.textContent = '开始检查';}
  }

  async function runComparison() {
    const ids = [...document.querySelectorAll('#surface-compare-list input:checked')].map(input => input.value);
    if (ids.length < 2 || ids.length > 8) {toast('请选择 2–8 张属性面', true); return;}
    const button = $('#surface-compare-run'); button.disabled = true; button.textContent = '比较中…';
    try {const result = await post('/api/reserves/surface-compare', {surface_ids:ids}); state.comparison = result; renderComparison(result);}
    catch (error) {$('#surface-compare-results').textContent = error.message; toast(error.message, true);}
    finally {button.disabled = false; button.textContent = '比较属性';}
  }

  window.loadSurfaceQc = (surfaces, reset = false) => {
    if (!state.bound) {$('#surface-qc-run').onclick = runQuality; $('#surface-compare-run').onclick = runComparison; state.bound = true;}
    const selected = reset ? '' : $('#surface-qc-surface').value;
    const checked = new Set(reset ? [] : [...document.querySelectorAll('#surface-compare-list input:checked')].map(input => input.value));
    state.quality = null; state.comparison = null;
    $('#surface-qc-results').className = 'surface-qc-results empty-state';
    $('#surface-qc-results').textContent = '选择一张属性面后开始检查';
    $('#surface-compare-results').className = 'surface-qc-results empty-state';
    $('#surface-compare-results').textContent = '选择属性面后查看共同有效点、相关性和二维概览';
    $('#surface-qc-surface').innerHTML = '<option value="">选择属性面</option>' + surfaces.map(row => `<option value="${esc(row.id)}">${esc(row.name)} · ${esc(row.source)}</option>`).join('');
    if (surfaces.some(row => row.id === selected)) $('#surface-qc-surface').value = selected;
    $('#surface-compare-list').innerHTML = surfaces.map(row => `<label title="${esc(row.name)}"><input type="checkbox" value="${esc(row.id)}" ${checked.has(row.id) ? 'checked' : ''}><span>${esc(row.name)}</span></label>`).join('') || '<p class="surface-qc-note">当前工区没有可分析的规则属性面</p>';
  };
})();
