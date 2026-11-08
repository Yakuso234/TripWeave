const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => value == null ? '待确认' : '¥'+Number(value).toLocaleString('zh-CN');
const S = {page:'overview',step:1,day:1,config:null,draft:null,trip:null,candidate:null,hotels:[],hotelContext:null,recommended:null,history:[],planHistory:[],historyOpen:false,busy:false,error:null,mode:'basic'};
let toastTimer;
function toast(text){$('toast').textContent=text;$('toast').className='show';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').className='',5500);}
async function api(path, method = 'GET', body) {
  let response;
  try {
    response = await fetch('/api' + path, {method, headers:{'Content-Type':'application/json'},
      body:body === undefined ? undefined : JSON.stringify(body)});
  } catch { throw Error('无法连接本地服务，请确认服务仍在运行后重试。'); }
  let data;
  try { data = await response.json(); }
  catch { throw Error('服务未返回有效结果，请稍后重试。'); }
  if (!response.ok) {
    const error = Error(typeof data.detail === 'string' ? data.detail
      : data.detail?.[0]?.msg || '请求未完成，请检查输入');
    error.status = response.status;
    throw error;
  }
  return data;
}
const button=(label,action,kind='',extra='')=>`<button class="btn ${kind}" data-action="${action}" ${extra} ${S.busy?'disabled':''}>${label}</button>`;
const tag=(text,warn=false)=>`<span class="tag ${warn?'warn':''}">${esc(text)}</span>`;
function field(label,key,type='text',help='',attrs=''){if(['city','date','origin_query','start_time','end_time'].includes(key))attrs+=' required';return `<label class="field">${label}<input data-field="${key}" type="${type}" value="${esc(S.draft[key])}" ${attrs}>${help?`<small>${help}</small>`:''}</label>`;}
function choices(key,items){return `<div class="choice-group">${items.map(([value,label,sub])=>`<button class="choice ${sub?'wide':''} ${String(S.draft[key])===String(value)?'active':''}" aria-pressed="${String(S.draft[key])===String(value)}" data-field-choice="${key}" data-value="${esc(value)}">${label}${sub?`<small>${sub}</small>`:''}</button>`).join('')}</div>`;}
function shownPlan(){const p=S.candidate||S.trip?.accepted_plan;return p&&['basic','deepseek'].includes(p.mode)?p:null;}
function dirty(){return S.trip && Object.entries(S.draft).some(([k,v])=>JSON.stringify(S.trip.requirements[k]??S.config.defaults[k])!==JSON.stringify(v));}
// A plan keeps its own input snapshot; the editable draft is never its source of truth.
function planRequirements(plan = shownPlan()) {
  if (!plan) return null;
  if (plan.requirements) return plan.requirements;
  if (plan.requirements_revision === S.trip?.requirements_revision) return S.trip.requirements;
  return null;
}
function stalePlan(plan = shownPlan()) {
  return !!plan && (dirty() || plan.requirements_revision !== S.trip?.requirements_revision
    || (plan.id !== S.trip?.accepted_plan?.id && plan.base_version !== S.trip?.accepted_version));
}
function canAccept(plan = shownPlan()) {
  return !!plan && plan.status === 'ready' && !stalePlan(plan)
    && plan.trip_id === S.trip?.id && plan.base_version === S.trip?.accepted_version;
}
function hotelQueryContext(req) {
  return JSON.stringify(['city', 'origin_query', 'booking_query', 'hotel_preference', 'days']
    .map(key => req?.[key]));
}
function currentHotels() {
  const context = hotelQueryContext(S.draft);
  if (S.hotelContext === context) return S.hotels;
  const plan = shownPlan(), req = planRequirements(plan);
  return req && hotelQueryContext(req) === context ? plan.hotel_options || [] : [];
}
function exportPayload(plan = shownPlan()) {
  return {requirements: planRequirements(plan), candidate: plan,
    note: planRequirements(plan) ? '需求为该方案生成时的快照。' : '旧方案未保存原始需求快照，不能用当前草稿补填。'};
}
function daysLabel(req = S.draft){return req.days+'天'+Math.max(0,req.days-1)+'晚';}
function dateFor(day, req = S.draft) {
  if (!req.date) return '日期待确认';
  const date = new Date(req.date + 'T12:00:00+08:00');
  date.setUTCDate(date.getUTCDate() + day - 1);
  return date.toLocaleDateString('zh-CN', {timeZone:'Asia/Shanghai', month:'long', day:'numeric'});
}
function stopHeading(stop, place) {
  if (!place.name) return '未核实地点 · ' + stop.place_id;
  if (stop.kind === 'meal') {
    return String(place.type || '').startsWith('餐饮')
      ? '用餐 · ' + place.name : place.name + '附近 · 自选用餐';
  }
  return ({booking:'固定预约 · ', hotel:'住宿 · '}[stop.kind] || '') + place.name;
}
function displayTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未记录'
    : date.toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false});
}
function planningRecords() {
  if (!S.trip) return '';
  const current = shownPlan();
  return `<section class="card planning-records"><div class="section-title"><div><h3>规划记录</h3><p class="muted small">保留每次候选与失败结果，可回看；当前接受版本 v${S.trip.accepted_version}。</p></div><div class="actions">${S.trip.accepted_plan&&['basic','deepseek'].includes(S.trip.accepted_plan.mode)&&current?.id!==S.trip.accepted_plan.id?button('查看已接受方案','show-accepted','secondary tiny'):''}${button(S.historyOpen?'收起记录':'查看规划记录', 'history', 'secondary tiny')}</div></div>${S.historyOpen
    ? (S.planHistory.length ? S.planHistory.map(plan => `<div class="history-row"><div><strong>${esc(plan.requirements?.city||'旅行方案')} · ${plan.mode==='deepseek'?'DeepSeek':'基础排程'}</strong><p>${esc(displayTime(plan.created_at))} · 需求 v${esc(plan.requirements_revision)}</p>${tag(plan.status==='ready'?'检查通过':plan.error?'规划失败':'存在冲突',plan.status!=='ready')} ${tag(plan.id===S.trip.accepted_plan?.id?'当前已接受':stalePlan(plan)?'已过时':'候选')}${plan.error?`<p class="danger">${esc(plan.error)}</p>`:''}</div>${button(current?.id===plan.id?'正在查看':'查看此记录', 'show-candidate', 'secondary tiny', `data-id="${esc(plan.id)}"`)}</div>`).join('')
      : '<p class="muted small">尚无规划记录；生成的候选和失败信息会保存在这里。</p>') : ''}</section>`;
}
async function loadPlanHistory(restoreLatest = false) {
  if (!S.trip) { S.planHistory = []; return; }
  const history = await api(`/trips/${S.trip.id}/history`);
  S.planHistory = (history.candidates || []).filter(plan => ['basic','deepseek'].includes(plan.mode));
  if (restoreLatest) { S.candidate = S.planHistory[0] || null; S.day = 1; }
}
function missingNotice(){return !S.config.map_configured?`<div class="notice">先连接高德 Web 服务，即可用真实地点与路线进行基础规划；连接 DeepSeek 后可测试自然语言需求和智能修订。${button('查看接入步骤','settings','tiny secondary')}</div>`:'';}
function heroArt(){return `<div class="hero-art" aria-hidden="true"><svg viewBox="0 0 400 230" fill="none"><circle cx="310" cy="48" r="28" fill="#f5e4b8"/><path d="M10 204H390" stroke="#72927b"/><path d="M44 204V130H88V204M105 204V94H147V204M172 204V116H218V204M244 204V70H279V204M302 204V119H345V204" fill="#78967f"/><path d="M259 70V26M251 41H267M191 116V66M181 81H201" stroke="#50785f" stroke-width="3"/><path d="M14 219C80 170 120 236 180 213S300 193 389 216" stroke="#e3ede1" stroke-width="6"/><path d="M33 128C95 34 206 77 330 164" stroke="#f9f9ef" stroke-dasharray="5 7" stroke-width="2"/><circle cx="33" cy="128" r="6" fill="#f7f7ee"/><circle cx="330" cy="164" r="6" fill="#f7f7ee"/></svg><span class="hero-note">A LITTLE PLAN. A BETTER JOURNEY.</span></div>`;}
function overview(){const d=S.draft,p=shownPlan();return `<section class="hero"><div class="hero-copy"><p class="eyebrow">YOUR NEXT CHAPTER / 下一次出发</p><h1>把${esc(d.city)}的${d.days}天，<br>安排成喜欢的样子。</h1><p class="subtle">从住宿片区到每日路线，把人文、风味和留白织进行程。你可以随时改变想法，让计划跟上变化。</p><div class="actions">${button('开始完善旅行偏好 ↗','preferences')}${button(p?'查看当前行程':'查看规划工作台','itinerary','ghost')}</div></div>${heroArt()}</section><div class="metrics"><div class="metric"><span>旅行时长</span><strong>${daysLabel()}</strong><small>${esc(d.date)}</small></div><div class="metric"><span>同行人数</span><strong>${d.party_size}</strong><small>位旅行者</small></div><div class="metric"><span>整趟预算</span><strong>${money(d.budget_total)}</strong><small>全体人数</small></div><div class="metric"><span>当前规划方式</span><strong style="font-size:18px">${S.mode==='deepseek'?'DeepSeek':'基础排程'}</strong><small>${S.config.map_configured?'真实高德地图':'等待地图接入'}</small></div></div>${missingNotice()}<div class="section-title"><h2>一趟旅行，慢慢成形</h2><span class="muted small">三步，把想法变成可检查的安排</span></div><div class="grid3">${[['01','先说说你想怎么旅行','日期、节奏、预算和不能错过的预约，都从这里开始。','preferences','填写旅行偏好'],['02','找到合适的落脚点','依据真实酒店位置推荐片区与备选，房价与房量保持待确认。','hotels','查看住宿方案'],['03','让每天都有自己的节奏','真实路线核对交通，按天查看景点、餐饮、住宿与待办事项。','itinerary','进入每日安排']].map(([n,title,text,target,label])=>`<article class="card"><span class="step-num">${n}</span><h3>${title}</h3><p class="muted small">${text}</p>${button(label+' →',target,'secondary')}</article>`).join('')}</div><div class="section-title"><h2>最近的行程</h2>${button('＋ 新建旅行','new','secondary tiny')}</div><section class="card">${S.history.length?S.history.slice(0,5).map(t=>`<div class="history-row"><div><strong>${esc(t.requirements.city)} · ${t.requirements.days||1}日旅行</strong><p>${esc(t.requirements.date)} · 已接受版本 ${t.accepted_version}</p></div>${button('继续查看 →','load','secondary tiny',`data-id="${esc(t.id)}"`)}</div>`).join(''):'<p class="muted small">这里会保存你创建过的旅行。先从一份偏好开始吧。</p>'}</section>`;}
function preferences(){const d=S.draft;let content='';if(S.step===1){content=`<p class="eyebrow">01 / 出发的轮廓</p><h2>先决定，去哪里待多久。</h2><p class="muted small">支持国内城市地点查询；请填写明确的出发地点或酒店名称。</p><div class="choice-group">${['上海','杭州','苏州','南京','成都','北京'].map(city=>`<button class="choice ${d.city===city?'active':''}" aria-pressed="${d.city===city}" data-city="${city}">${city}</button>`).join('')}</div><div class="field-grid">${field('目的地城市','city')}${field('出行日期','date','date')}</div><label class="field">旅行天数</label>${choices('days',[[1,'1天','当日往返'],[2,'2天1晚','周末短途'],[3,'3天2晚','多留一点时间']])}<div class="field-grid">${field('同行人数','party_size','number','房间暂按每间2人预留，实际入住政策待核实。','min="1" max="6"')}${field('整趟预算（全体人数 / 元）','budget_total','number','不含往返大交通和购物。','min="100" max="100000"')}</div>${field('第一天从哪里出发','origin_query','text','例如：上海站、人民广场，或你已知的酒店名称。')}`;}
if(S.step===2){const tags=['城市漫步','历史人文','自然公园','室内展览','美食探索','江景夜色'];content=`<p class="eyebrow">02 / 旅行的温度</p><h2>把节奏留给自己。</h2><label class="field">你更在意什么？</label><div class="choice-group">${tags.map(t=>`<button class="choice ${d.interests.includes(t)?'active':''}" aria-pressed="${d.interests.includes(t)}" data-interest="${t}">${t}</button>`).join('')}</div><label class="field">一天的节奏</label>${choices('pace',[['relaxed','悠闲慢游','单站停留更久'],['balanced','刚刚好','体验与留白兼顾'],['full','充实探索','增加城市体验']])}<label class="field">市内交通</label>${choices('transport',[['mixed','步行 + 公共交通','较远路段查询公共交通'],['walking','尽量步行','用步行路线核对时间']])}<div class="field-grid">${field('每天最早出发','start_time','time')}${field('每天最晚结束','end_time','time')}</div><label class="field">每日最多景点 / 固定活动</label>${choices('max_activities',[[2,'2项'],[3,'3项'],[4,'4项'],[5,'5项']])}`;}
if(S.step===3){content=`<p class="eyebrow">03 / 安心的边界</p><h2>保留那些已经确定的事。</h2><div class="field-grid">${field('住宿上限（每间每晚 / 元）','hotel_budget','number','仅为预算预留，不当作真实报价。','min="100" max="5000"')}${field('预约在第几天','booking_day','number','','min="1" max="'+d.days+'"')}</div><label class="field">住宿取向</label>${choices('hotel_preference',[['balanced','衔接便利','靠近出发或预约片区'],['value','预算优先','房价需订房平台核实'],['riverside','体验优先','具体景观房需另行确认']])}${field('固定活动 / 预约地点','booking_query','text','没有固定预约可留空。这里只保留你的计划，不会代你预约。')}<div class="field-grid">${field('预约开始','booking_start','time')}${field('预约结束','booking_end','time')}</div><label class="field">还有哪些想法？<textarea data-field="notes" placeholder="例如：带父母出行，尽量减少长距离步行；希望第二天下午保留自由时间。">${esc(d.notes)}</textarea><small>基础规划使用明确的日期、节奏和约束字段；这段自由文本由DeepSeek模式理解，基础排程不会假装理解。</small></label>`;}
return `<div class="page-heading"><div><p class="eyebrow">TRAVEL BRIEF</p><h1>旅行偏好</h1><p class="muted">一次说清楚，后面的修改不必从头开始。</p></div>${tag('第 '+S.step+' / 3 步')}</div><div class="layout"><aside class="sidebar">${[['目的地与日期','先画出旅途轮廓'],['兴趣与节奏','决定一天怎样度过'],['预算与固定安排','把重要的事留住']].map(([a,b],i)=>`<button class="step-link ${S.step===i+1?'active':''}" aria-current="${S.step===i+1?'step':'false'}" data-step="${i+1}"><span>0${i+1}</span><div><strong>${a}</strong><small>${b}</small></div></button>`).join('')}</aside><section class="card form-card">${content}<div class="form-bottom">${S.step>1?button('← 上一步','prev','secondary'):button('返回概览','overview','secondary')}${S.step<3?button('下一步 →','next'):button('保存需求，查看住宿 →','finish')}</div></section></div>`;}
function hotelCard(h,index){return `<article class="card hotel-card ${S.draft.hotel_id===h.id?'selected':''}"><div class="hotel-art" aria-hidden="true">0${index+1}</div><div class="section-title">${tag(index===0?'位置推荐':'备选住宿')}${S.draft.hotel_id===h.id?tag('已手选'):''}</div><h3>${esc(h.name)}</h3><p class="muted small">${esc(h.area||'')} · ${esc(h.address)}</p><p class="small subtle">${esc(h.selection_reason||'按真实地点的位置衔接推荐；房价与空房待确认。')}</p><div class="price">房价待查询 <small>/ 非实时预订系统</small></div><p class="muted small">${Math.ceil(S.draft.party_size/2)}间 × ${Math.max(0,S.draft.days-1)}晚，按上限预留 ${money(S.draft.hotel_budget*Math.ceil(S.draft.party_size/2)*Math.max(0,S.draft.days-1))}</p><ul>${(h.benefits||['来自高德地点查询','需核实房型、空房和取消规则']).map(t=>`<li>${esc(t)}</li>`).join('')}</ul>${button(S.draft.hotel_id===h.id?'已选择此住宿':'选择这家住宿','hotel','secondary',`data-id="${esc(h.id)}"`)}</article>`;}
function hotels(){const hotels=currentHotels();return `<div class="page-heading"><div><p class="eyebrow">A PLACE TO RETURN TO</p><h1>给旅途，选一个落脚点。</h1><p class="muted">${esc(S.draft.city)} · ${daysLabel()} · ${S.draft.party_size}人 · 每间每晚预算 ${money(S.draft.hotel_budget)}</p></div><div class="actions">${button('调整住宿预算','constraints','secondary')}${button('查询真实住宿 ↗','query-hotels', '',!S.config.map_configured||S.draft.days===1?'disabled':'')}</div></div><div class="notice">自动推荐主要核对酒店位置与路线衔接。高德地点搜索不保证实时房价、空房、早餐或房型，预算上限仅作为预留。</div>${S.draft.days===1?`<div class="empty"><span class="empty-mark">↗</span><h2>这次当天往返</h2><p>单日行程不添加住宿费用。你可以直接进入每日安排，或把天数调整为两天一晚。</p>${button('进入每日安排','itinerary')}</div>`:hotels.length?`<div class="actions">${button('让系统自动推荐','auto-hotel','ghost')}${S.draft.hotel_id?tag('手选住宿将在重新规划时保留'):tag('自动推荐已开启')}</div><div class="grid3" style="margin-top:20px">${hotels.slice(0,6).map(hotelCard).join('')}</div><div class="actions">${button('按当前住宿生成行程 →','generate')}</div>`:`<div class="empty"><span class="empty-mark">⌂</span><h2>${S.config.map_configured?'从真实酒店候选开始':'接入地图后，查询真实酒店'}</h2><p>系统会查询目的地酒店，比较与你出发地点、固定预约的距离。这里不会放置虚构酒店或占位房价。</p>${button(S.config.map_configured?'开始查询住宿':'查看接入设置',S.config.map_configured?'query-hotels':'settings')}</div>`}`;}
function itinerary(){const p=shownPlan(),snapshot=planRequirements(p),d=p?(snapshot||{city:'历史',date:'原日期未保存',days:Math.max(1,...(p.itinerary?.stops||[]).map(s=>s.day||1)),party_size:'未记录'}):S.draft;const busy=S.busy?`<div class="busy-panel"><span class="spinner"></span>正在查询地点与路线、安排住宿和活动，请稍候…</div>`:'';const heading=`<div class="page-heading"><div><p class="eyebrow">DAY BY DAY / 一天一天，慢慢出发</p><h1>${esc(d.city)}，${daysLabel(d)}的旅行安排</h1><p class="muted">${esc(d.date)}出发 · ${d.party_size}人 · ${p?'依据需求 v'+p.requirements_revision:S.trip?'需求 v'+S.trip.requirements_revision:'尚未保存需求'}</p></div><div class="actions">${button('修改旅行偏好','preferences','secondary')}${button(p?'重新规划 ↗':'生成旅行计划 ↗','generate')}</div></div>`;
if(!p)return heading+planningRecords()+busy+`<div class="empty"><span class="empty-mark">↗</span><h2>一份属于你的行程，还差一步。</h2><p>${S.config.map_configured?'基础排程将查询真实地点与路线，安排景点、用餐与住宿；也可以在设置中切换DeepSeek。':'请先配置高德Web服务Key，系统才能按真实地点与路线安排行程。还没有Key时不会生成虚构方案。'}</p><div class="actions" style="justify-content:center">${button('查看旅行偏好','preferences','secondary')}${button(S.config.map_configured?'开始基础规划':'去接入设置',S.config.map_configured?'generate':'settings')}</div></div>`;
const day=Math.min(S.day,d.days),stops=p.itinerary?.stops?.filter(s=>(s.day||1)===day)||[];const cost=p.costs||{};const isAccepted=S.trip?.accepted_plan?.id===p.id;const eligible=canAccept(p);const issues=p.error?[p.error]:(p.issues||[]).map(i=>i.message);
return heading+planningRecords()+busy+(stalePlan(p)?'<div class="notice">旅行偏好已修改，下面保留的是原方案及原日期，不能作为新需求的结果接受。请重新规划。</div>':'')+`<div class="actions">${tag(p.data_mode)}${tag(stalePlan(p)?'旧方案 · 待重新规划':isAccepted?'已接受 v'+S.trip.accepted_version:p.status==='ready'?'候选 · 待确认':'待解决冲突',p.status!=='ready')}${button('晚出门一小时','later','tiny secondary')}${button('节奏再慢一点','relax','tiny secondary')}${button('导出所示方案','export','tiny secondary')}</div>${issues.length?`<div class="notice danger">${issues.map(esc).join('<br>')}</div>`:''}<div class="day-tabs">${Array.from({length:d.days},(_,i)=>`<button class="${day===i+1?'active':''}" aria-pressed="${day===i+1}" data-day="${i+1}">DAY 0${i+1}<small>${dateFor(i+1,snapshot||{})}</small></button>`).join('')}</div><div class="itinerary-layout"><section><div class="timeline">${stops.length?stops.map((s,index)=>{const place=p.places?.find(t=>t.id===s.place_id)||{};const leg=p.routes?.find(r=>r.day===day && (r.stop_index!=null?r.stop_index===index:r.destination_id===s.place_id && r.origin_id===(index?stops[index-1].place_id:r.origin_id)));return `<article class="stop"><div class="stop-time">${esc(s.start_time)}<small>至 ${esc(s.end_time)}</small></div><div>${tag(({activity:'城市体验',booking:'固定预约',meal:'用餐建议',hotel:'住宿安排'})[s.kind]||'活动')}<h3>${esc(stopHeading(s,place))}</h3><p>安排建议：${esc(s.reason)}</p><div class="stop-meta">${esc(place.address||'')} · ${esc(place.source||'来源待确认')}</div>${leg?`<div class="leg">↗ 从上一站${esc(leg.mode)}约${esc(leg.duration_minutes)}分钟${leg.distance_meters?' · '+(leg.distance_meters/1000).toFixed(1)+'公里':''} · ${esc(leg.source)}</div>`:''}</div></article>`;}).join(''):'<p class="muted">这一天尚未生成可接受安排，请查看冲突并重新规划。</p>'}</div><div class="card" style="margin-top:20px"><h3>本次查询与检查</h3><ol class="trace">${(p.trace||[]).map(t=>`<li>${esc(t.detail)}</li>`).join('')}</ol></div></section><aside class="card summary-card"><h3>这趟旅行的预留</h3><p class="muted small">全体${d.party_size}人 · ${daysLabel(d)}</p>${[['住宿预算预留',cost.accommodation],['餐饮预算预留',cost.food],['门票实际费用',cost.tickets],['市内交通费用',cost.transport],['住宿＋餐饮预留',cost.subtotal],['你的整趟预算',cost.budget]].map(([a,b])=>`<div class="cost-row"><span>${a}</span><strong>${money(b)}</strong></div>`).join('')}<p class="muted small">${esc(cost.scope||'费用未核实')}</p><div class="divider"></div><h3>住宿方案</h3><p class="small">${esc(p.hotel?.name||'当日往返 / 尚未确定')}</p>${button('查看住宿详情','hotels','secondary tiny')}<div class="divider"></div><h3>出发前再确认</h3><ul>${(p.warnings||[]).map(t=>`<li>${esc(t)}</li>`).join('')}</ul>${!isAccepted?button('接受当前候选行程','accept','',!eligible?'disabled':''):tag('当前已接受方案已保存')}<p class="muted small">接受只保存本应用内的版本，不代表完成酒店或门票预订。</p></aside></div>`;}
function settings(){const c=S.config;return `<div class="page-heading"><div><p class="eyebrow">CONNECTED, WITH CLEAR BOUNDARIES</p><h1>接入真实信息，开始真实规划。</h1><p class="muted">Key 只保存在你电脑的 .env 中，不放进网页，也不提交到仓库。</p></div>${button('刷新配置状态','refresh','secondary')}</div><div class="settings-grid"><section class="card"><h2>两项服务，各司其职</h2><div class="status-card"><div><strong><i class="status-dot ${c.map_configured?'ok':''}"></i>高德 Web 服务</strong><p class="muted small">真实地点、酒店位置与路段耗时</p></div>${tag(c.map_configured?'已配置':'待配置',!c.map_configured)}</div><div class="status-card"><div><strong><i class="status-dot ${c.model_configured?'ok':''}"></i>DeepSeek</strong><p class="muted small">${esc(c.model)} · 理解额外要求与修订方案</p></div>${tag(c.model_configured?'已配置':'待配置',!c.model_configured)}</div><p class="muted small">“已配置”表示检测到本地Key，不代表接口权限或额度已经验证。</p><div class="divider"></div><h3>选择规划方式</h3><div class="choice-group"><button class="choice wide ${S.mode==='basic'?'active':''}" aria-pressed="${S.mode==='basic'}" data-mode="basic">基础排程<small>需要高德Key，按真实路线与约束排程</small></button><button class="choice wide ${S.mode==='deepseek'?'active':''}" aria-pressed="${S.mode==='deepseek'}" data-mode="deepseek">DeepSeek智能规划<small>需要两个Key，支持自然语言需求</small></button></div><p class="muted small">当前方式：${S.mode==='basic'?'基础排程':'DeepSeek智能规划'}。缺少配置或调用失败时会明确报错，不会自动输出固定数据。</p></section><section class="card"><h2>本地配置，只需三步</h2><ol class="trace"><li>将项目根目录 .env.example 复制为 .env。</li><li>填写高德Web服务Key，以及稍后申请的DeepSeek Key。</li><li>回到此页面点击“刷新配置状态”，再生成行程。</li></ol><pre class="code">AMAP_API_KEY=你的高德Web服务Key
DEEPSEEK_API_KEY=你的DeepSeekKey
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=${esc(c.model)}</pre><p class="muted small">高德Key类型选 Web 服务。酒店地点搜索不等于实时订房服务；本版不收集预订账户或支付信息。</p><div class="notice">MCP适配会接在地图工具层，后续替换调用协议时保留行程与约束合同。本轮先跑通官方HTTP接口。</div><div class="actions">${button('回到行程工作台 →','itinerary')}${button('查看旅行偏好','preferences','secondary')}</div></section></div>`;}
const pages = {overview, preferences, hotels, itinerary, settings};
function render() {
  if (!S.draft) return;
  document.querySelectorAll('nav a').forEach(a => {
    const active = a.hash === '#' + S.page;
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  $('connection').textContent = S.config.map_configured
    ? (S.config.model_configured ? '地图与模型已配置' : '高德已配置 · 基础排程') : '连接地图与 DeepSeek';
  $('app').innerHTML = (S.error ? `<div class="notice danger" role="alert">${esc(S.error)}</div>` : '')
    + (pages[S.page] || overview)();
  $('app').setAttribute('aria-busy', String(S.busy));
  if (S.busy) document.querySelectorAll('#app input, #app textarea, #app button').forEach(el => {
    el.disabled = true;
  });
}
function go(page) {
  S.page = Object.hasOwn(pages, page) ? page : 'overview';
  if (location.hash !== '#' + S.page) location.hash = S.page;
  render();
  window.scrollTo({top:0, behavior:'smooth'});
}
function visibleFieldsValid() {
  return [...document.querySelectorAll('#app input, #app textarea')].every(el => el.reportValidity());
}
function rememberTrip(trip) {
  localStorage.setItem('tripweave-v2-id', trip.id);
  const summary = {id:trip.id, requirements:trip.requirements,
    requirements_revision:trip.requirements_revision, accepted_version:trip.accepted_version};
  S.history = [summary, ...S.history.filter(t => t.id !== trip.id)].slice(0, 20);
}
function resetHotels() {
  S.hotels = [];
  S.hotelContext = null;
  S.recommended = null;
}
function adoptTrip(trip) {
  S.trip = trip;
  S.draft = {...S.config.defaults, ...trip.requirements, days:trip.requirements.days || 1};
  S.candidate = null;
  S.planHistory = [];
  S.historyOpen = false;
  S.day = 1;
  S.step = 1;
  resetHotels();
  rememberTrip(trip);
}
async function save() {
  if (S.trip && !dirty()) return;
  S.trip = S.trip
    ? await api(`/trips/${S.trip.id}/requirements`, 'PUT', {...S.draft, expected_revision:S.trip.requirements_revision})
    : await api('/trips', 'POST', S.draft);
  rememberTrip(S.trip);
}
async function busy(action) {
  if (S.busy) return;
  S.busy = true;
  S.error = null;
  render();
  try { await action(); }
  catch (error) {
    S.error = error.message;
    if (error.status === 409 && S.trip) {
      // Refresh server versions without throwing away unsaved edits or the stale candidate.
      try { S.trip = await api('/trips/' + S.trip.id); rememberTrip(S.trip); } catch {}
    }
    toast(error.message);
  } finally { S.busy = false; render(); }
}
async function generate() {
  if (!S.config.map_configured || (S.mode === 'deepseek' && !S.config.model_configured)) {
    S.error = !S.config.map_configured ? '请先配置高德Web服务Key，再生成真实行程。' : '请先配置DeepSeek Key。';
    toast(S.error);
    go('settings');
    return;
  }
  S.page = 'itinerary';
  location.hash = 'itinerary';
  await busy(async () => {
    await save();
    const input = {...S.trip.requirements};
    const candidate = await api(`/trips/${S.trip.id}/plan`, 'POST', {mode:S.mode});
    S.candidate = {...candidate, requirements:candidate.requirements || input};
    S.planHistory = [S.candidate, ...S.planHistory.filter(p => p.id !== candidate.id)].slice(0,20);
    S.day = 1;
    S.hotels = S.candidate.hotel_options || [];
    S.hotelContext = hotelQueryContext(input);
    S.recommended = S.candidate.hotel;
    toast(S.candidate.status === 'ready' ? '行程已生成，请查看每日安排与待确认事项。'
      : S.candidate.error || '发现未解决的约束，请查看冲突说明。');
  });
}
const actions = {
  overview:() => go('overview'), preferences:() => go('preferences'),
  hotels:() => go('hotels'), itinerary:() => go('itinerary'), settings:() => go('settings'),
  next:() => { if (visibleFieldsValid()) { S.step = Math.min(3, S.step + 1); render(); } },
  prev:() => { S.step = Math.max(1, S.step - 1); render(); },
  constraints:() => { S.step = 3; go('preferences'); },
  finish:() => {
    if (!visibleFieldsValid()) return;
    return busy(async () => { await save(); toast('旅行偏好已保存。'); go('hotels'); });
  },
  new:() => {
    S.trip = null; S.candidate = null; S.error = null; S.planHistory = []; S.historyOpen = false; resetHotels();
    S.draft = {...S.config.defaults}; S.step = 1; S.day = 1;
    localStorage.removeItem('tripweave-v2-id'); go('preferences');
  },
  generate,
  'query-hotels':() => busy(async () => {
    const input = {...S.draft};
    const result = await api('/hotels/preview', 'POST', input);
    S.hotels = result.options;
    S.hotelContext = hotelQueryContext(input);
    S.recommended = result.recommended;
    toast(S.hotels.length ? '真实酒店候选已查询，房价与空房需另行确认。' : '未查询到酒店，请检查目的地和天数。');
  }),
  hotel:el => {
    if (!currentHotels().some(h => h.id === el.dataset.id)) { toast('酒店候选已过时，请重新查询。'); return; }
    S.draft.hotel_id = el.dataset.id; render(); toast('已选择住宿，重新规划时会保留。');
  },
  'auto-hotel':() => { S.draft.hotel_id = null; render(); toast('已恢复按地理位置自动推荐。'); },
  later:() => {
    const [h, m] = S.draft.start_time.split(':').map(Number);
    const next = String(h + 1).padStart(2, '0') + ':' + String(m).padStart(2, '0');
    if (h >= 23 || next >= S.draft.end_time) { toast('出发时间不能晚于结束时间，请在偏好中调整。'); return; }
    S.draft.start_time = next; render(); toast('出发时间已修改，点击重新规划后生效。');
  },
  relax:() => {
    S.draft.pace = 'relaxed'; S.draft.max_activities = 2;
    render(); toast('已调整为悠闲节奏，每日最多2项活动，请重新规划。');
  },
  refresh:() => busy(async () => { S.config = await api('/config'); toast('配置状态已刷新。'); }),
  accept:() => busy(async () => {
    const plan = shownPlan();
    if (!canAccept(plan)) throw Error('此方案未通过检查或已过时，请重新规划后再接受。');
    S.trip = await api(`/trips/${S.trip.id}/candidates/${plan.id}/accept`, 'POST', {});
    S.candidate = S.trip.accepted_plan; rememberTrip(S.trip); toast('已接受并保存当前行程。');
  }),
  load:el => busy(async () => {
    adoptTrip(await api('/trips/' + el.dataset.id));
    go('itinerary');
    await loadPlanHistory(true);
  }),
  history:() => busy(async () => {
    S.historyOpen = !S.historyOpen;
    if (S.historyOpen) await loadPlanHistory();
  }),
  'show-accepted':() => {
    S.candidate = S.trip?.accepted_plan || null; S.day = 1; S.error = null;
    resetHotels(); render();
  },
  'show-candidate':el => {
    const plan = S.planHistory.find(p => p.id === el.dataset.id);
    if (!plan) { toast('未找到该规划记录，请刷新记录列表。'); return; }
    S.candidate = plan; S.day = 1; S.error = null;
    resetHotels(); render();
  },
  export:() => {
    const plan = shownPlan();
    if (!plan) return;
    const payload = exportPayload(plan);
    const blob = new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'TripWeave-' + (payload.requirements?.date || plan.id) + '.json';
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};
function changeDraft(key, value) {
  if (S.draft[key] === value) return;
  S.draft[key] = value;
  S.error = null;
  if (key === 'city') {
    // A destination change invalidates city-specific facts, including a previous booking.
    S.draft.origin_query = ''; S.draft.booking_query = ''; S.draft.hotel_id = null;
    resetHotels();
    document.querySelectorAll('[data-field="origin_query"], [data-field="booking_query"]')
      .forEach(el => { el.value = ''; });
  }
  if (['origin_query', 'booking_query', 'hotel_preference', 'days'].includes(key)) resetHotels();
  if (key === 'days') {
    S.draft.booking_day = Math.min(S.draft.booking_day, S.draft.days);
    if (value === 1) S.draft.hotel_id = null;
  }
}
document.addEventListener('click', event => {
  const el = event.target.closest('button');
  if (!el || el.disabled || S.busy) return;
  if (el.dataset.fieldChoice) {
    const key = el.dataset.fieldChoice;
    changeDraft(key, ['days', 'max_activities'].includes(key) ? Number(el.dataset.value) : el.dataset.value);
    render();
  } else if (el.dataset.city) {
    changeDraft('city', el.dataset.city);
    if (!S.draft.origin_query) S.draft.origin_query = el.dataset.city === '上海' ? '人民广场' : el.dataset.city + '站';
    render();
  } else if (el.dataset.interest) {
    const tags = new Set(S.draft.interests.split('、').filter(Boolean));
    tags.has(el.dataset.interest) ? tags.delete(el.dataset.interest) : tags.add(el.dataset.interest);
    changeDraft('interests', [...tags].join('、')); render();
  } else if (el.dataset.step) {
    S.step = Number(el.dataset.step); render();
  } else if (el.dataset.day) {
    S.day = Number(el.dataset.day); render();
  } else if (el.dataset.mode) {
    S.mode = el.dataset.mode; render();
  } else if (actions[el.dataset.action]) actions[el.dataset.action](el);
});
document.addEventListener('input', event => {
  const key = event.target.dataset.field;
  if (!key || S.busy) return;
  changeDraft(key, event.target.type === 'number' ? Number(event.target.value) : event.target.value);
});
window.addEventListener('hashchange', () => {
  const page = location.hash.slice(1);
  S.page = Object.hasOwn(pages, page) ? page : 'overview'; render();
});
(async () => {
  try {
    S.config = await api('/config'); S.draft = {...S.config.defaults}; S.history = await api('/trips');
    const id = localStorage.getItem('tripweave-v2-id');
    if (id) {
      try { adoptTrip(await api('/trips/' + id)); await loadPlanHistory(true); }
      catch (error) {
        if (error.status === 404) localStorage.removeItem('tripweave-v2-id');
        else S.error = error.message;
      }
    }
    const page = location.hash.slice(1);
    S.page = Object.hasOwn(pages, page) ? page : 'overview'; render();
  } catch (error) {
    $('app').replaceChildren();
    const message = document.createElement('p');
    message.textContent = '无法加载工作台：' + error.message; $('app').append(message);
  }
})();
