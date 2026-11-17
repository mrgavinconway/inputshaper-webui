'use strict';
// Archive exploration is independent of the saved single-measurement workspace.
(() => {
  const h = id => document.getElementById(id);
  const esc = escapeHTML;
  const palette = ['#24634c', '#df6957', '#557cc9', '#9251bd', '#bd8b27', '#159caa'];
  const state = {active:false, loaded:false, busy:false, records:[], counts:{}, selected:new Set(), band:new Map(), bandSequence:0, renderSequence:0, uploadBusy:false, changeSequence:0, change:null};
  const key = r => `${r.run_id}:${r.measurement}`;
  const reference = r => ({run_id:r.run_id, measurement:r.measurement});
  const number = (n, digits=1) => Number.isFinite(n) ? n.toLocaleString(undefined, {maximumFractionDigits:digits}) : '—';
  const power = n => Number.isFinite(n) ? n.toLocaleString(undefined, {maximumSignificantDigits:4}) : '—';
  const stampValue = r => typeof r.recorded_at === 'number' ? r.recorded_at : String(r.recorded_at || r.created || '');
  const chronological = (a,b) => String(stampValue(a)).localeCompare(String(stampValue(b))) || key(a).localeCompare(key(b));
  function stamp(r) {
    if (typeof r.recorded_at === 'number') return new Date(r.recorded_at*1000).toLocaleString();
    // Filename dates have no timezone. Preserve the source's wall-clock time.
    return r.recorded_at ? String(r.recorded_at).replace('T',' ').replace(/\.0+$/, '') : 'Date unknown';
  }
  const shortLabel = r => `${stamp(r)} · ${r.excitation || '?'} · ${r.friendly_name || r.source_label || r.title || r.name || 'Recording'}`;
  const hasPSD = r => r.quality?.status !== 'invalid' && Array.isArray(r.frequency) && r.frequency.length > 1 && Array.isArray(r.raw_psd?.[h('historyChannel').value]) && r.raw_psd[h('historyChannel').value].length===r.frequency.length;

  h('importButton').insertAdjacentHTML('afterend', '<div class="historyNav"><button id="historyButton" class="quiet">▤ Recording history</button><button id="batchImportButton" class="textButton">＋ Import an archive / batch</button></div>');
  h('workspace').insertAdjacentHTML('afterend', `
    <section id="historyWorkspace" class="hidden" aria-label="Recording history">
      <div class="historyIntro card">
        <div><span class="eyebrow">THE LONGER VIEW</span><h2>Find what repeats. Check what changed.</h2><p>Follow a frequency feature across your recordings, then use a controlled test to investigate its cause.</p></div>
        <div class="actions"><button id="historyRefresh" class="quiet">Refresh</button><button id="historyUpload" class="primary">Import archive / batch</button></div>
        <div id="historyCounts" class="historyCounts" aria-live="polite"></div>
      </div>
      <p id="historyLoadStatus" class="hint" role="status"></p>
      <div class="card historyFilters">
        <label>Excitation direction<select id="historyExcitation"><option value="X">X test</option><option value="Y">Y test</option><option value="all">All directions</option><option value="unknown">Unknown direction</option></select></label>
        <label>Recorded channel<select id="historyChannel"><option value="XYZ">Combined X+Y+Z</option><option value="X">Sensor X</option><option value="Y">Sensor Y</option><option value="Z">Sensor Z</option></select></label>
        <label class="historyCohortFilter">Inferred setup group<select id="historyCohort"><option value="all">All groups</option></select></label>
        <label>Display scale<select id="historyScale"><option value="relative">Relative shape · each peak = 1</option><option value="absolute">Absolute ordinary PSD</option></select></label>
        <label>Display up to (Hz)<input id="historyMaxFrequency" type="number" min="20" max="1000" step="10" value="500"></label>
      </div>
      <div id="historyContextNotice" class="historyNotice" role="note"></div>
      <div class="card historyPlotCard">
        <div class="sectionHead"><div><span class="eyebrow">02 / ACROSS RECORDINGS</span><h2>Where does the pattern change?</h2></div><span id="historyMapCount" class="hint"></span></div>
        <p id="historyScaleNote" class="hint"></p>
        <div class="historyMapScroll"><div id="historyHeatmap" class="historyHeatmap"></div></div>
        <p class="hint">Oldest at the top. Click a row to add its native spectrum below. The map uses linear interpolation onto a shared grid for display only; no samples, metrics or peak positions are calculated from it. Blank cells have no supported spectrum.</p>
      </div>
      <div class="card historyPlotCard">
        <div class="sectionHead"><div><span class="eyebrow">03 / SELECTED RECORDINGS</span><h2>Inspect the native spectra</h2></div><div class="historyOverlayActions"><span id="historySelectionCount" class="hint"></span><button id="historySelectRecent" class="quiet">Latest 3</button><button id="historyClearSelection" class="quiet">Clear</button></div></div>
        <p class="hint">Select up to six recordings in the table. Lines use each recording’s original PSD frequency bins; the shaded area is the band measured below.</p>
        <div id="historyOverlay" class="historyOverlay"></div>
      </div>
      <div class="card historyPlotCard">
        <div class="sectionHead"><div><span class="eyebrow">04 / BAND EVIDENCE</span><h2>Put numbers beside the feature</h2></div><span id="historyBandStatus" class="hint" aria-live="polite"></span></div>
        <form id="historyBandForm" class="historyBandControls"><label>Band start (Hz)<input id="historyBandLow" type="number" min="0" max="1000" step="any" value="10" required></label><label>Band end (Hz)<input id="historyBandHigh" type="number" min="0" max="1000" step="any" value="200" required></label><button class="primary" id="historyMeasureBand">Measure visible recordings</button></form>
        <p class="hint">Power is the integral of ordinary PSD in the chosen band, in (mm/s²)²; RMS is in mm/s². X/Y/Z shares describe recorded channels, not identified mechanical causes. Absolute changes require verified acquisition conditions.</p>
        <div id="historyBandWarnings"></div>
        <div class="tableWrap historyTableWrap"><table class="historyTable"><thead><tr><th>Overlay</th><th>Recording / source date</th><th>Band power<br><span id="historyBandAxis">X+Y+Z</span></th><th>RMS<br>mm/s²</th><th>Channel share in band<br>X / Y / Z</th><th>Peaks in band<br>Hz</th><th>Timing / apparent gravity</th><th>Recorded context</th><th>Inspect</th></tr></thead><tbody id="historyRows"></tbody></table></div>
        <p id="historyEmpty" class="hint hidden">No recordings match these filters. Try another excitation direction or setup group, or import your archive.</p>
        <p class="hint">Dates inferred from filenames retain an unknown timezone. Open a measurement to inspect its waveform, spectrogram, quality checks and saved notes. Identical stored recordings are grouped; duplicates are not independent repeats.</p>
      </div>
      <details class="card historyProtocol"><summary>Turn an archive observation into a physical test</summary><p>When the sensor and umbilical change together, their separate effects cannot be isolated from that comparison. A peak’s frequency alone does not identify a belt, motor, fan or mount.</p><ol><li><strong>A — establish repeatability.</strong> Keep the sensor, mounting, axis mapping, umbilical routing, test position, excitation, fans and temperatures fixed. Record at least two X/Y pairs and note the band’s normal spread.</li><li><strong>B — change one thing.</strong> For example, change only the umbilical’s support or routing while retaining the same sensor and mounting. Record the same X/Y tests and the change made. If changing sensor hardware, document mounting and mapping and treat that as a separate experiment.</li><li><strong>A — restore the starting state.</strong> Repeat the original setup. A consistent change in B that returns towards the A range supports an association; it does not by itself establish the failed component.</li></ol><p class="hint">Choose a band around the feature, compare the same excitation direction, and inspect adjacent peaks, channel shares and timing quality. The combined X+Y+Z channel is less sensitive to a simple axis rotation, but mounting, sensor response, calibration and test conditions still matter.</p></details>
    </section>`);

  h('historyWorkspace').querySelector('.historyProtocol').insertAdjacentHTML('beforebegin', `
    <div class="card historyPlotCard" id="historyChangesCard">
      <div class="sectionHead"><div><span class="eyebrow">01 / WHAT CHANGED?</span><h2>Compare the evidence, then annotate</h2></div><button id="historyCompareChanges" class="primary">Compare changes</button></div>
      <p class="hint">Select a baseline and a current recording. Two selected overlay rows populate these fields in date order; you can choose explicitly or swap them. Choose the frequency band to compare and measure below.</p>
      <div class="historyChangeSelectors"><label>Baseline · before<select id="historyChangeBaseline"></select></label><button id="historySwapChanges" class="quiet" type="button" aria-label="Swap baseline and current recordings">⇄ Swap</button><label>Current · after<select id="historyChangeCurrent"></select></label></div>
      <p id="historyChangeStatus" class="hint" role="status">Choose two recordings to compare.</p>
      <div id="historyChangeResults" class="hidden">
        <div id="historyChangeWarnings" class="historyNotice"></div>
        <p id="historyChangeOverlayNote" class="hint"></p><div id="historyChangeOverlay" class="historyOverlay"></div>
        <h3 class="historyDeltaTitle">Signed PSD difference · current minus baseline</h3><p id="historyDeltaNote" class="hint"></p><div id="historyChangeDelta" class="historyDelta"></div>
        <div id="historyChangeFacts" class="historyFacts"></div>
        <button type="button" id="historyStartAnnotation" class="quiet">Annotate this change ↓</button>
        <form id="historyChangeNoteForm" class="historyChangeNoteForm">
          <h3>Keep your explanation separate from the observation</h3>
          <p class="hint">Your note is attached to the current recording with baseline/current identities, band, selected channel and a snapshot of the observed changes. It remains useful after re-analysis.</p>
          <div class="historyAnnotationGrid"><label>What I changed<textarea id="historyNoteChanged" maxlength="1000" required placeholder="Exact change and what stayed fixed. If several things changed, list them."></textarea></label><label>What I expected<textarea id="historyNoteExpected" maxlength="1000" placeholder="The feature and direction of change you predicted."></textarea></label><label>What I observed<textarea id="historyNoteObserved" maxlength="1000" required placeholder="What changed in the plot or band metrics, including what did not change."></textarea></label><label>My next controlled test<textarea id="historyNoteNext" maxlength="1000" placeholder="One change to test the explanation, plus a return to the baseline state."></textarea></label></div>
          <button id="historySaveChangeNote" class="primary">Save annotation with evidence</button><p id="historyNoteStatus" class="hint" role="status"></p>
        </form>
      </div>
    </div>`);

  document.body.insertAdjacentHTML('beforeend', `
    <dialog id="batchImportDialog" class="historyBatchDialog"><form id="batchImportForm">
      <div class="sectionHead"><div><span class="eyebrow">BUILD YOUR RECORDING HISTORY</span><h2>Import an archive or batch</h2></div><button type="button" id="batchClose" class="close" aria-label="Close batch import">×</button></div>
      <p>Choose ZIP / TGZ archives and .stdata files together. X/Y shaper recordings and belt comparisons become separate sets in the same batch. Original belt files containing both directions are supported.</p>
      <label class="dropzone">Choose recording files<input id="batchFiles" name="file" type="file" accept=".stdata,.zip,.tgz,.tar.gz" multiple required><small>Up to 128 recordings · 64 MiB per recording · 512 MiB total</small></label>
      <p id="batchFileSummary" class="hint" aria-live="polite"></p>
      <label>Printer<input name="printer" maxlength="200" placeholder="Optional — leave unknown if not recorded"></label>
      <div class="formGrid"><label>Square corner velocity<input name="scv" type="number" min="0" max="100" step="any" value="5" required></label><label>Maximum frequency (Hz)<input name="max_freq" type="number" min="20" max="1000" step="any" value="500" required></label><label>Maximum smoothing<input name="max_smoothing" type="number" min="0.001" max="10" step="any" placeholder="Engine default"></label></div>
      <p class="hint">500 Hz includes higher-frequency features that a 200 Hz view could miss. Match the original SCV and smoothing when using shaper recommendations.</p>
      <label>Conditions shared by this batch<textarea name="conditions" maxlength="10000" placeholder="Only record conditions you know apply to every selected recording."></textarea></label>
      <details><summary>Shared test & acquisition context</summary><p class="hint">Unknown context stays unknown. These values apply to every imported run; use separate batches for different known conditions. Use Auto direction for X/Y pairs.</p><div id="batchContext"></div></details>
      <p class="hint">Archive grouping is a convenience, not proof that test conditions match. Existing imports may be reused. Originals stay attached to their import.</p>
      <progress id="batchProgress" class="hidden" max="100" value="0" aria-label="Upload progress"></progress><p id="batchProgressText" class="hint" role="status"></p>
      <div class="actions"><button id="batchSubmit" class="primary">Import & analyse recordings →</button><button id="batchViewHistory" type="button" class="quiet hidden">View recording history</button></div>
      <div id="batchResult" aria-live="polite"></div><p id="batchError" class="error" role="alert"></p>
    </form></dialog>`);
  h('batchContext').innerHTML = typeof contextMarkup === 'function' ? contextMarkup() : '';
  const batchType=h('batchContext').querySelector('[data-context="test_type"]');batchType.insertAdjacentHTML('afterbegin','<option value="auto" selected>Auto · mixed X/Y, belts and other tests</option>');h('batchFileSummary').insertAdjacentElement('afterend',batchType.closest('label'));
  h('historyContextNotice').insertAdjacentElement('afterend',h('historyChangesCard'));
  h('historyChangesCard').querySelector('.historyChangeSelectors').insertAdjacentElement('beforebegin',h('historyBandForm'));

  function visibleRecords() {
    const excitation=h('historyExcitation').value, cohort=h('historyCohort').value;
    return state.records.filter(r=>(excitation==='all'||(r.excitation||'unknown')===excitation) && (cohort==='all'||r.cohort_key===cohort)).sort(chronological);
  }
  function maxFrequency() { return Math.min(1000,Math.max(20,+h('historyMaxFrequency').value||500)); }
  function bandBounds() {
    const low=+h('historyBandLow').value, high=+h('historyBandHigh').value;
    return Number.isFinite(low)&&Number.isFinite(high)&&low>=0&&high>low&&high<=1000 ? [low,high] : null;
  }
  function setShell() {
    for(const id of ['workspace','empty','pending','deleteImport','contextButton','renameImport']) h(id)?.classList.add('hidden');
    h('historyWorkspace').classList.remove('hidden');h('historyButton').classList.add('active');
    h('runTitle').textContent='Recording history';
    h('runMeta').textContent='Follow the evidence across tests, with acquisition changes kept in view.';
    document.querySelectorAll('#library .run.active').forEach(el=>el.classList.remove('active'));
  }
  const originalOpenRun=openRun;
  openRun=async function(id, restore=true) {
    // Existing background run polling must not navigate away from the archive.
    if(state.active && restore===false) return;
    state.active=false;h('historyWorkspace').classList.add('hidden');h('historyButton').classList.remove('active');
    await originalOpenRun(id,restore);
    if(state.active) setShell();
  };
  const originalTrashToggle=h('trashToggle').onclick;
  h('trashToggle').onclick=async()=>{state.active=false;h('historyWorkspace').classList.add('hidden');h('historyButton').classList.remove('active');await originalTrashToggle();};
  document.addEventListener('workspacechange',event=>{if(event.detail!=='history'){state.active=false;h('historyWorkspace').classList.add('hidden');h('historyButton').classList.remove('active');}});
  async function openHistory() {
    document.dispatchEvent(new CustomEvent('workspacechange',{detail:'history'}));state.active=true;setShell();
    await loadHistory();
    if(state.active) {setShell();await renderHistory();}
  }
  h('historyButton').onclick=openHistory;
  h('historyRefresh').onclick=async()=>{await loadHistory();if(state.active)await renderHistory();};
  function cohortOptions() {
    const previous=h('historyCohort').value, firstPopulatedLoad=!state.hadRecords&&state.records.length>0;
    const groups=new Map();
    for(const r of [...state.records].sort(chronological).reverse()) {
      const group=groups.get(r.cohort_key)||{label:r.cohort_label||r.source_label||'Unclassified',count:0};group.count++;groups.set(r.cohort_key,group);
    }
    h('historyCohort').innerHTML='<option value="all">All groups · context may differ</option>'+[...groups].map(([id,g])=>`<option value="${esc(id)}">${esc(g.label)} · ${g.count} recordings</option>`).join('');
    h('historyCohort').value=firstPopulatedLoad ? 'all' : groups.has(previous)||previous==='all' ? previous : 'all';
    if(state.records.length)state.hadRecords=true;
  }
  async function loadHistory() {
    if(state.busy)return;
    state.busy=true;h('historyRefresh').disabled=true;h('historyLoadStatus').textContent='Loading recording history…';
    try {
      const data=await api('history');h('historyLoadStatus').classList.remove('error');
      state.records=Array.isArray(data.records)?data.records:[];state.counts=data.counts||{};state.qualification=data.qualification||'';
      cohortOptions();
      if(!state.loaded) {
        const inGroup=state.records.filter(r=>h('historyCohort').value==='all'||r.cohort_key===h('historyCohort').value);
        if(!inGroup.some(r=>r.excitation==='X'))h('historyExcitation').value=inGroup.some(r=>r.excitation==='Y')?'Y':'all';
      }
      const active=visibleRecords();state.selected=new Set([...state.selected].filter(id=>active.some(r=>key(r)===id)));
      if(!state.loaded)selectRecent(false);
      state.loaded=true;
      h('historyLoadStatus').textContent=((state.counts.queued||state.counts.processing)?'Analysis continues in the background. This history updates as recordings become ready. ':'')+(data.errors?.length?`${data.errors.length} imports could not be indexed; open them from the library to inspect their status.`:'');
      updateCounts();
    } catch(e) {h('historyLoadStatus').textContent=e.message;h('historyLoadStatus').classList.add('error');}
    finally {state.busy=false;h('historyRefresh').disabled=false;}
  }
  function updateCounts() {
    const groups=new Set(state.records.map(r=>r.cohort_key)).size;
    h('historyCounts').innerHTML=`<div><strong>${state.records.length}</strong><span>unique recordings</span></div><div><strong>${groups}</strong><span>inferred setup groups</span></div><div><strong>${state.counts.queued||0} / ${state.counts.processing||0}</strong><span>imports queued / analysing</span></div><div><strong>${state.counts.failed||0}</strong><span>failed imports</span></div>`;
  }
  function contextNotice(records) {
    const notes=[];
    const knownValues=field=>new Set(records.map(r=>r.settings?.[field]).filter(v=>v&&v!=='unknown'));
    const axes=new Set(records.map(r=>r.signal_profile?.dominant_dc_axis).filter(v=>v&&v!=='unknown'));
    const directions=records.map(r=>r.signal_profile?.gravity_direction).filter(d=>d&&Number.isFinite(d.X)&&Number.isFinite(d.Y)&&Number.isFinite(d.Z));
    const referenceDirection=directions[0];
    const changedDirection=referenceDirection&&directions.some(d=>d.X*referenceDirection.X+d.Y*referenceDirection.Y+d.Z*referenceDirection.Z<.94);
    const rates=records.map(r=>r.quality?.sample_rate_hz).filter(Number.isFinite);
    if(new Set(records.map(r=>r.cohort_key)).size>1)notes.push('More than one inferred setup group is visible. Treat across-group differences as exploratory.');
    if(axes.size>1||changedDirection)notes.push('Apparent gravity direction changes across these recordings. Sensor pose or axis mapping may have changed; channel-by-channel power is not directly equivalent.');
    if(rates.length&&Math.max(...rates)/Math.min(...rates)>1.05)notes.push('Sample rates differ by more than 5%. Check acquisition hardware and settings before interpreting amplitude changes.');
    if(new Set(records.map(r=>r.source_label).filter(Boolean)).size>1)notes.push('Source labels differ. Filename labels can suggest a hardware or acquisition change, but do not confirm which part changed.');
    if(knownValues('axes_map').size>1)notes.push('Recorded axis mappings differ. Verify the physical direction represented by each channel.');
    if(knownValues('sensor').size>1)notes.push('Different sensor identifiers are recorded. Sensor response and calibration may affect absolute power.');
    if(records.some(r=>r.signal_profile?.gravity_plausible===false))notes.push('Some mean-acceleration magnitudes are outside the gravity plausibility screen. Check mounting, units and calibration.');
    if(h('historyScale').value==='absolute')notes.push('Absolute PSD is shown in ((mm/s²)²/Hz), assuming the recorded acceleration units are mm/s². Unverified excitation, sensor, mounting and conditions prevent a causal before/after interpretation.');
    const qualification=typeof state.qualification==='string'?state.qualification:'Groups are inferred from filename labels, sample-rate buckets and apparent gravity axis; they do not verify matching conditions.';
    h('historyContextNotice').innerHTML=`<strong>Keep the acquisition context in view</strong><p>${esc(qualification||'Groups are inferred from filenames, sample rates and apparent gravity axis; matching groups do not prove matching test conditions.')}</p>${notes.map(n=>`<p class="historyCaveat">${esc(n)}</p>`).join('')}<p class="hint">Apparent gravity is a mean-acceleration clue, not a calibrated pose. Unknown mounting, mapping and test settings remain unverified.</p>`;
  }
  function selectRecent(render=true) { state.selected=new Set(visibleRecords().filter(hasPSD).slice(-3).map(key));if(render) {renderRows(visibleRecords());renderOverlay(visibleRecords());updateChangeOptions(visibleRecords(),true);} }
  function toggleSelection(record) {
    const id=key(record);
    if(state.selected.has(id))state.selected.delete(id);
    else if(state.selected.size<6)state.selected.add(id);
    else {toast('Choose up to six native spectra. Clear one selection first.');renderRows(visibleRecords());return;}
    const rows=visibleRecords();renderRows(rows);renderOverlay(rows);if(state.selected.size===2)updateChangeOptions(rows,true);
  }
  h('historySelectRecent').onclick=()=>selectRecent();
  h('historyClearSelection').onclick=()=>{state.selected.clear();renderRows(visibleRecords());renderOverlay(visibleRecords());};
  for(const id of ['historyExcitation','historyCohort'])h(id).onchange=()=>{selectRecent(false);renderHistory();};
  for(const id of ['historyChannel','historyScale'])h(id).onchange=()=>renderHistory();
  h('historyMaxFrequency').onchange=()=>{h('historyMaxFrequency').value=maxFrequency();renderHistory(false);};
  h('historyBandForm').onsubmit=e=>{e.preventDefault();if(!bandBounds()){toast('Choose a band with 0 ≤ start < end ≤ 1000 Hz.');return;}invalidateChange();renderOverlay(visibleRecords());renderHeatmap(visibleRecords());measureBand(visibleRecords());};
  for(const id of ['historyBandLow','historyBandHigh'])h(id).oninput=invalidateChange;

  async function renderHistory(recalculate=true) {
    const sequence=++state.renderSequence, records=visibleRecords();
    if(recalculate){++state.bandSequence;state.band.clear();}
    contextNotice(records);renderRows(records);updateChangeOptions(records);
    h('historyMapCount').textContent=`${records.length} recordings · ${h('historyExcitation').selectedOptions[0].textContent}`;
    await renderHeatmap(records);if(sequence!==state.renderSequence||!state.active)return;
    await renderOverlay(records);
    if(state.change)await renderChangeResults();
    if(recalculate)await measureBand(records);
  }
  function normalizedValues(r,axis) {
    const values=r.raw_psd?.[axis]||[];
    if(h('historyScale').value==='absolute')return values;
    const maximum=values.reduce((best,v,i)=>r.frequency?.[i]<=maxFrequency()&&Number.isFinite(v)?Math.max(best,v):best,0);
    return values.map(v=>maximum>0&&Number.isFinite(v)?v/maximum:null);
  }
  function interpolate(frequency,values,grid) {
    let index=0;
    return grid.map(f=>{
      if(f<frequency[0]||f>frequency.at(-1))return null;
      while(index<frequency.length-2&&frequency[index+1]<f)index++;
      const x0=frequency[index],x1=frequency[index+1],y0=values[index],y1=values[index+1];
      return Number.isFinite(y0)&&Number.isFinite(y1)&&x1>x0 ? y0+(y1-y0)*(f-x0)/(x1-x0) : null;
    });
  }
  function plotLayout() {
    return {...structuredClone(layoutBase),margin:{l:75,r:35,t:30,b:62},legend:{orientation:'h',y:-.2,font:{size:10}},xaxis:{...layoutBase.xaxis,range:[0,maxFrequency()]},yaxis:{...layoutBase.yaxis,autorange:true},hovermode:'x unified'};
  }
  function bandShape() {const b=bandBounds();return b?[{type:'rect',x0:b[0],x1:b[1],y0:0,y1:1,xref:'x',yref:'paper',fillcolor:'rgba(36,99,76,.07)',line:{color:'#8cad9a',width:1},layer:'below'}]:[];}
  const plotOptions={responsive:true,displaylogo:false,modeBarButtonsToRemove:['lasso2d','select2d','sendDataToCloud']};
  async function renderHeatmap(records) {
    const axis=h('historyChannel').value, relative=h('historyScale').value==='relative';
    h('historyScaleNote').textContent=relative?'Relative shape: each recording’s selected-channel peak within the display range is scaled to 1. Colours compare frequency patterns, not vibration magnitude.':'Absolute ordinary PSD: all recordings share one colour scale in ((mm/s²)²/Hz). A brighter row can reflect changed acquisition or excitation, not only changed mechanics.';
    const plot=h('historyHeatmap');
    if(!records.length){Plotly.purge(plot);plot.innerHTML='<div class="historyPlotEmpty">No spectra match the current filters.</div>';plot.style.height='200px';return;}
    const grid=Array.from({length:501},(_,i)=>i*maxFrequency()/500);
    const z=records.map(r=>hasPSD(r)?interpolate(r.frequency,normalizedValues(r,axis),grid):grid.map(()=>null));
    const labels=records.map((r,i)=>`${i+1}. ${stamp(r)} · ${r.excitation||'?'}`);
    const layout=plotLayout();layout.margin={l:190,r:75,t:20,b:55};layout.hovermode='closest';layout.showlegend=false;layout.yaxis={type:'category',categoryorder:'array',categoryarray:records.map(key),autorange:'reversed',tickmode:'array',tickvals:records.map(key),ticktext:labels,automargin:true,title:{text:''}};layout.shapes=bandShape();
    plot.style.height=`${Math.max(240,Math.min(1700,records.length*25+80))}px`;
    if(plot.removeAllListeners)plot.removeAllListeners('plotly_click');
    await Plotly.react(plot,[{type:'heatmap',x:grid,y:records.map(key),z,customdata:records.map(r=>grid.map(()=>shortLabel(r))),zmin:0,...(relative?{zmax:1}:{}),colorscale:[[0,'#f4f7eb'],[.15,'#d9e9bc'],[.4,'#99c087'],[.7,'#40886a'],[1,'#174d3e']],colorbar:{title:{text:relative?'Relative<br>peak = 1':'PSD'},thickness:12},hovertemplate:`%{customdata}<br>%{x:.1f} Hz · %{z:.4g}${relative?' relative':' ((mm/s²)²/Hz)'}<extra></extra>`,hoverongaps:false}],layout,plotOptions);
    plot.on('plotly_click',event=>{const point=event.points?.[0];const record=records.find(r=>key(r)===point?.y);if(record&&hasPSD(record))toggleSelection(record);});
  }
  async function renderOverlay(records) {
    const selected=records.filter(r=>state.selected.has(key(r))&&hasPSD(r)), relative=h('historyScale').value==='relative',axis=h('historyChannel').value;
    h('historySelectionCount').textContent=`${selected.length} / 6 selected`;
    const plot=h('historyOverlay');
    if(!selected.length){Plotly.purge(plot);plot.innerHTML='<div class="historyPlotEmpty">Choose a recording in the table or click a heatmap row to inspect its spectrum.</div>';return;}
    const layout=plotLayout();layout.yaxis.title={text:relative?'Relative ordinary PSD (peak = 1)':'Ordinary PSD ((mm/s²)²/Hz)'};layout.shapes=bandShape();layout.legend={...layout.legend,itemclick:false,itemdoubleclick:false};
    await Plotly.react(plot,selected.map((r,i)=>({type:'scatter',mode:'lines',x:r.frequency,y:normalizedValues(r,axis),name:shortLabel(r),line:{color:palette[i],width:2},hovertemplate:`${esc(shortLabel(r))}<br>%{x:.2f} Hz · %{y:.4g}<extra></extra>`})),layout,plotOptions);
  }
  function contextDetails(r) {
    const settings=r.settings||{}, fields=[['Printer',settings.printer],['Sensor',settings.sensor],['Mount',settings.sensor_mount],['Axis map',settings.axes_map],['Acquisition group',settings.acquisition_group],['Umbilical',settings.umbilical],['Intervention',settings.intervention],['Position',settings.position],['Test mode',settings.test_mode],['Excitation / Hz',settings.accel_per_hz],['Sweep Hz/s',settings.sweep_rate],['Fans',settings.fans],['Temperature',settings.temperature],['Conditions',settings.conditions]];
    const known=fields.filter(([,v])=>v&&v!=='unknown').length;
    return `<details><summary>${known} / ${fields.length} fields recorded</summary><dl>${fields.map(([label,v])=>`<dt>${label}</dt><dd>${esc(v||'Unknown')}</dd>`).join('')}<dt>Inferred group</dt><dd>${esc(r.cohort_label||'Unknown')}</dd><dt>Date source</dt><dd>${esc(r.recorded_at_source||'Unknown')}</dd><dt>Persistent candidates</dt><dd>${r.spectral_features?(r.spectral_features.candidates||[]).map(c=>number(c.frequency_hz)+' Hz').join(', ')||'None met the screen':'Re-analyse to calculate'}</dd></dl></details>`;
  }
  function gravityDescription(r) {
    const p=r.signal_profile;if(!p)return 'Apparent gravity unavailable';
    const d=p.gravity_direction||{};
    return `${p.dominant_dc_axis||'?'} DC · ${number(p.apparent_gravity_norm,0)} mm/s² · direction [${number(d.X,2)}, ${number(d.Y,2)}, ${number(d.Z,2)}]${p.gravity_plausible===false?' · check units / mounting':''}`;
  }
  function shareMarkup(shares) {
    if(!shares||!['X','Y','Z'].every(axis=>Number.isFinite(shares[axis])))return '—';
    return `<div class="historyShareBar" aria-hidden="true">${['X','Y','Z'].map(axis=>`<span style="width:${Math.max(0,Math.min(100,shares[axis]*100))}%;background:${colors[axis]}"></span>`).join('')}</div><span class="historyShares">${['X','Y','Z'].map(axis=>`<span title="${axis} share">${number(shares[axis]*100)}%</span>`).join(' / ')}</span>`;
  }
  function renderRows(records) {
    h('historyEmpty').classList.toggle('hidden',records.length>0);
    h('historyBandAxis').textContent=h('historyChannel').value==='XYZ'?'X+Y+Z':h('historyChannel').value;
    h('historyRows').innerHTML=[...records].reverse().map(r=>{
      const id=key(r), metric=state.band.get(id), quality=r.quality, selected=state.selected.has(id), available=hasPSD(r);
      const peaks=Array.isArray(metric?.peaks)?metric.peaks:metric?.peaks?.[h('historyChannel').value]||[];
      return `<tr class="${selected?'selected':''}"><td><input type="checkbox" data-history-select="${esc(id)}" aria-label="Overlay ${esc(shortLabel(r))}" ${selected?'checked':''} ${available?'':'disabled'}></td><td><strong>${esc(stamp(r))}</strong><small>${esc(r.friendly_name||r.source_label||r.title||r.name)} · ${esc(r.excitation||'unknown')} test</small>${r.duplicate_count>1?`<small>${r.duplicate_count} identical stored copies</small>`:''}</td><td>${metric?.error?`<span class="error" title="${esc(metric.error)}">Unavailable</span>`:power(metric?.power)}${metric?.error?`<small>${esc(metric.error)}</small>`:''}</td><td>${power(metric?.rms)}</td><td>${shareMarkup(metric?.shares)}</td><td>${peaks.length?peaks.map(p=>number(typeof p==='number'?p:p.frequency)).join(', '):metric&&!metric.error?'None detected':'—'}</td><td><span class="historyQuality ${quality?.status==='pass'?'pass':'review'}">${esc(quality?.status||'unchecked')}</span><small>${number(quality?.sample_rate_hz,0)} samples/s · ${number(quality?.resolution_hz,2)} Hz bins</small><small title="${esc(gravityDescription(r))}">${esc(gravityDescription(r))}</small>${quality?.warnings?.length?`<details><summary>Timing notes</summary>${quality.warnings.map(w=>`<p class="hint">${esc(w)}</p>`).join('')}</details>`:''}</td><td>${contextDetails(r)}</td><td><button type="button" class="quiet" data-history-open="${esc(id)}">Open ↗</button></td></tr>`;
    }).join('');
    h('historyRows').querySelectorAll('[data-history-select]').forEach(el=>el.onchange=()=>{const r=records.find(r=>key(r)===el.dataset.historySelect);if(r)toggleSelection(r);});
    h('historyRows').querySelectorAll('[data-history-open]').forEach(el=>el.onclick=async()=>{
      const r=records.find(r=>key(r)===el.dataset.historyOpen);if(!r)return;
      await openRun(r.run_id);
      if(run?.id!==r.run_id||!run.result?.measurements?.[r.measurement])return;
      view.measurement=r.measurement;delete view.band;delete view.xRange;delete view.yRange;
      h('measurement').value=r.measurement;await render();renderNotes();await prepareDiagnostics();saveView();
      h('runTitle').scrollIntoView({behavior:'smooth',block:'start'});
    });
  }
  async function measureBand(records) {
    const bounds=bandBounds(), sequence=++state.bandSequence;
    state.band.clear();renderRows(records);h('historyBandWarnings').textContent='';
    if(!bounds||!records.length){h('historyMeasureBand').disabled=false;h('historyBandStatus').textContent=!bounds?'Enter a valid frequency band.':'No recordings selected by filters.';return;}
    h('historyBandStatus').textContent='Calculating from native PSD bins…';h('historyMeasureBand').disabled=true;
    try {
      const data={records:[],warnings:[]},axis=h('historyChannel').value;
      // The archive can span multiple uploads; each metric request has a 128-reference limit.
      for(let offset=0;offset<records.length;offset+=128){
        const page=await api('history/band',json('POST',{references:records.slice(offset,offset+128).map(reference),low:bounds[0],high:bounds[1],axis}));
        if(sequence!==state.bandSequence)return;
        data.records.push(...(page.records||[]));data.warnings.push(...(page.warnings||[]));
      }
      data.warnings=[...new Set(data.warnings)];
      if(sequence!==state.bandSequence)return;
      state.band=new Map((data.records||[]).map(r=>[key(r),r]));renderRows(records);
      const errors=(data.records||[]).filter(r=>r.error).length;
      h('historyBandStatus').textContent=`${bounds[0]}–${bounds[1]} Hz · ${data.records?.length||0} recordings${errors?` · ${errors} unavailable`:''}`;
      const warnings=Array.isArray(data.warnings)?data.warnings:[];
      h('historyBandWarnings').innerHTML=warnings.length?`<details class="historyBandCaveats"><summary>${warnings.length} band comparison caveats</summary>${warnings.map(w=>`<p class="hint">${esc(typeof w==='string'?w:JSON.stringify(w))}</p>`).join('')}</details>`:'';
    }catch(e){if(sequence===state.bandSequence)h('historyBandStatus').textContent=e.message;}
    finally{if(sequence===state.bandSequence)h('historyMeasureBand').disabled=false;}
  }

  function changeSignature() {return `${h('historyChangeBaseline').value}|${h('historyChangeCurrent').value}|${h('historyBandLow').value}|${h('historyBandHigh').value}`;}
  function invalidateChange() {
    ++state.changeSequence;state.change=null;
    h('historyChangeResults').classList.add('hidden');h('historyCompareChanges').disabled=false;
    h('historyChangeStatus').textContent='Selection or band changed. Compare again before annotating.';
  }
  function updateChangeOptions(records,preferSelected=false) {
    const before=h('historyChangeBaseline').value,after=h('historyChangeCurrent').value;
    const available=records.filter(hasPSD), selected=available.filter(r=>state.selected.has(key(r)));
    const defaults=selected.length>=2?selected:available.slice(-2);
    const html=available.map(r=>`<option value="${esc(key(r))}">${esc(shortLabel(r))}</option>`).join('');
    h('historyChangeBaseline').innerHTML=html;h('historyChangeCurrent').innerHTML=html;
    h('historyChangeBaseline').value=!preferSelected&&available.some(r=>key(r)===before)?before:defaults[0]?key(defaults[0]):'';
    h('historyChangeCurrent').value=!preferSelected&&available.some(r=>key(r)===after)?after:defaults.at(-1)?key(defaults.at(-1)):'';
    if(before!==h('historyChangeBaseline').value||after!==h('historyChangeCurrent').value)invalidateChange();
    const enough=available.length>=2;
    h('historyCompareChanges').disabled=!enough;h('historySwapChanges').disabled=!enough;
    if(!enough)h('historyChangeStatus').textContent='At least two recordings with supported spectra are needed. Try another setup group or excitation filter.';
  }
  for(const id of ['historyChangeBaseline','historyChangeCurrent'])h(id).onchange=invalidateChange;
  h('historySwapChanges').onclick=()=>{const before=h('historyChangeBaseline').value;h('historyChangeBaseline').value=h('historyChangeCurrent').value;h('historyChangeCurrent').value=before;invalidateChange();};
  h('historyCompareChanges').onclick=async()=>{
    const baseline=state.records.find(r=>key(r)===h('historyChangeBaseline').value),current=state.records.find(r=>key(r)===h('historyChangeCurrent').value),bounds=bandBounds();
    if(!baseline||!current||key(baseline)===key(current)){toast('Choose two different recordings for baseline and current.');return;}
    if(!bounds){toast('Choose a valid frequency band above before comparing.');return;}
    const sequence=++state.changeSequence,signature=changeSignature();
    state.change=null;h('historyChangeResults').classList.add('hidden');h('historyCompareChanges').disabled=true;h('historyChangeStatus').textContent='Checking context and calculating observed changes…';
    try {
      const data=await api('changes',json('POST',{baseline:reference(baseline),current:reference(current),low:bounds[0],high:bounds[1]}));
      if(sequence!==state.changeSequence||signature!==changeSignature())return;
      state.change={data,baseline:structuredClone(baseline),current:structuredClone(current),signature,capturedAt:new Date().toISOString()};
      h('historyChangeStatus').textContent=`Baseline: ${stamp(baseline)} → current: ${stamp(current)} · ${bounds[0]}–${bounds[1]} Hz`;
      h('historyNoteStatus').textContent=['historyNoteChanged','historyNoteExpected','historyNoteObserved','historyNoteNext'].some(id=>h(id).value.trim())?'Draft text retained. Review it for this baseline, current recording and band before saving.':'';
      const prompts=new Map((data.annotation_prompts||[]).map(p=>[p.key,p.prompt]));
      if(prompts.has('what_changed'))h('historyNoteChanged').placeholder=prompts.get('what_changed');
      if(prompts.has('expected_outcome'))h('historyNoteExpected').placeholder=prompts.get('expected_outcome');
      if(prompts.has('next_controlled_test'))h('historyNoteNext').placeholder=prompts.get('next_controlled_test');
      await renderChangeResults();
    }catch(e){if(sequence===state.changeSequence)h('historyChangeStatus').textContent=e.message;}
    finally{if(sequence===state.changeSequence)h('historyCompareChanges').disabled=false;}
  };
  function relevantObservations(data,axis) {
    return (data.observations||[]).filter(o=>!['power','peaks','unmatched'].includes(o.kind)||o.title?.startsWith(axis+' '));
  }
  async function renderChangeResults() {
    if(!state.change||state.change.signature!==changeSignature())return;
    const {data,baseline,current}=state.change,axis=h('historyChannel').value,check=data.compatibility||{},sequence=state.changeSequence;
    h('historyChangeResults').classList.remove('hidden');
    const blockers=check.blockers||[],warnings=check.warnings||[];
    h('historyChangeWarnings').innerHTML=`<strong>${check.comparable?'Numerical comparison available · interpretation remains conditional':'Exploratory overlay · numerical difference withheld'}</strong>${blockers.map(w=>`<p>${esc(w)}</p>`).join('')}${warnings.length?`<details><summary>Test context · ${warnings.length} caveats</summary>${warnings.map(w=>`<p>${esc(w)}</p>`).join('')}</details>`:''}<p class="hint">${esc(data.qualification||'Observed changes do not establish a cause or whether the machine improved.')}</p>`;
    h('historyChangeOverlayNote').textContent=`${axis==='XYZ'?'Combined X+Y+Z':axis} · ${data.overlay?.qualification||'Native ordinary PSD on one absolute scale.'}`;
    const layout=plotLayout();layout.yaxis.title={text:'Ordinary PSD ((mm/s²)²/Hz)'};layout.shapes=bandShape();layout.legend={orientation:'h',y:1.13,font:{size:11},itemclick:false,itemdoubleclick:false};
    await Plotly.react('historyChangeOverlay',[
      {type:'scatter',mode:'lines',x:data.overlay?.baseline?.frequency||[],y:data.overlay?.baseline?.raw_psd?.[axis]||[],name:`Baseline · ${stamp(baseline)}${baseline.friendly_name?" · "+baseline.friendly_name:""}`,line:{color:'#8d74b5',width:2,dash:'dash'}},
      {type:'scatter',mode:'lines',x:data.overlay?.current?.frequency||[],y:data.overlay?.current?.raw_psd?.[axis]||[],name:`Current · ${stamp(current)}${current.friendly_name?" · "+current.friendly_name:""}`,line:{color:'#24634c',width:2.5}}
    ],layout,plotOptions);
    if(sequence!==state.changeSequence)return;
    const delta=h('historyChangeDelta');
    if(check.comparable&&data.delta?.psd?.[axis]) {
      h('historyDeltaNote').textContent=`${data.delta.method} Positive means more recorded power; negative means less. Neither is an automatic better/worse verdict.`;
      const differenceLayout=plotLayout();differenceLayout.margin.t=20;differenceLayout.showlegend=false;differenceLayout.xaxis.range=data.band;differenceLayout.yaxis={...differenceLayout.yaxis,title:{text:'PSD change ((mm/s²)²/Hz)'},zeroline:true,zerolinecolor:'#566b5c',zerolinewidth:1.5};
      await Plotly.react(delta,[{type:'scatter',mode:'lines',x:data.delta.frequency,y:data.delta.psd[axis],name:'Current − baseline',line:{color:'#557cc9',width:2},fill:'tozeroy',fillcolor:'rgba(85,124,201,.12)',hovertemplate:'%{x:.2f} Hz<br>Current − baseline: %{y:+.4g}<extra></extra>'}],differenceLayout,plotOptions);
    } else {Plotly.purge(delta);delta.innerHTML='<div class="historyPlotEmpty">A signed difference is withheld because acquisition or test direction is not sufficiently comparable. Review the context above.</div>';h('historyDeltaNote').textContent='The overlay can show patterns, but does not isolate a mechanical change.';}
    h('historyChangeFacts').innerHTML=relevantObservations(data,axis).map(o=>`<article><span class="eyebrow">${esc(o.kind||'observation')}</span><h3>${esc(o.title)}</h3><p>${esc(o.detail)}</p></article>`).join('');
  }
  h('historyStartAnnotation').onclick=()=>{h('historyChangeNoteForm').scrollIntoView({behavior:'smooth',block:'start'});h('historyNoteChanged').focus({preventScroll:true});};
  h('historyChangeNoteForm').onsubmit=async event=>{
    event.preventDefault();const snapshot=state.change;
    if(!snapshot||snapshot.signature!==changeSignature()){toast('Compare the selected recordings again before saving the annotation.');return;}
    const axis=h('historyChannel').value,{data,baseline,current}=snapshot;
    const fields=[['What I changed',h('historyNoteChanged').value],['What I expected',h('historyNoteExpected').value],['What I observed',h('historyNoteObserved').value],['My next controlled test',h('historyNoteNext').value]];
    const metric=data.axes?.[axis];
    const metricSnapshot=metric?{before_power:metric.before_power,after_power:metric.after_power,change_percent:metric.change_percent,before_rms:metric.before_rms,after_rms:metric.after_rms,matched_peaks:metric.peaks?.matched?.map(p=>({before_hz:p.before.frequency,after_hz:p.after.frequency,shift_hz:p.shift_hz,power_change_percent:p.power_change_percent}))}:null;
    const observationSnapshot=relevantObservations(data,axis).map(o=>`${o.title}: ${o.detail}`).join('\n');
    const text=`Comparison annotation\nBaseline: ${shortLabel(baseline)} | run ${baseline.run_id}, measurement ${baseline.measurement} (${baseline.name||'unnamed'})\nCurrent: ${shortLabel(current)} | run ${current.run_id}, measurement ${current.measurement} (${current.name||'unnamed'})\nBand: ${data.band?.join('–')} Hz | recorded channel ${axis}\nEvidence captured: ${snapshot.capturedAt}\n\n${fields.map(([label,value])=>`${label}:\n${value.trim()||'Not recorded'}`).join('\n\n')}\n\nObserved evidence snapshot (retained even if the recording is re-analysed):\nNumerically comparable: ${!!data.compatibility?.comparable}\n${[...(data.compatibility?.blockers||[]),...(data.compatibility?.warnings||[])].join('\n')}\n${observationSnapshot}\nSelected-channel metrics: ${JSON.stringify(metricSnapshot)}\n${data.delta?.method||'Numerical difference withheld.'}\n${data.qualification||''}`;
    if(text.length>10000){h('historyNoteStatus').textContent='The annotation plus evidence exceeds the note limit. Shorten your text and try again.';return;}
    h('historySaveChangeNote').disabled=true;h('historyNoteStatus').textContent='Saving annotation and observed evidence…';
    try {
      await api(`runs/${current.run_id}/notes`,json('POST',{text,measurement:current.measurement,low:data.band[0],high:data.band[1]}));
      h('historyNoteStatus').textContent='Saved to the current recording with baseline identity and observed evidence.';toast('Comparison annotation saved.');
    }catch(e){h('historyNoteStatus').textContent=e.message;}
    finally{h('historySaveChangeNote').disabled=false;}
  };

  function showBatch() {h('batchImportDialog').showModal();}
  h('batchImportButton').onclick=showBatch;h('historyUpload').onclick=showBatch;
  h('batchClose').onclick=()=>h('batchImportDialog').close();
  h('batchImportDialog').addEventListener('cancel',e=>{if(state.uploadBusy)e.preventDefault();});
  h('batchViewHistory').onclick=()=>{h('batchImportDialog').close();openHistory();};
  function validateFiles() {
    const files=[...h('batchFiles').files], total=files.reduce((sum,f)=>sum+f.size,0);
    h('batchFileSummary').textContent=files.length?`${files.length} selected file${files.length===1?'':'s'} · ${number(total/1048576,1)} MiB`:'';
    if(!files.length)return 'Choose an archive or .stdata recordings.';
    if(files.length>128)return 'Choose at most 128 files in one batch.';
    if(files.some(f=>!/(\.stdata|\.zip|\.tgz|\.tar\.gz)$/i.test(f.name)))return 'Choose only .stdata, .zip, .tgz or .tar.gz files.';
    if(files.some(f=>/\.stdata$/i.test(f.name)&&f.size>64*1048576))return 'Each .stdata file must be no larger than 64 MiB.';
    if(total>512*1048576)return 'The selected batch exceeds 512 MiB.';
    return '';
  }
  h('batchFiles').onchange=()=>{h('batchError').textContent=validateFiles();h('batchResult').textContent='';h('batchViewHistory').classList.add('hidden');};
  function uploadBatch(payload) {
    return new Promise((resolve,reject)=>{
      const request=new XMLHttpRequest();request.open('POST','/api/batch');request.responseType='json';
      request.upload.onprogress=event=>{if(event.lengthComputable){const percent=100*event.loaded/event.total;h('batchProgress').value=percent;h('batchProgressText').textContent=percent<100?`Uploading ${Math.floor(percent)}%…`:'Upload received. Checking archive and queueing recordings…';}};
      request.onload=()=>{if(request.status>=200&&request.status<300)resolve(request.response||{});else {const detail=request.response?.detail;reject(Error(typeof detail==='string'?detail:detail?JSON.stringify(detail):request.status===401?'Your session expired. Sign in again before importing.':`Import failed (${request.status}).`));}};
      request.onerror=()=>reject(Error('The upload connection failed. Check the connection and refresh recording history before retrying.'));
      request.onabort=()=>reject(Error('Upload interrupted. Refresh recording history before retrying.'));
      request.send(payload);
    });
  }
  h('batchImportForm').onsubmit=async event=>{
    event.preventDefault();if(state.uploadBusy)return;
    const validation=validateFiles();h('batchError').textContent=validation;if(validation)return;
    const form=event.target,payload=new FormData(form);
    const context=typeof contextValues==='function'?contextValues(h('batchContext')):{},automatic=batchType.value==='auto';context.test_type=automatic?'shaper':batchType.value;payload.set('auto_detect',String(automatic));payload.append('context',JSON.stringify(context));
    state.uploadBusy=true;h('batchSubmit').disabled=true;h('batchClose').disabled=true;h('batchFiles').disabled=true;
    h('batchResult').textContent='';h('batchViewHistory').classList.add('hidden');h('batchProgress').classList.remove('hidden');h('batchProgress').value=0;h('batchProgressText').textContent='Uploading recordings…';
    try {
      const result=await uploadBatch(payload), imports=result.imports||[], skipped=result.skipped||[];
      h('batchProgress').value=100;h('batchProgressText').textContent='Upload complete. Analysis runs in the background.';
      h('batchResult').innerHTML=`<p class="historyImportSuccess"><strong>${result.created??imports.filter(r=>!r.reused).length} new imports queued</strong> · ${result.reused??imports.filter(r=>r.reused).length} existing imports reused.</p>${skipped.length?`<details><summary>${skipped.length} skipped archive entries</summary><ul>${skipped.map(entry=>`<li>${esc(typeof entry==='string'?entry:entry.filename?`${entry.filename}: ${entry.reason||entry.error||'Skipped'}`:JSON.stringify(entry))}</li>`).join('')}</ul></details>`:''}`;
      h('batchResult').insertAdjacentHTML('beforeend','<ul>'+imports.map(item=>`<li>${esc(item.title)} · ${esc(item.test_type==='belts'?'Belts comparison':'Input '+(item.test_type||'shaper'))}</li>`).join('')+'</ul>');h('batchViewHistory').classList.remove('hidden');
      await refreshLibrary();await loadHistory();if(state.active)await renderHistory();
    }catch(e){h('batchError').textContent=e.message;h('batchProgressText').textContent='Import did not complete successfully. Your existing recordings are unchanged.';}
    finally {state.uploadBusy=false;h('batchSubmit').disabled=false;h('batchClose').disabled=false;h('batchFiles').disabled=false;}
  };
  setInterval(async()=>{
    if(!state.active||state.busy||h('app').classList.contains('hidden'))return;
    const pending=(state.counts.queued||0)+(state.counts.processing||0);
    if(!pending&&!library.some(r=>['queued','processing'].includes(r.status)))return;
    await loadHistory();if(state.active) {setShell();await renderHistory();}
  },5000);
})();
