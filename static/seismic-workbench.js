(function(){
  let bound=false;
  const seismicState=()=>state.seismicInventory||(state.seismicInventory={data:null,selectedGroup:null,selectedPath:null});
  const size=value=>typeof fileSize==='function'?fileSize(value):`${(Number(value||0)/1073741824).toFixed(2)} GB`;
  const number=(value,digits=0)=>value===null||value===undefined?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits});

  function renderMetrics(data){
    const rows=[['地震体大类',data.group_count],['文件路径',data.file_count],['SEG-Y',data.sgy_count],['ZGY',data.zgy_count],['重复路径组',data.duplicate_group_count]];
    document.querySelector('#seismic-inventory-metrics').innerHTML=rows.map(row=>`<div><span>${row[0]}</span><b>${number(row[1])}</b></div>`).join('');
    document.querySelector('#seismic-inventory-method').textContent=`${number(data.file_count)} 个文件 · ${size(data.bytes)} · 快速头信息，不读取振幅样点`;
  }

  function filteredGroups(){
    const data=seismicState().data,q=document.querySelector('#seismic-volume-search').value.trim().toLowerCase(),format=document.querySelector('#seismic-format-filter').value,dimension=document.querySelector('#seismic-dimension-filter').value,domain=document.querySelector('#seismic-domain-filter').value,attribute=document.querySelector('#seismic-attribute-filter').value,duplicates=document.querySelector('#seismic-duplicate-filter').checked;
    return data.groups.filter(group=>{
      const haystack=`${group.name} ${group.attribute_types.join(' ')} ${group.paths.map(row=>`${row.relative_path} ${row.attribute_type}`).join(' ')}`.toLowerCase();
      return (!q||haystack.includes(q))&&(!format||group.formats.includes(format))&&(!dimension||group.dimensions.includes(dimension))&&(!domain||group.domains.includes(domain))&&(!attribute||group.attribute_types.includes(attribute))&&(!duplicates||group.path_count>1);
    });
  }

  function renderGroups(){
    const ss=seismicState(),groups=filteredGroups(),list=document.querySelector('#seismic-volume-list');
    document.querySelector('#seismic-filter-count').textContent=`${number(groups.length)} / ${number(ss.data.group_count)} 类`;
    list.innerHTML=groups.map(group=>`<button type="button" class="seismic-volume-item ${ss.selectedGroup?.key===group.key?'active':''}" data-seismic-group="${esc(group.key)}"><span class="seismic-volume-icon">${group.formats.includes('ZGY')?'ZG':'SG'}</span><span><b>${esc(group.name)}</b><small>${esc(group.attribute_types.join(' / '))} · ${esc(group.domains.join(' / '))}</small><em>${group.formats.map(value=>`<i>${esc(value)}</i>`).join('')}<i>${esc(group.dimensions.join('/'))}</i></em></span><strong>${group.path_count>1?`${group.path_count} 条路径`:'1 条路径'}</strong></button>`).join('')||'<div class="empty-state">没有符合当前筛选条件的地震体</div>';
    list.querySelectorAll('[data-seismic-group]').forEach(button=>button.onclick=()=>selectGroup(button.dataset.seismicGroup));
  }

  function selectGroup(key){
    const ss=seismicState(),group=ss.data.groups.find(row=>row.key===key);if(!group)return;
    ss.selectedGroup=group;
    if(!ss.selectedPath||!group.paths.some(row=>row.path===ss.selectedPath.path))ss.selectedPath=group.paths[0];
    renderGroups();renderDetail();
  }

  function statusPill(path){
    if(path.analysis_status==='已完整解析'||path.quick_status==='decoded')return '<span class="seismic-read-status ready">已解析</span>';
    if(path.quick_status==='invalid')return '<span class="seismic-read-status error">文件异常</span>';
    return `<span class="seismic-read-status">${path.format==='ZGY'?'待解码':'快速头'}</span>`;
  }

  function renderDetail(){
    const ss=seismicState(),group=ss.selectedGroup,path=ss.selectedPath,detail=document.querySelector('#seismic-detail');if(!group||!path)return;
    const warnings=[...(path.warnings||[]),...(path.warning?[path.warning]:[])].filter(Boolean);
    const traceGrid=path.trace_grid||(path.dimension==='2D'&&path.trace_count?`1 条测线 × ${number(path.trace_count)} 道`:((path.inline_min!==null&&path.inline_min!==undefined&&path.crossline_min!==null&&path.crossline_min!==undefined)?`${number(path.inline_min)}–${number(path.inline_max)} × ${number(path.crossline_min)}–${number(path.crossline_max)}`:'完整解析后获取'));
    const traceCount=path.trace_count===null||path.trace_count===undefined?'待解析':`${number(path.trace_count)}${path.trace_count_exact?'':'（估）'}`;
    const verticalUnit=path.domain==='时间域'?' ms':path.domain==='深度域'?'（深度单位）':'';
    const verticalRange=path.z_min!==null&&path.z_min!==undefined?`${number(path.z_min,3)} → ${number(path.z_max,3)}${verticalUnit}`:(path.vertical_extent||'起止值待确认');
    detail.innerHTML=`
      <div class="seismic-detail-head"><div><span class="eyebrow">SEISMIC VOLUME DETAIL</span><h2>${esc(group.name)}</h2><p>${esc(path.filename)} · ${size(path.bytes)}</p></div><div><span class="seismic-format-badge">${esc(path.format)}</span><span class="dimension">${esc(path.dimension)}</span></div></div>
      <div class="seismic-semantic-strip"><div><span>地震道内容</span><b>${esc(path.attribute_type)}</b><small>${esc(path.semantic_source)}，需业务复核</small></div><div><span>垂向数据域</span><b>${esc(path.domain)}</b><small>${path.domain==='待确认'?'文件头未提供可靠域信息':'由命名或元数据判别'}</small></div><div><span>样点编码</span><b>${esc(path.sample_encoding)}</b><small>${path.format==='SEG-Y'?`SEG-Y format code ${path.format_code??'—'}`:'ZGY 内部压缩格式'}</small></div></div>
      <div class="seismic-cube-grid"><div class="primary"><span>Trace 网格</span><b>${esc(traceGrid)}</b><small>${path.dimension==='2D'?'测线数 × 道数':'Inline 数 × Crossline 数'}</small></div><div><span>总地震道</span><b>${esc(traceCount)}</b><small>${path.dimension==='2D'?'2D 测线无规则 Inline / Crossline 网格':`Inline ${number(path.inline_min)}–${number(path.inline_max)}（${number(path.inline_count)}） · Crossline ${number(path.crossline_min)}–${number(path.crossline_max)}（${number(path.crossline_count)}）`}</small></div><div><span>每道样点</span><b>${path.sample_count_min===path.sample_count_max?number(path.sample_count_max):`${number(path.sample_count_min)}–${number(path.sample_count_max)}`}</b><small>最小–最大</small></div><div><span>采样间隔</span><b>${esc(path.sample_interval_label||'待确认')}</b><small>${path.domain==='时间域'?'时间域换算':'单位随数据域确认'}</small></div><div><span>垂向范围</span><b>${esc(verticalRange)}</b><small>${path.analysis_status==='已完整解析'?'由道头起始时间和样点数计算':'快速读取通常只得长度'}</small></div><div><span>平面坐标范围</span><b>${path.x_min===null||path.x_min===undefined?'完整解析后获取':`X ${number(path.x_min,1)}–${number(path.x_max,1)}`}</b><small>${path.y_min===null||path.y_min===undefined?'—':`Y ${number(path.y_min,1)}–${number(path.y_max,1)}`}</small></div></div>
      <section class="seismic-path-section"><header><div><h3>同一大类的文件路径</h3><p>名称归为一类，读取状态与文件路径分别记录。</p></div><span>${group.path_count} 条</span></header><div class="seismic-path-list">${group.paths.map(item=>`<button type="button" class="${item.path===path.path?'active':''}" data-seismic-path="${esc(item.path)}"><i>${item.extension.replace('.','').toUpperCase()}</i><span><b>${esc(item.relative_path)}</b><small>${size(item.bytes)} · ${new Date(item.modified_at*1000).toLocaleDateString('zh-CN')}</small></span>${statusPill(item)}</button>`).join('')}</div></section>
      <div class="seismic-detail-actions"><div><span>当前完整路径</span><b title="${esc(path.path)}">${esc(path.path)}</b></div><button type="button" class="primary" id="analyze-seismic-path" ${path.format==='ZGY'&&!path.zgy_decoder?'title="当前仍可尝试读取；没有解码组件时会保持待解析"':''}>${path.analysis_status==='已完整解析'?'重新完整解析':'完整解析道头'}</button></div>
      ${warnings.length?`<div class="seismic-warning-list">${warnings.map(value=>`<p>! ${esc(value)}</p>`).join('')}</div>`:''}
      <details class="seismic-header-preview" ${path.text_header_preview?'':'hidden'}><summary>查看 SEG-Y 文本头摘要</summary><pre>${esc(path.text_header_preview||'')}</pre></details>`;
    detail.querySelectorAll('[data-seismic-path]').forEach(button=>button.onclick=()=>{ss.selectedPath=group.paths.find(row=>row.path===button.dataset.seismicPath);renderDetail()});
    detail.querySelector('#analyze-seismic-path').onclick=analyzeSelected;
    window.seismicSectionSelectionChanged?.(path);
  }

  async function analyzeSelected(){
    const ss=seismicState(),button=document.querySelector('#analyze-seismic-path'),path=ss.selectedPath;if(!path)return;
    button.disabled=true;button.textContent=path.format==='SEG-Y'?'正在遍历 trace header…':'正在读取 ZGY 元数据…';
    try{
      const result=await api('/api/seismic-inventory/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:path.path})});
      Object.assign(path,result);renderDetail();renderGroups();toast(path.format==='SEG-Y'?`已解析 ${number(path.trace_count)} 道`:(path.zgy_decoder?'ZGY 元数据读取完成':'ZGY 已索引，内部元数据仍待解码'));
    }catch(error){toast(error.message,true);button.disabled=false;button.textContent='重试完整解析'}
  }

  function bind(){if(bound)return;bound=true;
    document.querySelector('#refresh-seismic-inventory').onclick=()=>loadSeismicWorkbench(true);
    document.querySelector('#seismic-volume-search').oninput=renderGroups;
    ['seismic-format-filter','seismic-dimension-filter','seismic-domain-filter','seismic-attribute-filter','seismic-duplicate-filter'].forEach(id=>document.querySelector(`#${id}`).onchange=renderGroups);
  }

  window.loadSeismicWorkbench=async function(force=false){
    bind();const ss=seismicState();if(!ss.data||force){
      if(force)window.seismicSectionSelectionChanged?.(null);
      const button=document.querySelector('#refresh-seismic-inventory');button.disabled=true;button.textContent='正在检索…';
      document.querySelector('#seismic-volume-list').innerHTML='<div class="empty-state">正在读取地震文件头；不会读取振幅样点…</div>';
      try{
        ss.data=await api('/api/seismic-inventory');state.seismic=ss.data.groups;ss.selectedGroup=null;ss.selectedPath=null;const attribute=document.querySelector('#seismic-attribute-filter'),current=attribute.value,types=[...new Set(ss.data.groups.flatMap(group=>group.attribute_types))].sort((a,b)=>a.localeCompare(b,'zh-CN'));attribute.innerHTML='<option value="">全部属性</option>'+types.map(value=>`<option value="${esc(value)}">${esc(value)}</option>`).join('');if(types.includes(current))attribute.value=current;renderMetrics(ss.data);renderGroups();
        const first=filteredGroups()[0];if(first)selectGroup(first.key);else {document.querySelector('#seismic-detail').innerHTML='<div class="seismic-detail-empty"><i>≋</i><b>没有可展示的地震文件</b><span>当前项目目录尚未发现 SEG-Y 或 ZGY。</span></div>';window.seismicSectionSelectionChanged?.(null)}
      }finally{button.disabled=false;button.textContent='↻ 重新检索目录'}
    }else{renderMetrics(ss.data);renderGroups();if(ss.selectedGroup)renderDetail()}
  };
})();
