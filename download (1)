'use strict';
let csrf = '', currentView = 'overview', state = null, refreshPending = false, toastTimer = null;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = s => { if (!s) return 'Never'; const d = new Date(s); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); };
const fmtSize = n => n ? (n / 1048576 >= 1024 ? (n/1073741824).toFixed(2)+' GiB' : (n/1048576).toFixed(0)+' MiB') : '—';
const watchStatus = w => w.enabled ? ['ACTIVE',''] : w.max_grabs>0 && w.grab_count>=w.max_grabs ? ['COMPLETE','completed'] : ['PAUSED','paused'];
const formatStatus = s => ({found:'Found',queued:'Queued',completed:'Completed',failed:'Queue failed',failed_download:'Download failed',skipped:'Duplicate'}[s] || s);
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
function go(view){currentView=view;for(const v of ['overview','watches','activity','settings'])$('view-'+v).classList.toggle('hidden',v!==view);document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));$('page-title').textContent=({overview:'Overview',watches:'Watchlist',activity:'Activity',settings:'Connections'})[view];render();}
function render(){if(!state)return;const c=state.counts;$('stat-active').textContent=c.active;$('stat-found').textContent=c.found;$('stat-queued').textContent=c.queued;$('stat-completed').textContent=c.completed;$('nav-count').textContent=c.watches;renderOverview();renderWatches();renderActivity();renderSettings();}
function empty(text){return `<div class="empty"><div class="empty-symbol">◇</div>${esc(text)}</div>`;}
function renderOverview(){
  $('overview-watches').innerHTML=state.watches.length?state.watches.slice(0,6).map(w=>`<div class="watch-row"><div class="row-icon">◎</div><div class="row-main"><strong>${esc(w.name)}</strong><small>${esc(w.query)}</small></div><span class="pill ${watchStatus(w)[1]}">${watchStatus(w)[0]}</span></div>`).join(''):empty('No watches yet. Add one to start monitoring.');
  $('overview-events').innerHTML=state.events.length?state.events.slice(0,7).map(eventHtml).join(''):empty('No activity yet.');
}
function eventHtml(e){const sym=({found:'✳',queued:'⇣',completed:'✓',failed_download:'!',error:'!',warning:'!',auth:'♙',search:'◎',config:'⚙'})[e.type]||'·';return `<div class="event-row"><div class="event-bullet ${e.type==='error'?'error':''}">${sym}</div><div class="event-text"><div>${esc(e.message)}</div><small>${esc(e.type)} &nbsp; · &nbsp; ${fmtTime(e.created_at)}</small></div></div>`;}
function renderWatches(){const filter=$('filter-watches').value.toLowerCase();const watches=state.watches.filter(w=>(w.name+' '+w.query).toLowerCase().includes(filter));$('watchlist').innerHTML=watches.length?watches.map(w=>`
  <article class="panel watch-card"><div class="watch-card-top"><div><h3>${esc(w.name)}</h3><div class="query">${esc(w.query)}</div></div><span class="pill ${watchStatus(w)[1]}">${watchStatus(w)[0]}</span></div>
  <div class="meta"><span>${w.mode==='auto'?'⇣ Auto-download':'✧ Notify only'}</span><span>↻ ${esc(w.poll_minutes)} min</span><span>${w.max_grabs===0?'Unlimited grabs':esc(w.grab_count)+' / '+esc(w.max_grabs)+' grabs'}</span>${w.sab_category?'<span>Category: '+esc(w.sab_category)+'</span>':''}</div>
  <div class="last">Last check: ${fmtTime(w.last_checked)} &nbsp;·&nbsp; Next check: ${w.enabled?fmtTime(w.next_run):'Paused'}</div>${w.last_error?'<div class="last err">⚠ '+esc(w.last_error)+'</div>':''}
  <div class="card-actions"><button class="btn secondary tiny" data-check="${w.id}" ${!w.enabled?'disabled':''}>↻ Check now</button><button class="btn secondary tiny" data-edit="${w.id}">Edit</button><button class="btn danger tiny" data-delete="${w.id}">Delete</button></div></article>`).join(''):empty(filter?'No watches match that filter.':'No watches configured yet. Use “Add watch” to get started.');}
function renderActivity(){const status=$('status-filter').value;const matches=state.matches.filter(m=>!status||m.status===status);$('results-body').innerHTML=matches.length?matches.map(m=>`<tr><td>${esc(m.title)}${m.note?'<div class="muted-sub">'+esc(m.note)+'</div>':''}${m.nzo_id?'<div class="muted-sub">'+esc(m.nzo_id)+'</div>':''}</td><td>${esc(m.watch_name)}</td><td>${fmtSize(m.size_bytes)}</td><td><span class="pill ${esc(m.status)}">${esc(formatStatus(m.status))}</span></td><td>${fmtTime(m.found_at)}</td><td>${m.status==='found'||m.status==='failed'?`<button class="btn secondary tiny" data-queue="${m.id}">Queue</button>`:''}</td></tr>`).join(''):`<tr><td colspan="6">${empty('No matching results yet.')}</td></tr>`;$('event-log').innerHTML=state.events.length?state.events.map(eventHtml).join(''):empty('No events yet.');}
function renderSettings(){for(const provider of ['hydra','sab','pushover']){const status=$('service-'+provider).querySelector('.service-status');status.innerHTML=state.integrations[provider]?'<span class="live-dot"></span> Credentials configured':'<span style="color:#ffabac">●</span> Not configured';}$('notification-types').textContent='Configured notification events: '+(state.integrations.notify_events.join(', ')||'none');}
function openWatch(w=null){$('watch-form').reset();$('watch-id').value=w?.id||'';$('modal-title').textContent=w?'Edit watch':'New watch';const defaults={name:'',query:'',enabled:1,mode:'auto',match_mode:'all',required_words:'',excluded_words:'',categories:'',sab_category:'',min_mb:'',max_mb:'',max_age_days:'',max_grabs:1,poll_minutes:30};const data={...defaults,...(w||{})};for(const [key,val] of Object.entries(data)){const field=$('watch-form').elements.namedItem(key);if(!field)continue;if(field.type==='checkbox')field.checked=Boolean(val);else field.value=val===null?'':val;}$('watch-modal').classList.remove('hidden');document.body.style.overflow='hidden';$('watch-name').focus();}
function closeWatch(){$('watch-modal').classList.add('hidden');document.body.style.overflow='';}
function watchPayload(){const f=$('watch-form');const obj={};for(const name of ['name','query','mode','match_mode','required_words','excluded_words','categories','sab_category'])obj[name]=f.elements.namedItem(name).value.trim();for(const name of ['min_mb','max_mb','max_age_days']){const str=f.elements.namedItem(name).value;obj[name]=str===''?null:Number(str);}for(const name of ['max_grabs','poll_minutes'])obj[name]=Number(f.elements.namedItem(name).value);obj.enabled=$('watch-enabled').checked;return obj;}
function handleError(e){toast(e.message||String(e),true);}
window.addEventListener('DOMContentLoaded',()=>{
  $('login-form').addEventListener('submit',async e=>{e.preventDefault();$('login-error').textContent='';try{const r=await api('/api/login',{method:'POST',body:{password:$('password').value}});csrf=r.csrf;showApp();go('overview');await refresh();}catch(ex){$('login-error').textContent=ex.message;}});
  $('logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});}catch{}csrf='';showLogin();});
  document.querySelectorAll('[data-view]').forEach(x=>x.addEventListener('click',()=>go(x.dataset.view)));
  document.querySelectorAll('[data-goto]').forEach(x=>x.addEventListener('click',()=>go(x.dataset.goto)));
  $('add-watch').addEventListener('click',()=>openWatch());$('close-modal').addEventListener('click',closeWatch);$('cancel-modal').addEventListener('click',closeWatch);
  $('watch-modal').addEventListener('click',e=>{if(e.target===$('watch-modal'))closeWatch();});document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('watch-modal').classList.contains('hidden'))closeWatch();});
  $('watch-form').addEventListener('submit',async e=>{e.preventDefault();const id=$('watch-id').value;const btn=$('save-watch');btn.disabled=true;try{await api(id?`/api/watches/${id}`:'/api/watches',{method:id?'PUT':'POST',body:watchPayload()});closeWatch();toast(id?'Watch updated':'Watch created');await refresh();}catch(ex){handleError(ex)}finally{btn.disabled=false;}});
  $('filter-watches').addEventListener('input',renderWatches);$('status-filter').addEventListener('change',renderActivity);
  $('watchlist').addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;const id=Number(b.dataset.edit||b.dataset.delete||b.dataset.check);if(b.dataset.edit){const w=state.watches.find(x=>x.id===id);if(w)openWatch(w);}else if(b.dataset.delete){const w=state.watches.find(x=>x.id===id);if(!confirm(`Delete “${w?.name||id}” and its saved matches? This cannot be undone.`))return;try{await api(`/api/watches/${id}`,{method:'DELETE'});toast('Watch deleted');await refresh();}catch(ex){handleError(ex);}}else if(b.dataset.check){try{await api(`/api/watches/${id}/check`,{method:'POST'});toast('Scan started. Results will appear in Activity.');}catch(ex){handleError(ex);}}});
  $('results-body').addEventListener('click',async e=>{const id=e.target.closest('[data-queue]')?.dataset.queue;if(!id)return;try{const r=await api(`/api/matches/${id}/queue`,{method:'POST'});toast('Queue result: '+r.result);await refresh();}catch(ex){handleError(ex);}});
  $('check-all').addEventListener('click',async()=>{try{const r=await api('/api/check-all',{method:'POST'});toast(`Scheduled ${r.count} watch checks`);}catch(ex){handleError(ex);}});
  document.querySelectorAll('[data-test]').forEach(b=>b.addEventListener('click',async()=>{b.disabled=true;try{const r=await api('/api/test/'+b.dataset.test,{method:'POST'});toast(r.message);}catch(ex){handleError(ex)}finally{b.disabled=false;}}));
  $('export-json').addEventListener('click',async()=>{try{const res=await fetch('/api/export',{credentials:'same-origin'});if(!res.ok)throw new Error('Export failed');const blob=await res.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='nzbwatch-watchlist.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1500);}catch(ex){handleError(ex);}});
  $('import-json').addEventListener('click',()=>$('import-file').click());$('import-file').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;try{const json=JSON.parse(await file.text());const r=await api('/api/import',{method:'POST',body:json});toast(`Imported ${r.added} watches`);await refresh();}catch(ex){handleError(ex)}finally{e.target.value='';}});
  initialize();
});
