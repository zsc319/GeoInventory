pageInfo.project=['项目驾驶舱','资料完备程度、空间匹配与解释精度的一站式体检'];
state.project=null;

const cockpitIcons={well_heads:'WH',well_paths:'DEV',well_logs:'LAS',checkshots:'CS',well_tops:'TOP',core:'RC',interpretations:'IP',seismic_3d:'3D',seismic_2d:'2D',horizons:'HZ',faults:'FT',polygons:'PG',production:'PR'};
const projectFmt=(value,digits=0)=>value===null||value===undefined?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits});
const pct=value=>`${projectFmt(value,1)}%`;
const fileSize=bytes=>bytes>=1073741824?`${(bytes/1073741824).toFixed(2)} GB`:bytes>=1048576?`${(bytes/1048576).toFixed(1)} MB`:`${projectFmt(bytes/1024,1)} KB`;

async function loadProject(){
  const snapshot=await api('/api/project');
  if(snapshot.empty){renderEmptyProject();return}
  state.project=snapshot;
  renderProject(snapshot);
  void refreshProjectCurveCoverage(true);
}

function renderEmptyProject(){
  document.querySelector('#cockpit-metrics').innerHTML='<div class="empty-state" style="grid-column:1/-1">还没有项目快照，请点击右上角“扫描目录”</div>';
  document.querySelector('#project-tree').innerHTML='<div class="tree-empty">等待项目快照</div>';
}

function renderProject(data){
  const project=data.project,quality=data.quality,coverage=data.well_coverage,seismic=data.seismic||{},three=seismic.three_d||{},faults=data.faults||{};
  document.querySelector('#sidebar-project-name').textContent=state.workspace?.project_title||project.name;
  document.querySelector('#sidebar-project-size').textContent=`${project.total_files.toLocaleString()} 文件 · ${project.total_gb} GB`;
  document.querySelector('#tree-total').textContent=project.total_files.toLocaleString();
  document.querySelector('#scan-time').textContent=`快照 ${String(project.scanned_at||'').replace('T',' ').slice(0,16)}`;
  document.querySelector('#project-root').value=project.root;
  renderProjectTree(data.categories||[]);
  const inlineCount=three.inline_min==null||three.inline_max==null?null:Math.abs(three.inline_max-three.inline_min)+1;
  const crosslineCount=three.crossline_min==null||three.crossline_max==null?null:Math.abs(three.crossline_max-three.crossline_min)+1;
  const gridSize=inlineCount&&crosslineCount?`${projectFmt(inlineCount)} × ${projectFmt(crosslineCount)}`:'—';
  const metrics=[
    ['资料完备度',quality.score,'/ 100',quality.grade],
    ['井头清单',data.wellheads?.count||0,'口井',`${coverage.estimated_unique_wells||0} 口文件名估算`],
    ['3D 道网格',gridSize,'Inline × Crossline',three.grid_transform?`${three.grid_transform.inline_spacing} × ${three.grid_transform.crossline_spacing} m 道距`:'道距待识别'],
    ['项目数据',project.total_gb,'GB',`${project.representative_count} 个目录代表样本`]
  ];
  document.querySelector('#cockpit-metrics').innerHTML=metrics.map((row,index)=>`<div class="cockpit-metric"><span>${row[0]}</span><strong>${typeof row[1]==='string'?esc(row[1]):index===3?projectFmt(row[1],2):projectFmt(row[1])}</strong><small>${row[2]} · ${esc(row[3])}</small><i class="trend">${index===0?'◈':index===2?'⌗':'↗'}</i></div>`).join('');
  renderCoverageSummary(data);
  renderSampleCurves(data.curve_type_coverage||data.representative_analysis||{});
  renderQuality(data);
  renderCategoryTable(data.categories||[]);
  renderFindings(data);
  requestAnimationFrame(()=>{drawProjectMap();drawProject3d()});
}

function renderProjectTree(categories){
  document.querySelector('#project-tree').innerHTML=categories.map(row=>`<div class="tree-row ${row.files?'active':''}"><i class="tree-icon">${cockpitIcons[row.key]||'DT'}</i><span><b>${esc(row.label)}</b><span>${fileSize(row.bytes)}</span></span><em>${projectFmt(row.files)}</em></div>`).join('');
}

function renderCoverageSummary(data){
  const three=data.seismic?.three_d||{},two=data.seismic?.two_d||{},surface=data.surface||{},faults=data.faults||{},sm=data.matches?.surface_to_3d||{},fm=data.matches?.fault_to_3d||{},hm=data.matches?.horizon_2d_to_seismic||{};
  const cards=[
    ['3D 道网格',sm.trace_grid||'—',three.grid_transform?`${three.grid_transform.inline_spacing}m × ${three.grid_transform.crossline_spacing}m，残差 ${three.grid_transform.rms_residual}m`:'道网格未建立',three.grid_transform?'已标定':'待核实',false],
    ['层面网格',sm.surface_grid||'—',`${surface.x_increment||'—'}m 网格 · 有效值 ${pct(surface.valid_percentage||0)}`,sm.resampled_finer_than_seismic?'超采样':'匹配',!!sm.resampled_finer_than_seismic],
    ['3D 空间覆盖',pct(sm.seismic_coverage_percentage||0),`层面覆盖地震范围；占层面自身 ${pct(sm.surface_overlap_percentage||0)}`,(sm.seismic_coverage_percentage||0)>=95?'完整':'不足',(sm.seismic_coverage_percentage||0)<95],
    ['断层模型',`${projectFmt(faults.model?.fault_count||0)} 条`,`${projectFmt(faults.model?.pillar_count||0)} pillars · 独立文件 ${faults.individual_file_count||0}`,fm.matched?'落网完成':'待匹配',!fm.matched],
    ['2D 层位',`${projectFmt(data.horizon_2d?.line_count||0)} 条线`,`${projectFmt(data.horizon_2d?.point_count||0)} 解释点 · 代表地震 ${projectFmt(two.trace_count||0)} 道`,hm.matched?'已对比':'待匹配',hm.matched&&hm.on_trace_percentage<80],
    ['代表性抽样',`${projectFmt(data.project.representative_count)} 目录`,`${projectFmt(data.representative_analysis?.las_sample_count||0)} LAS + ${projectFmt(data.representative_analysis?.dev_sample_count||0)} DEV`,data.representative_analysis?.errors?.length?'有异常':'无解析错误',!!data.representative_analysis?.errors?.length]
  ];
  document.querySelector('#coverage-summary').innerHTML=cards.map(row=>`<div class="coverage-card"><div class="card-top"><h3>${row[0]}</h3><span class="status-chip ${row[4]?'warn':''}">${row[3]}</span></div><strong>${row[1]}</strong><p>${row[2]}</p></div>`).join('');
}

function renderSampleCurves(analysis){
  const typeRows=Array.isArray(analysis.types),rows=(typeRows?analysis.types:(analysis.curve_sample_coverage||[]).filter(row=>!(/^(?:ONE[-_ ]?WAY[-_ ]?TIME|OWT|TWT)(?:\d|$)/i.test(row.mnemonic)))).slice(0,12),count=row=>typeRows?Number(row.well_count||0):Number(row.sample_files||0),max=Math.max(1,...rows.map(count));
  const note=document.querySelector('#sample-curves-note'),meta=document.querySelector('#sample-curves-meta');
  if(note)note.textContent=typeRows?`${analysis.profile_scope||'LAS 画像'} · ${analysis.filter?.active?'已应用井点筛选':'当前全部井'}`:'代表 LAS 样本 · 正在读取类型联动';
  if(meta)meta.textContent=typeRows?`分母 ${projectFmt(analysis.well_denominator||0)} 口有曲线井 · ${analysis.profile_source==='all_las_headers'?'全量 LAS 头段':'代表 LAS 样本'} · ${analysis.filter?.active?'已应用井点筛选':'当前全部井'}`:'曲线类型与测井曲线页面联动';
  document.querySelector('#sample-curve-bars').innerHTML=rows.map(row=>{const label=typeRows?row.type_name:row.mnemonic,detail=typeRows?`${(row.mnemonics||[]).join(' · ')||'尚未归属 mnemonic'} · ${row.confirmed_mnemonic_count||0} 条人工确认`:`${row.mnemonic} · ${row.sample_files} 个代表样本`;return `<div class="sample-curve" title="${esc(detail)}"><span>${esc(label)}</span><div class="track"><div class="fill" style="width:${count(row)/max*100}%"></div></div><b>${projectFmt(count(row))}${typeRows?' 井':''}</b></div>`}).join('')||`<div class="empty-state">${typeRows?'当前范围内没有已识别的常规测井曲线类型':'没有可展示的代表曲线'}</div>`;
}

async function refreshProjectCurveCoverage(quiet=false){
  const button=document.querySelector('#project-curve-coverage-refresh');
  if(!button)return;
  const originalText='↻ 刷新类型联动';button.disabled=true;button.textContent='正在汇总…';
  try{const data=await api('/api/project/curve-type-coverage');if(state.project){state.project.curve_type_coverage=data;renderSampleCurves(data)}if(!quiet)toast(`已按当前曲线类型刷新：${projectFmt(data.types?.length||0)} 类`)}catch(error){if(!quiet)toast(error.message,true);const note=document.querySelector('#sample-curves-note');if(note)note.textContent='类型联动读取失败，可稍后重试'}finally{button.disabled=false;button.textContent=originalText}
}

function renderQuality(data){
  const quality=data.quality,coverage=data.well_coverage,matches=data.matches||{},score=quality.score;
  const ring=document.querySelector('#quality-ring');ring.style.setProperty('--score',score);ring.querySelector('strong').textContent=score;
  document.querySelector('#quality-grade').textContent=quality.grade;
  document.querySelector('#estimated-wells').textContent=`估算 ${projectFmt(coverage.estimated_unique_wells)} 口`;
  const rows=[['井头 → LAS',coverage.head_with_las_percentage],['井头 → DEV',coverage.head_with_dev_percentage],['LAS → DEV',coverage.las_with_dev_percentage],['3D → 层面',matches.surface_to_3d?.seismic_coverage_percentage||0]];
  document.querySelector('#well-coverage-matrix').innerHTML=rows.map(row=>`<div class="coverage-item ${row[1]<80?'warn':''}"><span>${row[0]}</span><div class="track"><div class="fill" style="width:${Math.min(100,row[1])}%"></div></div><b>${pct(row[1])}</b></div>`).join('');
  const alerts=[...(quality.alerts||[])];
  const hmatch=matches.horizon_2d_to_seismic;
  if(hmatch?.matched&&hmatch.on_trace_percentage<80)alerts.push({level:'critical',title:'2D 层位与代表地震线存在系统偏移',detail:`${hmatch.matched_line||'代表线'} 中位偏移 ${projectFmt(hmatch.median_snap_distance,1)}m，需核对处理版本与基准。`});
  document.querySelector('#alert-count').textContent=`${alerts.length} 项`;
  document.querySelector('#quality-alerts').innerHTML=alerts.map(row=>`<div class="quality-alert ${row.level}"><i></i><div><b>${esc(row.title)}</b><span>${esc(row.detail)}</span></div></div>`).join('')||'<div class="empty-state">未发现明显问题</div>';
  const three=data.seismic?.three_d||{};
  document.querySelector('#seismic-facts').innerHTML=`<div class="fact"><b>${projectFmt(three.inline_min)}–${projectFmt(three.inline_max)}</b><span>INLINE</span></div><div class="fact"><b>${projectFmt(three.crossline_min)}–${projectFmt(three.crossline_max)}</b><span>CROSSLINE</span></div><div class="fact"><b>${projectFmt(three.sample_interval_us)} μs</b><span>采样间隔</span></div><div class="fact"><b>${esc(three.byte_layout||'—')}</b><span>道头布局</span></div>`;
}

function renderCategoryTable(categories){
  document.querySelector('#project-category-table').innerHTML=categories.map(row=>`<tr><td><strong>${esc(row.label)}</strong></td><td>${projectFmt(row.files)}</td><td>${fileSize(row.bytes)}</td><td>每文件夹 1 个代表样本</td><td><span class="status ${row.files?'ready':'failed'}">${row.files?'已发现':'缺失'}</span></td></tr>`).join('');
}

function renderFindings(data){
  const sm=data.matches?.surface_to_3d||{},fm=data.matches?.fault_to_3d||{},hm=data.matches?.horizon_2d_to_seismic||{};
  const findings=[
    ['坐标基准',data.surface?.crs?.epsg||'EPSG:32614'],
    ['层面解释精度',sm.resampled_finer_than_seismic?'10m 网格为 15m 地震的重采样':'与地震道距相当'],
    ['断层落道误差',fm.median_snap_distance!=null?`中位 ${projectFmt(fm.median_snap_distance,2)}m · P95 ${projectFmt(fm.p95_snap_distance,2)}m`:'待匹配'],
    ['2D 版本一致性',hm.median_snap_distance!=null?`代表线偏移 ${projectFmt(hm.median_snap_distance,1)}m，需复核`:'待匹配']
  ];
  document.querySelector('#finding-strip').innerHTML=findings.map((row,index)=>`<div class="finding ${index===3&&hm.median_snap_distance>50?'warn':''}"><span>${row[0]}</span><strong>${esc(row[1])}</strong></div>`).join('');
}

function drawProjectMap(){
  const canvas=document.querySelector('#project-map-canvas'),data=state.project?.visualization;if(!canvas||!data)return;
  const rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;const c=canvas.getContext('2d');c.scale(ratio,ratio);c.clearRect(0,0,rect.width,rect.height);
  const wells=(data.wells||[]).map(row=>[+row[0],+row[1]]),foot=data.seismic_3d_footprint||[],lines=(data.seismic_2d_points||[]).map(row=>[+row[0],+row[1]]),horizon=(data.horizon_2d_points||[]).map(row=>[+row[0],+row[1]]),fault=(data.fault_points||[]).map(row=>[+row[0],+row[1]]),sb=data.surface_bounds;
  const surface=sb?[[sb.x_min,sb.y_min],[sb.x_max,sb.y_min],[sb.x_max,sb.y_max],[sb.x_min,sb.y_max]]:[];const all=[...wells,...foot,...lines,...surface];if(!all.length)return;
  const xs=all.map(p=>p[0]),ys=all.map(p=>p[1]),xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys),pad=48,scale=Math.min((rect.width-pad*2)/(xmax-xmin||1),(rect.height-pad*2)/(ymax-ymin||1)),tx=x=>pad+(x-xmin)*scale+(rect.width-pad*2-(xmax-xmin)*scale)/2,ty=y=>rect.height-pad-(y-ymin)*scale-(rect.height-pad*2-(ymax-ymin)*scale)/2;
  c.font='8px Consolas';c.fillStyle='#4c685e';c.strokeStyle='rgba(53,91,77,.35)';for(let i=0;i<=4;i++){const x=pad+(rect.width-pad*2)*i/4,y=pad+(rect.height-pad*2)*i/4;c.beginPath();c.moveTo(x,pad);c.lineTo(x,rect.height-pad);c.stroke();c.beginPath();c.moveTo(pad,y);c.lineTo(rect.width-pad,y);c.stroke();c.fillText(projectFmt(xmin+(xmax-xmin)*i/4),x-15,rect.height-19);c.fillText(projectFmt(ymax-(ymax-ymin)*i/4),6,y+3)}
  drawPolygon(c,surface,tx,ty,'rgba(185,233,75,.025)','#8fb84a',[5,4]);drawPolygon(c,foot,tx,ty,'rgba(43,163,148,.16)','#2ba394');drawLine(c,lines,tx,ty,'rgba(70,173,205,.7)',1);drawLine(c,horizon,tx,ty,'rgba(205,98,200,.8)',1.2);drawLine(c,fault,tx,ty,'#e5b44b',1.4);
  c.fillStyle='#ee8554';for(const point of wells){c.beginPath();c.arc(tx(point[0]),ty(point[1]),1.3,0,Math.PI*2);c.fill()}
}

function drawPolygon(c,points,tx,ty,fill,stroke,dash=[]){if(points.length<2)return;c.beginPath();points.forEach((p,i)=>i?c.lineTo(tx(p[0]),ty(p[1])):c.moveTo(tx(p[0]),ty(p[1])));c.closePath();c.fillStyle=fill;c.fill();c.strokeStyle=stroke;c.lineWidth=1;c.setLineDash(dash);c.stroke();c.setLineDash([])}
function drawLine(c,points,tx,ty,color,width){if(points.length<2)return;c.beginPath();points.forEach((p,i)=>i?c.lineTo(tx(p[0]),ty(p[1])):c.moveTo(tx(p[0]),ty(p[1])));c.strokeStyle=color;c.lineWidth=width;c.stroke()}

function drawProject3d(){
  const canvas=document.querySelector('#project-3d-canvas'),v=state.project?.visualization;if(!canvas||!v)return;const rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;const c=canvas.getContext('2d');c.scale(ratio,ratio);c.clearRect(0,0,rect.width,rect.height);
  const foot=v.seismic_3d_footprint||[],fault=v.fault_points||[],wells=v.wells||[],sb=v.surface_bounds;if(!foot.length)return;const xs=foot.map(p=>p[0]),ys=foot.map(p=>p[1]),xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys),cx=rect.width*.48,base=rect.height*.68,scale=Math.min(rect.width/(xmax-xmin||1),rect.height/(ymax-ymin||1))*.34,project=(x,y,z=0)=>[cx+((x-xmin)-(y-ymin))*scale*.72,base+((x-xmin)+(y-ymin))*scale*.28-z];
  c.strokeStyle='rgba(58,118,99,.55)';c.lineWidth=1;for(let i=0;i<=14;i++){let t=i/14,a=project(xmin+(xmax-xmin)*t,ymin,0),b=project(xmin+(xmax-xmin)*t,ymax,0);c.beginPath();c.moveTo(...a);c.lineTo(...b);c.stroke();a=project(xmin,ymin+(ymax-ymin)*t,0);b=project(xmax,ymin+(ymax-ymin)*t,0);c.beginPath();c.moveTo(...a);c.lineTo(...b);c.stroke()}
  const plane=foot.map(p=>project(p[0],p[1],0));c.beginPath();plane.forEach((p,i)=>i?c.lineTo(...p):c.moveTo(...p));c.closePath();c.fillStyle='rgba(33,136,121,.18)';c.fill();c.strokeStyle='#278d7d';c.stroke();
  if(sb){const z=90,sx0=Math.max(sb.x_min,xmin),sx1=Math.min(sb.x_max,xmax),sy0=Math.max(sb.y_min,ymin),sy1=Math.min(sb.y_max,ymax),corners=[[sx0,sy0],[sx1,sy0],[sx1,sy1],[sx0,sy1]].map(p=>project(p[0],p[1],z));c.beginPath();corners.forEach((p,i)=>i?c.lineTo(...p):c.moveTo(...p));c.closePath();c.strokeStyle='rgba(185,233,75,.65)';c.setLineDash([5,4]);c.stroke();c.setLineDash([])}
  c.strokeStyle='#e2b84e';for(let i=1;i<fault.length;i++){const a=fault[i-1],b=fault[i],pa=project(a[0],a[1],Math.min(180,Math.max(10,(a[2]||0)*.22))),pb=project(b[0],b[1],Math.min(180,Math.max(10,(b[2]||0)*.22)));c.beginPath();c.moveTo(...pa);c.lineTo(...pb);c.stroke()}
  c.strokeStyle='rgba(238,133,84,.45)';for(const well of wells.slice(0,1000)){const top=project(well[0],well[1],130),bottom=project(well[0],well[1],-55);c.beginPath();c.moveTo(...top);c.lineTo(...bottom);c.stroke()}
}

function selectProjectView(view){document.querySelectorAll('[data-project-view]').forEach(button=>button.classList.toggle('active',button.dataset.projectView===view));document.querySelectorAll('.project-view').forEach(panel=>panel.classList.toggle('active',panel.id===`project-view-${view}`));if(view==='2d')requestAnimationFrame(drawProjectMap);if(view==='3d')requestAnimationFrame(drawProject3d)}
document.querySelectorAll('[data-project-view]').forEach(button=>button.onclick=()=>selectProjectView(button.dataset.projectView));
document.querySelector('#project-curve-coverage-refresh').onclick=()=>refreshProjectCurveCoverage();

const scanDialog=document.querySelector('#scan-dialog');document.querySelector('#open-scan-dialog').onclick=()=>scanDialog.showModal();document.querySelector('.scan-close').onclick=()=>scanDialog.close();scanDialog.onclick=e=>{if(e.target===scanDialog)scanDialog.close()};
document.querySelector('#project-scan-form').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,button=form.querySelector('button[type=submit]'),progress=document.querySelector('#scan-progress'),payload={root:form.root.value.trim(),scan_surface_values:form.scan_surface_values.checked};button.disabled=true;button.textContent='扫描中…';progress.classList.remove('hidden');try{const snapshot=await api('/api/project/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});state.project=snapshot;renderProject(snapshot);scanDialog.close();toast(`扫描完成：${snapshot.project.total_files} 个文件`)}catch(error){toast(error.message,true)}finally{button.disabled=false;button.textContent='开始代表性扫描';progress.classList.add('hidden')}};
window.addEventListener('resize',()=>{if(state.activePage==='project'){drawProjectMap();drawProject3d()}});
navigate('project');
