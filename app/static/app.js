'use strict';
let csrf = '', currentView = 'overview', state = null, refreshPending = false, toastTimer = null, settingsData = null, previewRequest = 0;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = s => { if (!s) return 'Never'; const d = new Date(s); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); };
const fmtSize = n => n ? (n / 1048576 >= 1024 ? (n/1073741824).toFixed(2)+' GiB' : (n/1048576).toFixed(0)+' MiB') : '—';
// A watch is completed when the one-off/limited queue quota has been reached.
// This is distinct from a SABnzbd download's 'completed' status in Activity.
const isCompletedWatch = w => !w.enabled && w.max_grabs > 0 && w.grab_count >= w.max_grabs;
const watchStatus = w => isCompletedWatch(w) ? ['COMPLETE','completed'] : w.enabled ? ['ACTIVE',''] : ['PAUSED','paused'];
const formatStatus = s => ({found:'Found',queued:'Queued',completed:'Completed',assumed_completed:'Assumed complete',failed:'Queue failed',failed_download:'Download failed',skipped:'Duplicate'}[s] || s);
function toast(message, bad=false) { const el=$('toast');el.textContent=message;el.className='toast'+(bad?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.add('hidden'),4200); }
async function api(path, opts={}) {
  const options={credentials:'same-origin',...opts};
  options.headers={...(opts.headers||{})};
  if (opts.body && typeof opts.body !== 'string') {options.headers['Content-Type']='application/json'; options.body=JSON.stringify(opts.body);}
  if (options.method && options.method !== 'GET') options.headers['X-CSRF-Token']=csrf;
  const res=await fetch(path,options);
  const content=await res.text(); let data;
  try{data=content?JSON.parse(content):{}}catch{data={detail:content.slice(0,200)}}
  if (!res.ok) {if(res.status===401){showLogin();} const msg=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data);throw new Error(msg||`Request failed: ${res.status}`);}
  return data;
}
function showLogin(){ $('login').classList.remove('hidden'); $('application').classList.add('hidden'); $('password').value=''; }
function showApp(){ $('login').classList.add('hidden'); $('application').classList.remove('hidden'); }
async function initialize(){
  try{const s=await api('/api/session');if(s.authenticated){csrf=s.csrf;showApp();await refresh();}else{showLogin();}}catch{showLogin();}
  setInterval(()=>{if(!$('application').classList.contains('hidden')) refresh().catch(()=>{});},15000);
}
async function refresh(){if(refreshPending)return;refreshPending=true;try{state=await api('/api/state');render();$('side-update').textContent=`Updated ${fmtTime(state.time)}`;}catch(e){if(e.message!=='Login required')toast('Refresh failed: '+e.message,true);}finally{refreshPending=false;}}
function go(view){currentView=view;for(const v of ['overview','watches','activity','settings'])$('view-'+v).classList.toggle('hidden',v!==view);document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));$('page-title').textContent=({overview:'Overview',watches:'Watchlist',activity:'Activity',settings:'Settings'})[view];render();if(view==='settings')loadSettings().catch(handleError);}
function render(){if(!state)return;const c=state.counts;$('stat-active').textContent=c.active;$('stat-found').textContent=c.found;$('stat-queued').textContent=c.queued;$('stat-completed').textContent=c.completed;$('nav-count').textContent=state.watches.filter(w=>!isCompletedWatch(w)).length;renderOverview();renderWatches();renderActivity();renderSettings();}
function empty(text){return `<div class="empty"><div class="empty-symbol">◇</div>${esc(text)}</div>`;}
function renderOverview(){
  const ongoing=state.watches.filter(w=>!isCompletedWatch(w));
  $('overview-watches').innerHTML=ongoing.length?ongoing.slice(0,6).map(w=>`<div class="watch-row"><div class="row-icon">◎</div><div class="row-main"><strong>${esc(w.name)}</strong><small>${esc(w.query)}</small></div><span class="pill ${watchStatus(w)[1]}">${watchStatus(w)[0]}</span></div>`).join(''):empty(state.watches.length?'All watches have completed. View them in Watchlist → Completed.':'No watches yet. Add one to start monitoring.');
  $('overview-events').innerHTML=state.events.length?state.events.slice(0,7).map(eventHtml).join(''):empty('No activity yet.');
}
function eventHtml(e){const sym=({found:'✳',queued:'⇣',completed:'✓',assumed_completed:'?',failed_download:'!',error:'!',warning:'!',auth:'♙',search:'◎',config:'⚙',dismissed:'×'})[e.type]||'·';return `<div class="event-row"><div class="event-bullet ${e.type==='error'?'error':''}">${sym}</div><div class="event-text"><div>${esc(e.message)}</div><small>${esc(e.type)} &nbsp; · &nbsp; ${fmtTime(e.created_at)}</small></div></div>`;}
function watchCard(w){return `
  <article class="panel watch-card"><div class="watch-card-top"><div><h3>${esc(w.name)}</h3><div class="query">${esc(w.query)}</div></div><span class="pill ${watchStatus(w)[1]}">${watchStatus(w)[0]}</span></div>
  <div class="meta"><span>${w.mode==='auto'?'⇣ Auto-download':'✧ Notify only'}</span><span>↻ ${w.poll_minutes?esc(w.poll_minutes)+' min':'Global interval'}</span><span>${w.max_grabs===0?'Unlimited grabs':esc(w.grab_count)+' / '+esc(w.max_grabs)+' grabs'}</span>${w.sab_category?'<span>Category: '+esc(w.sab_category)+'</span>':''}</div>
  <div class="last">Last check: ${fmtTime(w.last_checked)} &nbsp;·&nbsp; Next check: ${isCompletedWatch(w)?'Completed':w.enabled?fmtTime(w.next_run):'Paused'}</div>${w.last_error?'<div class="last err">⚠ '+esc(w.last_error)+'</div>':''}
  <div class="card-actions"><button class="btn secondary tiny" data-check="${w.id}" ${!w.enabled?'disabled':''}>↻ Check now</button><button class="btn secondary tiny" data-edit="${w.id}">Edit</button><button class="btn danger tiny" data-delete="${w.id}">Delete</button></div></article>`;}
function renderWatches(){
  const filter=$('filter-watches').value.trim().toLowerCase();
  const watches=state.watches.filter(w=>(w.name+' '+w.query).toLowerCase().includes(filter));
  const ongoing=watches.filter(w=>!isCompletedWatch(w));
  const completed=watches.filter(isCompletedWatch);
  // Set a class, not inline styles, to preserve the responsive one-column mobile layout.
  const cols=state.preferences?.watchlist_columns===1?1:2;
  for(const id of ['watchlist','completed-watchlist']){
    const grid=$(id);
    grid.classList.toggle('one-column',cols===1);
    grid.classList.toggle('two-column',cols===2);
  }
  $('active-watch-count').textContent=ongoing.length;
  $('completed-watch-count').textContent=completed.length;
  $('watchlist').innerHTML=ongoing.length?ongoing.map(watchCard).join(''):empty(filter?'No active or paused watches match this filter.':'No active or paused watches. Add a watch to start monitoring.');
  $('completed-watchlist').innerHTML=completed.length?completed.map(watchCard).join(''):empty(filter?'No completed watches match this filter.':'No completed watches yet.');
}
function canDismissOrphan(m){return (m.status==='queued' && !!m.nzo_id && !!m.sab_missing_since && (m.note||'').startsWith('SAB status unverified:')) || (m.status==='assumed_completed' && (m.note||'').startsWith('Completion assumed:'));}
function renderActivity(){const status=$('status-filter').value;const matches=state.matches.filter(m=>!status||m.status===status);$('results-body').innerHTML=matches.length?matches.map(m=>`<tr><td>${esc(m.title)}${m.note?'<div class="muted-sub">'+esc(m.note)+'</div>':''}${m.nzo_id?'<div class="muted-sub">'+esc(m.nzo_id)+'</div>':''}</td><td>${esc(m.watch_name)}</td><td>${fmtSize(m.size_bytes)}${m.resolution?'<div class="muted-sub">'+esc(m.resolution)+'</div>':''}</td><td><span class="pill ${esc(m.status)}">${esc(formatStatus(m.status))}</span></td><td>${fmtTime(m.found_at)}</td><td>${m.status==='found'||m.status==='failed'?`<button class="btn secondary tiny" data-queue="${m.id}">Queue</button>`:canDismissOrphan(m)?`<button type="button" class="btn danger tiny orphan-dismiss" data-dismiss-match="${m.id}" aria-label="Dismiss orphaned download ${esc(m.title)}" title="Remove from NZBWatch Activity (does not delete from SABnzbd)">×</button>`:''}</td></tr>`).join(''):`<tr><td colspan="6">${empty('No matching results yet.')}</td></tr>`;$('event-log').innerHTML=state.events.length?state.events.map(eventHtml).join(''):empty('No events yet.');}
function renderSettings(){for(const provider of ['hydra','sab','pushover']){const status=$('service-'+provider).querySelector('.service-status');status.innerHTML=state.integrations[provider]?'<span class="live-dot"></span> Credentials configured':'<span style="color:#ffabac">●</span> Not configured';}$('notification-types').textContent='Active notifications: '+(state.integrations.notify_events.join(', ')||'none');}
function openWatch(w=null){$('watch-form').reset();$('watch-id').value=w?.id||'';$('modal-title').textContent=w?'Edit watch':'New watch';const defaults={name:'',query:'',enabled:1,mode:'auto',match_mode:'all',required_words:'',excluded_words:'',categories:'',sab_category:'',min_mb:'',max_mb:'',max_age_days:'',max_grabs:1,poll_minutes:0,broad_search:true,resolution_mode:'inherit'};const data={...defaults,...(w||{})};for(const [key,val] of Object.entries(data)){const field=$('watch-form').elements.namedItem(key);if(!field)continue;if(field.type==='checkbox')field.checked=Boolean(val);else field.value=val===null?'':val;}$('watch-modal').classList.remove('hidden');document.body.style.overflow='hidden';$('watch-name').focus();updatePreview();}
function closeWatch(){$('watch-modal').classList.add('hidden');document.body.style.overflow='';}
function watchPayload(){const f=$('watch-form');const obj={};for(const name of ['name','query','mode','match_mode','required_words','excluded_words','categories','sab_category','resolution_mode'])obj[name]=f.elements.namedItem(name).value.trim();for(const name of ['min_mb','max_mb','max_age_days']){const str=f.elements.namedItem(name).value;obj[name]=str===''?null:Number(str);}for(const name of ['max_grabs','poll_minutes'])obj[name]=Number(f.elements.namedItem(name).value);obj.enabled=$('watch-enabled').checked;obj.broad_search=$('watch-broad').checked;return obj;}
const secretKeys=['hydra_key','sab_key','pushover_key','pushover_user'];
async function loadSettings(){
  settingsData=await api('/api/settings');
  const f=$('settings-form');
  for(const [key,val] of Object.entries(settingsData)){
    const el=f.elements.namedItem(key);if(!el)continue;
    if(el.type==='checkbox')el.checked=Boolean(val);
    else if(key==='resolution_order')el.value=val.join(',');
    else el.value=val;
  }
  for(const key of secretKeys){
    const el=f.elements.namedItem(key);el.value='';
    f.elements.namedItem('clear_'+key).checked=false;
    el.placeholder=settingsData.configured_secrets[key]?'Saved — blank keeps existing':'Not configured';
  }
  document.querySelectorAll('#notify-checkboxes input').forEach(el=>el.checked=settingsData.notification_types.includes(el.value));
}
function settingsPayload(){
  const f=$('settings-form');
  const o={};
  for(const k of ['hydra_url','sab_url','fallback_min_resolution','sab_missing_policy'])o[k]=f.elements.namedItem(k).value.trim();
  o.watchlist_columns=Number(f.elements.namedItem('watchlist_columns').value);
  for(const k of ['scan_interval_minutes','max_queues_per_scan','sab_missing_grace_minutes'])o[k]=Number(f.elements.namedItem(k).value);
  for(const k of ['clean_search_terms','strip_release_groups','resolution_enabled','allow_unknown_resolution','dedupe_across_resolutions'])o[k]=f.elements.namedItem(k).checked;
  o.resolution_order=f.elements.namedItem('resolution_order').value.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean);
  o.notification_types=[...document.querySelectorAll('#notify-checkboxes input:checked')].map(x=>x.value);
  for(const k of secretKeys){
    const value=f.elements.namedItem(k).value.trim();
    o[k]=f.elements.namedItem('clear_'+k).checked?'':(value||null);
  }
  return o;
}
async function updatePreview(){
  const id=++previewRequest;
  const query=$('watch-query').value;
  if(!query.trim()){$('query-preview').textContent='';return;}
  try {
    const r=await api('/api/query-preview?q='+encodeURIComponent(query));
    if(id!==previewRequest)return;
    $('query-preview').textContent='Global broad-search preview: '+r.search;
  }catch{}
}

function handleError(e){toast(e.message||String(e),true);}
window.addEventListener('DOMContentLoaded',()=>{
  $('login-form').addEventListener('submit',async e=>{e.preventDefault();$('login-error').textContent='';try{const r=await api('/api/login',{method:'POST',body:{password:$('password').value}});csrf=r.csrf;showApp();go('overview');await refresh();}catch(ex){$('login-error').textContent=ex.message;}});
  $('logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});}catch{}csrf='';showLogin();});
  document.querySelectorAll('[data-view]').forEach(x=>x.addEventListener('click',()=>go(x.dataset.view)));
  document.querySelectorAll('[data-goto]').forEach(x=>x.addEventListener('click',()=>go(x.dataset.goto)));
  $('add-watch').addEventListener('click',()=>openWatch());$('close-modal').addEventListener('click',closeWatch);$('cancel-modal').addEventListener('click',closeWatch);
  $('watch-modal').addEventListener('click',e=>{if(e.target===$('watch-modal'))closeWatch();});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('watch-modal').classList.contains('hidden'))closeWatch();});
  $('watch-form').addEventListener('submit',async e=>{e.preventDefault();const id=$('watch-id').value;const btn=$('save-watch');btn.disabled=true;try{await api(id?`/api/watches/${id}`:'/api/watches',{method:id?'PUT':'POST',body:watchPayload()});closeWatch();toast(id?'Watch updated':'Watch created');await refresh();}catch(ex){handleError(ex)}finally{btn.disabled=false;}});
  $('watch-query').addEventListener('input',updatePreview);$('filter-watches').addEventListener('input',renderWatches);$('status-filter').addEventListener('change',renderActivity);
  $('view-watches').addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;const id=Number(b.dataset.edit||b.dataset.delete||b.dataset.check);if(b.dataset.edit){const w=state.watches.find(x=>x.id===id);if(w)openWatch(w);}else if(b.dataset.delete){const w=state.watches.find(x=>x.id===id);if(!confirm(`Delete “${w?.name||id}” and its saved matches? This cannot be undone.`))return;try{await api(`/api/watches/${id}`,{method:'DELETE'});toast('Watch deleted');await refresh();}catch(ex){handleError(ex);}}else if(b.dataset.check){try{await api(`/api/watches/${id}/check`,{method:'POST'});toast('Scan started. Results will appear in Activity.');}catch(ex){handleError(ex);}}});
  $('results-body').addEventListener('click',async e=>{
    const dismissButton=e.target.closest('[data-dismiss-match]');
    if(dismissButton){
      const id=Number(dismissButton.dataset.dismissMatch);
      const match=state.matches.find(m=>m.id===id);
      if(!match||!canDismissOrphan(match))return;
      if(!confirm(`Remove “${match.title}” from NZBWatch Activity?\n\nThis will NOT cancel or delete anything in SABnzbd. NZBWatch will retain its duplicate protection and watch grab count.`))return;
      dismissButton.disabled=true;
      try{await api(`/api/matches/${id}`,{method:'DELETE'});toast('Orphaned download removed from Activity');await refresh();}
      catch(ex){handleError(ex);dismissButton.disabled=false;}
      return;
    }
    const id=e.target.closest('[data-queue]')?.dataset.queue;
    if(!id)return;
    try{const r=await api(`/api/matches/${id}/queue`,{method:'POST'});toast('Queue result: '+r.result);await refresh();}catch(ex){handleError(ex);}
  });
  $('check-all').addEventListener('click',async()=>{try{const r=await api('/api/check-all',{method:'POST'});toast(`Scheduled ${r.count} watch checks`);}catch(ex){handleError(ex);}});
  document.querySelectorAll('[data-test]').forEach(b=>b.addEventListener('click',async()=>{b.disabled=true;try{const r=await api('/api/test/'+b.dataset.test,{method:'POST'});toast(r.message);}catch(ex){handleError(ex)}finally{b.disabled=false;}}));
  $('settings-form').addEventListener('submit',async e=>{
    e.preventDefault();
    const btn=$('save-settings');btn.disabled=true;
    try{await api('/api/settings',{method:'PUT',body:settingsPayload()});toast('Settings saved and applied without restart');await loadSettings();await refresh();}
    catch(ex){handleError(ex)}finally{btn.disabled=false;}
  });
  $('export-json').addEventListener('click',async()=>{try{const res=await fetch('/api/export',{credentials:'same-origin'});if(!res.ok)throw new Error('Export failed');const blob=await res.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='nzbwatch-watchlist.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1500);}catch(ex){handleError(ex);}});
  $('import-json').addEventListener('click',()=>$('import-file').click());$('import-file').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;try{const json=JSON.parse(await file.text());const r=await api('/api/import',{method:'POST',body:json});toast(`Imported ${r.added} watches`);await refresh();}catch(ex){handleError(ex)}finally{e.target.value='';}});
  initialize();
});
