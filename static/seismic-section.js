(() => {
  const section = {path:null, result:null, requestId:0, bound:false};
  const $ = selector => document.querySelector(selector);
  const number = (value, digits=1) => Number(value).toLocaleString('zh-CN', {maximumFractionDigits:digits});

  function draw() {
    const data = section.result;
    if (!data) return;
    const canvas = $('#seismic-section-canvas');
    const width = Math.max(320, canvas.clientWidth), height = Math.max(240, canvas.clientHeight);
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, width, height);
    const amplitudes = data.amplitudes, nx = amplitudes.length, ny = data.display_sample_count;
    const image = document.createElement('canvas'); image.width = nx; image.height = ny;
    const imageContext = image.getContext('2d');
    const pixels = imageContext.createImageData(nx, ny);
    const gain = Number($('#seismic-section-gain').value) || 1;
    const palette = $('#seismic-section-palette').value;
    for (let x = 0; x < nx; x++) {
      for (let y = 0; y < ny; y++) {
        const index = (y * nx + x) * 4, value = amplitudes[x][y];
        if (value === -128) {pixels.data[index]=210; pixels.data[index+1]=210; pixels.data[index+2]=210;}
        else {
          const scaled = Math.max(-1, Math.min(1, value * gain / 127));
          if (palette === 'grayscale') {
            const shade = Math.round(242 - (scaled + 1) * 105);
            pixels.data[index]=shade; pixels.data[index+1]=shade; pixels.data[index+2]=shade;
          } else if (scaled >= 0) {
            pixels.data[index]=245; pixels.data[index+1]=Math.round(245 - 190 * scaled); pixels.data[index+2]=Math.round(245 - 190 * scaled);
          } else {
            pixels.data[index]=Math.round(245 + 190 * scaled); pixels.data[index+1]=Math.round(245 + 190 * scaled); pixels.data[index+2]=245;
          }
        }
        pixels.data[index+3]=255;
      }
    }
    imageContext.putImageData(pixels, 0, 0);
    const plot = {left:64, top:18, right:width-18, bottom:height-48};
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(image, plot.left, plot.top, plot.right-plot.left, plot.bottom-plot.top);
    ctx.strokeStyle = '#50615a'; ctx.lineWidth = 1; ctx.strokeRect(plot.left, plot.top, plot.right-plot.left, plot.bottom-plot.top);
    ctx.fillStyle = '#3b4a44'; ctx.font = '11px Segoe UI';
    const verticalEnd = data.vertical_start + data.vertical_step * (ny - 1);
    ctx.fillText(`${number(data.vertical_start)} ${data.vertical_unit}`, 5, plot.top + 10);
    ctx.fillText(`${number((data.vertical_start + verticalEnd) / 2)} ${data.vertical_unit}`, 5, (plot.top + plot.bottom) / 2);
    ctx.fillText(`${number(verticalEnd)} ${data.vertical_unit}`, 5, plot.bottom);
    const labels = data.trace_labels;
    for (const fraction of [0, 0.5, 1]) {
      const index = Math.round(fraction * (labels.length - 1));
      const x = plot.left + fraction * (plot.right - plot.left);
      const text = String(labels[index]);
      const offset = fraction === 0 ? 0 : fraction === 1 ? ctx.measureText(text).width : ctx.measureText(text).width / 2;
      ctx.fillText(text, x - offset, plot.bottom + 17);
    }
    const axisName = data.axis === 'inline' ? 'Crossline' : data.axis === 'crossline' ? 'Inline' : '地震道序号';
    ctx.fillText(axisName, Math.max(plot.left, (plot.left+plot.right)/2-30), height - 7);
  }

  function showResult(data) {
    section.result = data;
    $('#seismic-section-empty').classList.add('hidden');
    $('#seismic-section-status').textContent = `${data.dimension} · ${data.axis === 'trace' ? '全测线' : `${data.axis === 'inline' ? 'Inline' : 'Crossline'} ${data.selected_line}`}`;
    $('#seismic-section-method').textContent = `${number(data.display_trace_count,0)} / ${number(data.source_trace_count,0)} 道 · ${number(data.display_sample_count,0)} / ${number(data.source_sample_count,0)} 样点 · ${data.domain} · ${data.method}`;
    const axis = $('#seismic-section-axis'), value = $('#seismic-section-value');
    if (data.dimension === '2D') {
      axis.innerHTML = '<option value="trace">2D 全测线</option>'; axis.disabled = true; value.value = ''; value.disabled = true;
    } else {
      axis.innerHTML = '<option value="inline">Inline</option><option value="crossline">Crossline</option>';
      axis.disabled = false; axis.value = data.axis; value.disabled = false; value.value = data.selected_line;
      const range = data.line_ranges[data.axis]; value.min = range.minimum; value.max = range.maximum;
      value.title = `可选 ${range.minimum}–${range.maximum}，若编号缺失将选最近的现有剖面`;
    }
    draw();
  }

  async function load() {
    if (!section.path || section.path.format !== 'SEG-Y') return;
    const button = $('#seismic-section-load'), requestId = ++section.requestId;
    button.disabled = true; button.textContent = '读取中…';
    $('#seismic-section-status').textContent = '正在按需读取道头与振幅…';
    try {
      const data = await api('/api/seismic-inventory/section', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        path:section.path.path, axis:$('#seismic-section-axis').value,
        value:$('#seismic-section-value').value || null,
        max_traces:Number($('#seismic-section-traces').value), max_samples:Number($('#seismic-section-samples').value),
      })});
      if (requestId === section.requestId) showResult(data);
    } catch (error) {
      if (requestId === section.requestId) {$('#seismic-section-status').textContent = error.message; toast(error.message, true);}
    } finally {
      if (requestId === section.requestId) {button.disabled = false; button.textContent = '加载剖面';}
    }
  }

  function bind() {
    if (section.bound) return;
    section.bound = true;
    $('#seismic-section-load').onclick = load;
    $('#seismic-section-axis').onchange = () => {$('#seismic-section-value').value = '';};
    $('#seismic-section-gain').oninput = event => {$('#seismic-section-gain-value').textContent = `${Number(event.target.value).toFixed(1)}×`; draw();};
    $('#seismic-section-palette').onchange = draw;
    window.addEventListener('resize', () => {if ($('#page-seismic').classList.contains('active')) draw();});
  }

  window.seismicSectionSelectionChanged = path => {
    bind();
    if (section.path?.path === path?.path) return;
    section.requestId++;
    section.path = path; section.result = null;
    const supported = path?.format === 'SEG-Y';
    $('#seismic-section-load').disabled = !supported;
    $('#seismic-section-load').textContent = '加载剖面';
    $('#seismic-section-file').textContent = path ? `当前文件：${path.relative_path}` : '选择 SEG-Y 文件后，按需读取振幅并显示剖面。';
    $('#seismic-section-status').textContent = supported ? '可加载剖面' : path ? 'ZGY 剖面预览暂未开放' : '尚未选择文件';
    $('#seismic-section-empty').classList.remove('hidden');
    $('#seismic-section-empty').textContent = supported ? '点击“加载剖面”读取振幅样点' : path ? '当前仅支持 SEG-Y 剖面预览' : '选择 SEG-Y 文件后点击“加载剖面”';
    const canvas = $('#seismic-section-canvas'), context = canvas.getContext('2d'); context.clearRect(0,0,canvas.width,canvas.height);
    $('#seismic-section-axis').innerHTML = '<option value="inline">Inline</option><option value="crossline">Crossline</option>';
    $('#seismic-section-axis').disabled = false;
    $('#seismic-section-value').disabled = false; $('#seismic-section-value').value = '';
    $('#seismic-section-method').textContent = '按需抽样预览，不加载整块地震体。';
  };
})();
