'use strict';
// Config generation uses the same saved recommendations as the acceleration cards.
(() => {
 let signature='', sequence=0;
 $('shaperSummary').insertAdjacentHTML('beforeend',`<details id="printerConfigPanel" class="printerConfig" open><summary>Recommended printer.cfg settings</summary><p class="hint">Merge these lines into your existing <code>[input_shaper]</code> section. Each axis uses the recommendation from this imported set.</p><div id="configChoices" class="formGrid"></div><label class="inline"><input id="configDamping" type="checkbox" checked>Include estimated damping ratios</label><p class="hint">Damping estimates are optional; Klipper normally defaults to 0.1. <a href="https://www.klipper3d.org/Config_Reference.html#input_shaper" target="_blank" rel="noopener">Klipper settings reference ↗</a></p><label class="srOnly" for="printerConfigText">Recommended input shaper configuration</label><textarea id="printerConfigText" readonly spellcheck="false" rows="7"></textarea><div class="sectionHead"><p id="configStatus" class="hint" role="status"></p><button id="copyPrinterConfig" class="quiet" disabled>Copy settings</button></div><ul id="configWarnings" class="hint hidden"></ul></details>`);
 async function updateConfig(reset=false){
  const ticket=++sequence,id=run.id,query=new URLSearchParams({include_damping:String($('configDamping').checked)});
  if(!reset)for(const select of $('configChoices').querySelectorAll('select'))query.set(select.dataset.axis.toLowerCase(),select.value);
  $('copyPrinterConfig').disabled=true;$('printerConfigText').value='';$('configStatus').textContent='Preparing settings…';$('configWarnings').classList.add('hidden');
  try{
   const config=await api(`runs/${id}/printer-config?${query}`);
   if(ticket!==sequence||run.id!==id)return;
   if(reset)$('configChoices').innerHTML=Object.entries(config.choices).map(([axis,choices])=>`<label>${axis} recommendation<select data-axis="${axis}" ${choices.length<2?'disabled':''}>${choices.map(choice=>`<option value="${escapeHTML(choice.type)}">${escapeHTML(choice.type)} @ ${choice.frequency.toFixed(1)} Hz</option>`).join('')}</select></label>`).join('');
   $('configChoices').querySelectorAll('select').forEach(select=>select.onchange=()=>updateConfig());
   $('printerConfigText').value=config.text;$('copyPrinterConfig').disabled=false;
   $('configStatus').textContent=config.complete?'Ready to copy · X and Y':`Partial configuration · ${config.axes.join(' + ')} only`;
   $('configWarnings').innerHTML=config.warnings.map(warning=>`<li>${escapeHTML(warning)}</li>`).join('');$('configWarnings').classList.toggle('hidden',!config.warnings.length);
  }catch(error){if(ticket===sequence&&run.id===id)$('configStatus').textContent=error.message;}
 }
 const previous=renderShaperSummary;
 renderShaperSummary=function(){previous();const next=JSON.stringify([run.id,run.result.test_type,(run.result.measurements||[]).map(m=>[m.excitation,m.choices,(m.filters||[]).map(f=>[f.type,f.frequency]),m.damping,m.damping_status,m.quality])]);if(next===signature)return;signature=next;sequence++;$('configChoices').innerHTML='';$('printerConfigText').value='';$('copyPrinterConfig').disabled=true;if(!$('shaperSummary').classList.contains('hidden')){$('configDamping').checked=true;updateConfig(true);}};
 $('configDamping').onchange=()=>updateConfig();
 $('copyPrinterConfig').onclick=async()=>{
  const field=$('printerConfigText');if(!field.value)return;
  try{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(field.value);else{field.focus();field.select();if(!document.execCommand('copy'))throw new Error('Clipboard unavailable');}$('configStatus').textContent='Settings copied';toast('printer.cfg settings copied');}
  catch{field.focus();field.select();$('configStatus').textContent='Settings selected — press Ctrl+C / Cmd+C to copy.';}
 };
})();
