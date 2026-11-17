const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => value == null ? '待确认' : '¥'+Number(value).toLocaleString('zh-CN');
const S = {page:'overview',step:1,day:1,config:null,draft:null,trip:null,candidate:null,hotels:[],hotelContext:null,recommended:null,history:[],planHistory:[],historyOpen:false,planningRuns:[],planningRunsOpen:false,revisionText:'',travelProfile:null,profileDraft:null,visitDayInputs:Object.create(null),busy:false,error:null};
let visitConstraintCounter = 0;
let attractionPreview = null;
let attractionTask = '';
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
const pacingKeys = ['max_attractions','max_sightseeing_minutes','max_sightseeing_ratio','max_transport_minutes'];
function normalizePacing(values) {
  const normalized = {...values};
  if (!Object.hasOwn(normalized, 'max_attractions') && Object.hasOwn(normalized, 'max_activities')) {
    normalized.max_attractions = normalized.max_activities;
  }
  delete normalized.max_activities;
  return normalized;
}
function draftFrom(values) {return {...normalizePacing(S.config.defaults), ...normalizePacing(values)};}
function pacingFields(values, profileMode=false) {
  const definitions = [
    ['每日景点数量上限','max_attractions',20,1,'同一地点当天分段参观计一个；固定预约地点也计入。'],
    ['每日游玩时长上限（分钟）','max_sightseeing_minutes',1440,1,'累计参观、体验时间；用餐、住宿、纯休息与交通除外。'],
    ['游玩时间占比上限（%）','max_sightseeing_ratio',100,'any','游玩分钟 ÷ 每日出发至结束的固定窗口。与游玩时长同时填写时，两项都须满足。'],
    ['每日交通时长上限（分钟）','max_transport_minutes',1440,1,'累计地图路段耗时，含首段和回酒店；不含额外交通缓冲。'],
  ];
  return `<div class="field-grid">${definitions.map(([label,key,max,step,help])=>`<label class="field">${label}<input ${profileMode?'name':'data-field'}="${key}" type="number" min="0" max="${max}" step="${step}" placeholder="留空不限" value="${esc(values[key])}"><small>${help}</small></label>`).join('')}</div><p class="muted small">四项独立设置，留空表示不限，填写 0 表示上限为 0。具体景点取舍由 AI 根据你的需求安排；程序只检查，不自动搬动行程。</p>`;
}
function dirty(){const original=S.trip?draftFrom(S.trip.requirements):{};return S.trip && (Object.entries(S.draft).some(([k,v])=>JSON.stringify(original[k])!==JSON.stringify(v))
  || (S.draft.visit_constraints || []).some(item => Object.hasOwn(S.visitDayInputs,item.constraint_id)
    && S.visitDayInputs[item.constraint_id] !== item.windows.map(window=>window.day).join(',')));}
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
  return isReadyPlan(plan) && !stalePlan(plan)
    && plan.trip_id === S.trip?.id && plan.base_version === S.trip?.accepted_version;
}
function isReadyPlan(plan) {
  return !!plan && plan.status === 'ready' && (!plan.outcome || plan.outcome === 'ready')
    && (plan.review == null || plan.review.status === 'approved');
}
function hasReviewDisagreement(plan) {
  return plan.review?.status === 'disputed'
    || (plan.issues || []).some(issue => issue.category === 'review_disagreement');
}
function planningOutcomeLabel(plan) {
  if (hasReviewDisagreement(plan)) return '规划分歧 · 待你协调';
  if (plan.status === 'ready' && (!plan.outcome || plan.outcome === 'ready') && plan.review && plan.review.status !== 'approved') return '校验协商未完成';
  return ({ready:'可用候选',needs_information:'资料待补充',needs_user_decision:'需要调整要求',
    incomplete:'本次未完成',service_error:'接口异常'})[plan.outcome]
    || (plan.status === 'ready' ? '检查通过' : plan.error ? '规划失败' : '存在冲突');
}
function planningOutcomeMessage(plan) {
  if (hasReviewDisagreement(plan)) return '规划 Agent 与校验 Agent 仍有不同意见，请查看具体分歧和双方理由。这不代表你的需求无法满足。';
  if (plan.status === 'ready' && (!plan.outcome || plan.outcome === 'ready') && plan.review && plan.review.status !== 'approved') return '这份记录尚未完成校验协商，暂不可接受。';
  return ({ready:'已通过当前已知路线与约束检查，请核对待确认事项。',
    needs_information:'关键资料仍不足，请查看具体缺口，补充可靠信息后再继续规划。',
    needs_user_decision:'已确认的要求无法同时满足，请核对冲突说明并修改相应要求。',
    incomplete:'本次尚未找到通过校验的方案，不代表你的旅行需求无解。',
    service_error:'接口请求未正常完成，请检查接入状态后重试。'})[plan.outcome] || '';
}
function planningIssueText(issue, plan) {
  const labels = {opening_hours:'开放时段',last_admission:'最晚入场时间',reservation_rules:'预约规则'};
  const place = (plan.places || []).find(item=>item.id === issue.place_id);
  const position = [issue.day ? '第 '+issue.day+' 天' : '',issue.stop_number ? '第 '+issue.stop_number+' 站' : '', place?.name || ''].filter(Boolean);
  const missing = (issue.missing_fields || []).map(field=>labels[field] || '所需资料');
  const diagnostics = (issue.diagnostics || []).map(item => {
    const actual = item.actual == null ? '' : '（提取/返回值：'+(Array.isArray(item.actual)?item.actual.join('、'):String(item.actual))+'）';
    return (labels[item.field] || '所需资料')+' · '+(item.origin === 'model'?'研究 Agent 判断（待核实）':'程序检测')+'：'+item.message+actual;
  });
  return [position.join(' · '),issue.message,missing.length ? '待补充：'+missing.join('、') : '',...diagnostics].filter(Boolean).join('：');
}
function parseVisitDays(value) {
  const text = value.trim();
  if (!/^[1-7](?:[\s,，、]+[1-7])*$/.test(text)) return null;
  const days = text.split(/[\s,，、]+/).map(Number);
  return new Set(days).size === days.length ? days : null;
}
function editableVisitConstraint(item) {
  const windows = item.windows || [];
  return !item.fixed && typeof item.place_query === 'string' && windows.length > 0
    && windows.every(window => window.start_time === windows[0].start_time && window.end_time === windows[0].end_time);
}
function visitConstraintsForm() {
  const items = S.draft.visit_constraints || [];
  return `<section class="visit-constraints"><div class="section-title"><h3>景点时间要求（可选）</h3>${button('＋ 添加时间要求','add-visit-constraint','secondary tiny',items.length >= 10 ? 'disabled' : '')}</div>
    <p class="muted small">填写后会作为必去要求。多个可选日期表示任选一天，完整游玩时段必须落在所填窗口内；没有特别要求可以留空。精确预约仍可填写下方的固定预约。</p>
    ${items.length ? items.map((item,index) => {
      const windows = item.windows || [], first = windows[0] || {};
      const remove = button('删除这条','remove-visit-constraint','secondary tiny',`data-id="${esc(item.constraint_id)}" aria-label="删除第 ${index+1} 条景点时间要求"`);
      const input = (label,key,type,value,attrs='') => `<label class="field">${label}<input data-visit-field="${key}" data-constraint-id="${esc(item.constraint_id)}" type="${type}" value="${esc(value)}" ${attrs} required></label>`;
      return `<fieldset class="visit-constraint"><legend>时间要求 ${index+1}</legend><div class="actions">${remove}</div>${editableVisitConstraint(item)
        ? `<div class="field-grid">${input('必去景点名称','place_query','text',item.place_query,'maxlength="200"')}${input('可选第几天','days','text',S.visitDayInputs[item.constraint_id] ?? windows.map(window=>window.day).join(','),'placeholder="例如：1,2，任选一天" inputmode="text" maxlength="30"')}</div><div class="field-grid">${input('可开始游玩的时间','start_time','time',first.start_time)}${input('须结束游玩的时间','end_time','time',first.end_time)}${input('至少游玩多久（分钟）','min_duration_minutes','number',item.min_duration_minutes,'min="1" max="720"')}</div>`
        : `<p><strong>${esc(item.place_query || (S.draft.selected_attractions || []).find(place=>place.id===item.place_id)?.name || '已确认景点')}</strong> · 至少 ${esc(item.min_duration_minutes)} 分钟</p><ul>${windows.map(window=>`<li>第 ${esc(window.day)} 天 · ${esc(window.start_time)}–${esc(window.end_time)}</li>`).join('')}</ul><p class="muted small">这条要求含不同时间窗口或精确安排，已完整保留。需要替换时，请删除后重新添加。</p>`}</fieldset>`;
    }).join('') : '<p class="muted small">尚未添加景点时间要求。</p>'}</section>`;
}
function updateVisitConstraint(id, key, value) {
  const items = S.draft.visit_constraints || [], index = items.findIndex(item=>item.constraint_id===id);
  if (index < 0 || !editableVisitConstraint(items[index])) return;
  const item = items[index];
  let update;
  if (key === 'place_query') update = {...item,place_query:value,place_id:null};
  else if (key === 'days') {
    const days = parseVisitDays(value);
    if (!days) { S.visitDayInputs[id] = value; S.error = null; return; }
    delete S.visitDayInputs[id];
    update = {...item,windows:days.map(day=>({...item.windows[0],day}))};
  } else if (['start_time','end_time'].includes(key)) {
    update = {...item,windows:item.windows.map(window=>({...window,[key]:value}))};
  } else if (key === 'min_duration_minutes') {
    update = {...item,min_duration_minutes:value === '' ? null : Number(value)};
  } else return;
  changeDraft('visit_constraints',items.map((entry,i)=>i===index ? update : entry));
}
function visitConstraintError() {
  const items = S.draft.visit_constraints || [];
  if (items.length > 10) return '景点时间要求最多 10 条。';
  const time = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
  for (const [index,item] of items.entries()) {
    const prefix = '第 '+(index+1)+' 条景点时间要求：';
    if (!item.place_id && !item.place_query?.trim()) return prefix+'请填写明确的景点名称。';
    if (Object.hasOwn(S.visitDayInputs,item.constraint_id) && !parseVisitDays(S.visitDayInputs[item.constraint_id])) return prefix+'日期请填 1 或 1,2，不能重复或留空。';
    if (!item.windows?.length || item.windows.some(window=>!Number.isInteger(window.day) || window.day < 1 || window.day > S.draft.days)) return prefix+'可选日期须在本次旅行天数内，请调整或删除这条要求。';
    if (item.windows.some(window=>!time.test(window.start_time) || !time.test(window.end_time) || window.end_time <= window.start_time)) return prefix+'请填写有效时段，结束须晚于开始。';
    if (!Number.isInteger(item.min_duration_minutes) || item.min_duration_minutes < 1 || item.min_duration_minutes > 720) return prefix+'游玩时长须为 1–720 分钟的整数。';
  }
  return '';
}
function hotelQueryContext(req) {
  return JSON.stringify(['city', 'origin_query', 'booking_query', 'hotel_preference', 'custom_hotel', 'days', 'nights']
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
function nightsFor(req = S.draft) { return req.nights ?? Math.max(0, req.days - 1); }
function daysLabel(req = S.draft){return req.days+'天'+nightsFor(req)+'晚';}
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
    ? (S.planHistory.length ? S.planHistory.map(plan => `<div class="history-row"><div><strong>${esc(plan.requirements?.city||'旅行方案')} · ${plan.mode==='deepseek'?'DeepSeek':'基础排程'}</strong><p>${esc(displayTime(plan.created_at))} · 需求 v${esc(plan.requirements_revision)}</p>${tag(planningOutcomeLabel(plan),!isReadyPlan(plan))} ${tag(plan.id===S.trip.accepted_plan?.id?'当前已接受':stalePlan(plan)?'已过时':'候选')}${plan.error?`<p class="danger">${esc(plan.error)}</p>`:''}</div>${button(current?.id===plan.id?'正在查看':'查看此记录', 'show-candidate', 'secondary tiny', `data-id="${esc(plan.id)}"`)}</div>`).join('')
      : '<p class="muted small">尚无规划记录；生成的候选和失败信息会保存在这里。</p>') : ''}</section>`;
}
async function loadPlanHistory(restoreLatest = false) {
  if (!S.trip) { S.planHistory = []; return; }
  const history = await api(`/trips/${S.trip.id}/history`);
  S.planHistory = (history.candidates || []).filter(plan => ['basic','deepseek'].includes(plan.mode));
  if (restoreLatest) { S.candidate = S.planHistory[0] || null; S.day = 1; }
}
function missingNotice(){return !S.config.map_configured?`<div class="notice">保存高德和 DeepSeek 的 Key 后，即可根据旅行偏好和真实地点生成行程。${button('查看接入步骤','settings','tiny secondary')}</div>`:'';}
function heroArt(){return `<div class="hero-art" aria-hidden="true"><svg viewBox="0 0 400 230" fill="none"><circle cx="310" cy="48" r="28" fill="#f5e4b8"/><path d="M10 204H390" stroke="#72927b"/><path d="M44 204V130H88V204M105 204V94H147V204M172 204V116H218V204M244 204V70H279V204M302 204V119H345V204" fill="#78967f"/><path d="M259 70V26M251 41H267M191 116V66M181 81H201" stroke="#50785f" stroke-width="3"/><path d="M14 219C80 170 120 236 180 213S300 193 389 216" stroke="#e3ede1" stroke-width="6"/><path d="M33 128C95 34 206 77 330 164" stroke="#f9f9ef" stroke-dasharray="5 7" stroke-width="2"/><circle cx="33" cy="128" r="6" fill="#f7f7ee"/><circle cx="330" cy="164" r="6" fill="#f7f7ee"/></svg><span class="hero-note">A LITTLE PLAN. A BETTER JOURNEY.</span></div>`;}
function overview(){const d=S.draft,p=shownPlan();return `<section class="hero"><div class="hero-copy"><p class="eyebrow">YOUR NEXT CHAPTER / 下一次出发</p><h1>把${esc(d.city)}的${d.days}天，<br>安排成喜欢的样子。</h1><p class="subtle">从住宿片区到每日路线，把人文、风味和留白织进行程。你可以随时改变想法，让计划跟上变化。</p><div class="actions">${button('开始完善旅行偏好 ↗','preferences')}${button(p?'查看当前行程':'查看规划工作台','itinerary','ghost')}</div></div>${heroArt()}</section><div class="metrics"><div class="metric"><span>旅行时长</span><strong>${daysLabel()}</strong><small>${esc(d.date)}</small></div><div class="metric"><span>同行人数</span><strong>${d.party_size}</strong><small>位旅行者</small></div><div class="metric"><span>整趟预算</span><strong>${money(d.budget_total)}</strong><small>全体人数</small></div><div class="metric"><span>当前规划方式</span><strong style="font-size:18px">DeepSeek</strong><small>${S.config.map_configured?'真实高德地图':'等待地图接入'}</small></div></div>${missingNotice()}<div class="section-title"><h2>一趟旅行，慢慢成形</h2><span class="muted small">三步，把想法变成可检查的安排</span></div><div class="grid3">${[['01','先说说你想怎么旅行','日期、节奏、预算和不能错过的预约，都从这里开始。','preferences','填写旅行偏好'],['02','找到合适的落脚点','依据真实酒店位置推荐片区与备选，房价与房量保持待确认。','hotels','查看住宿方案'],['03','让每天都有自己的节奏','真实路线核对交通，按天查看景点、餐饮、住宿与待办事项。','itinerary','进入每日安排']].map(([n,title,text,target,label])=>`<article class="card"><span class="step-num">${n}</span><h3>${title}</h3><p class="muted small">${text}</p>${button(label+' →',target,'secondary')}</article>`).join('')}</div><div class="section-title"><h2>最近的行程</h2>${button('＋ 新建旅行','new','secondary tiny')}</div><section class="card">${S.history.length?S.history.slice(0,5).map(t=>`<div class="history-row"><div><strong>${esc(t.requirements.city)} · ${t.requirements.days||1}日旅行</strong><p>${esc(t.requirements.date)} · 已接受版本 ${t.accepted_version}</p></div>${button('继续查看 →','load','secondary tiny',`data-id="${esc(t.id)}"`)}</div>`).join(''):'<p class="muted small">这里会保存你创建过的旅行。先从一份偏好开始吧。</p>'}</section>`;}
function preferences(){const d=S.draft;let content='';if(S.step===1){content=`<p class="eyebrow">01 / 出发的轮廓</p><h2>先决定，去哪里待多久。</h2><p class="muted small">支持国内城市地点查询；请填写明确的出发地点或酒店名称。</p><div class="choice-group">${['上海','杭州','苏州','南京','成都','北京'].map(city=>`<button class="choice ${d.city===city?'active':''}" aria-pressed="${d.city===city}" data-city="${city}">${city}</button>`).join('')}</div><div class="field-grid">${field('目的地城市','city')}${field('出行日期','date','date')}</div><label class="field">旅行天数</label>${choices('days',[[1,'1天','当日往返'],[2,'2天1晚','周末短途'],[3,'3天2晚','多留一点时间']])}<div class="field-grid">${field('自定义天数','days','number','支持 1–7 天。','min="1" max="7" required')}<label class="field">自定义住宿晚数<input data-field="nights" type="number" min="0" max="6" placeholder="${nightsFor(d)}" value="${d.nights ?? ''}"><small>留空按天数减一计算。指定较少晚数时，住宿从第一天起连续安排。</small></label></div><div class="field-grid">${field('同行人数','party_size','number','房间暂按每间2人预留，实际入住政策待核实。','min="1" max="6"')}${field('整趟预算（全体人数 / 元）','budget_total','number','不含往返大交通和购物。','min="100" max="100000"')}</div>${field('第一天从哪里出发','origin_query','text','例如：上海站、人民广场，或你已知的酒店名称。')}`;}
if(S.step===2){const tags=['城市漫步','历史人文','自然公园','室内展览','美食探索','江景夜色'];content=`<p class="eyebrow">02 / 旅行的温度</p><h2>把节奏留给自己。</h2><label class="field">你更在意什么？</label><div class="choice-group">${tags.map(t=>`<button class="choice ${d.interests.includes(t)?'active':''}" aria-pressed="${d.interests.includes(t)}" data-interest="${t}">${t}</button>`).join('')}</div>${field('自定义兴趣','custom_interests','text','例如：独立书店、建筑摄影。','maxlength="300"')}<label class="field">一天的节奏</label>${choices('pace',[['relaxed','悠闲慢游','单站停留更久'],['balanced','刚刚好','体验与留白兼顾'],['full','充实探索','增加城市体验']])}${field('自定义节奏','custom_pace','text','例如：午饭后休息一小时，每站至少留 90 分钟。','maxlength="300"')}<label class="field">市内交通</label>${choices('transport',[['mixed','步行 + 公共交通','较远路段查询公共交通'],['walking','尽量步行','用步行路线核对时间']])}${field('自定义交通需求','custom_transport','text','例如：尽量少换乘。路线耗时按上方选中的方式核对。','maxlength="300"')}<div class="field-grid">${field('每天最早出发','start_time','time')}${field('每天最晚结束','end_time','time')}</div>${pacingFields(d)}`;}
if(S.step===3){content=`<p class="eyebrow">03 / 安心的边界</p><h2>保留那些已经确定的事。</h2><div class="field-grid">${field('住宿上限（每间每晚 / 元）','hotel_budget','number','仅为预算预留，不当作真实报价。','min="100" max="5000"')}${field('预约在第几天','booking_day','number','','min="1" max="'+d.days+'"')}</div><label class="field">住宿取向</label>${choices('hotel_preference',[['balanced','衔接便利','靠近出发或预约片区'],['value','预算优先','房价需订房平台核实'],['riverside','体验优先','具体景观房需另行确认']])}${field('自定义住宿取向','custom_hotel','text','例如：靠近地铁站、希望有家庭房。房型与设施待确认。','maxlength="300"')}<label class="field">必去景点<textarea data-field="must_visit" maxlength="404" placeholder="每行一个，最多 5 个">${esc((d.must_visit || []).join('\n'))}</textarea><small>请填写明确的景点名称，规划会核对是否安排了这些地点。</small></label>${visitConstraintsForm()}${field('固定活动 / 预约地点','booking_query','text','没有固定预约可留空。这里只保留你的计划，不会代你预约。')}<div class="field-grid">${field('预约开始','booking_start','time')}${field('预约结束','booking_end','time')}</div><label class="field">额外需求<textarea data-field="notes" maxlength="1500" placeholder="例如：带父母出行，尽量减少长距离步行；希望第二天下午保留自由时间。">${esc(d.notes)}</textarea><small>DeepSeek 会结合这段说明和已填的日期、节奏及约束生成候选。</small></label>`;}
return `<div class="page-heading"><div><p class="eyebrow">TRAVEL BRIEF</p><h1>旅行偏好</h1><p class="muted">一次说清楚，后面的修改不必从头开始。</p></div><div class="actions">${tag('第 '+S.step+' / 3 步')}${button('长期偏好','profile','secondary')}</div></div><div class="layout"><aside class="sidebar">${[['目的地与日期','先画出旅途轮廓'],['兴趣与节奏','决定一天怎样度过'],['预算与固定安排','把重要的事留住']].map(([a,b],i)=>`<button class="step-link ${S.step===i+1?'active':''}" aria-current="${S.step===i+1?'step':'false'}" data-step="${i+1}"><span>0${i+1}</span><div><strong>${a}</strong><small>${b}</small></div></button>`).join('')}</aside><section class="card form-card">${content}<div class="form-bottom">${S.step>1?button('← 上一步','prev','secondary'):button('返回概览','overview','secondary')}${S.step<3?button('下一步 →','next'):preferenceNextActions(d)}</div></section></div>`;}
function preferenceNextActions(draft) {
  const chooseHotel = nightsFor(draft)
    ? button(draft.hotel_id ? '调整已选酒店' : '先选酒店', 'finish', 'secondary', 'data-next="hotels"')
    : '';
  const selected = (draft.selected_attractions || []).map(place => esc(place.name)).join('、');
  const summary = selected ? `<div class="actions"><p class="small">已选景点：${selected}</p>${button('清除选景', 'clear-attractions', 'secondary tiny')}</div>` : '';
  return `<div class="planning-choices">
    ${summary}
    <p class="small"><strong>选择规划方式</strong></p>
    <div class="actions">${button('一键生成行程', 'generate')}${button('一起选景点', 'choose-attractions', 'secondary')}</div>
    <p class="muted small">可以直接生成完整行程，也可以先挑几个景点再排程。两种方式生成后都能修改。</p>
    ${chooseHotel ? `<div class="actions">${chooseHotel}</div>` : ''}
  </div>`;
}

function profile() {
  const values = S.profileDraft || S.travelProfile?.values;
  if (!values) return `<p class="notice">请读取长期偏好。${button('读取长期偏好', 'profile')}</p>`;
  const select = (label, key, options) => `<label class="field">${label}<select name="${key}">${options.map(([value, text]) => `<option value="${value}" ${values[key] === value ? 'selected' : ''}>${text}</option>`).join('')}</select></label>`;
  const input = (label, key, max) => `<label class="field">${label}<input name="${key}" maxlength="${max}" value="${esc(values[key])}"></label>`;
  return `<div class="page-heading"><div><h1>长期旅行偏好</h1><p class="muted">这些设置会预填到下次新建的旅行，你可以在每次旅行里调整。</p></div></div>
    <form class="card form-card" id="travel-profile">
      <div class="field-grid">
        ${select('默认节奏', 'pace', [['relaxed','悠闲慢游'],['balanced','刚刚好'],['full','充实探索']])}
        ${select('默认交通', 'transport', [['mixed','步行 + 公共交通'],['walking','尽量步行']])}
        ${select('默认住宿取向', 'hotel_preference', [['balanced','衔接便利'],['value','预算优先'],['riverside','体验优先']])}
        <label class="field">每间每晚住宿预算（元）<input type="number" name="hotel_budget" min="100" max="5000" value="${esc(values.hotel_budget)}" required></label>
      </div>
      ${pacingFields(normalizePacing(values), true)}
      ${input('常去的景点类型', 'interests', 500)}
      ${input('自定义兴趣', 'custom_interests', 300)}
      ${input('习惯的旅行节奏', 'custom_pace', 300)}
      ${input('长期交通需求', 'custom_transport', 300)}
      ${input('长期住宿需求', 'custom_hotel', 300)}
      <div class="actions">${button('返回本次偏好', 'constraints', 'secondary', 'type="button"')}${button('重新读取', 'profile', 'secondary', 'type="button"')}<button class="btn" type="submit">保存长期偏好</button></div>
    </form>`;
}

function currentAttractionPreview() {
  return attractionPreview?.path === attractionDraftPath() && !dirty() ? attractionPreview : null;
}

function attractionDraftPath() {
  return S.trip ? `/trips/${S.trip.id}/attractions/${S.trip.requirements_revision}` : null;
}

function adoptAttractionDraft(path, saved) {
  attractionPreview = saved ? {
    path, options:saved.options, webNotice:saved.web_notice,
    revision:saved.selection_revision, selected:new Set(saved.selected_ids),
    pool:[...new Map([...(saved.candidate_pool || saved.options),...(saved.selected_items || [])].map(place=>[place.id,place])).values()],
    exhausted:!!saved.exhausted,
    batchNumber:saved.batch_number || 1,
    seenIds:new Set(saved.seen_ids || saved.options.map(place => place.id)),
    seenCount:(saved.seen_ids || []).length,
    rankingNotice:saved.ranking_notice,
  } : null;
}

async function restoreAttractionDraft() {
  const path = attractionDraftPath();
  if (path && !dirty()) adoptAttractionDraft(path, await api(path));
}

function conflictingSelectedExperiences(preview) {
  const groups = preview.pool.filter(place=>preview.selected.has(place.id))
    .map(place=>place.experience_group || place.id);
  return new Set(groups).size !== groups.length;
}

function attractions() {
  const preview = currentAttractionPreview();
  const options = preview?.options || [];
  const selected = (preview?.pool || []).filter(place => preview.selected.has(place.id));
  const duplicateSelection = preview && conflictingSelectedExperiences(preview);
  const earlier = (preview?.pool || []).filter(place => preview.seenIds.has(place.id)
    && !options.some(current => current.id === place.id));
  const cards = preview?.options.map(place => {
    const chosen = preview.selected.has(place.id);
    const query = encodeURIComponent(`${S.draft.city} ${place.name} 景点介绍`);
    const photos = [...new Set(place.photos || [])].slice(0,5);
    const gallery = photos.length ? `<div class="attraction-media">
      <div class="photo-gallery" data-photo-gallery>${photos.map((url,index) => `<figure class="photo-frame"><a href="${esc(url)}" target="_blank" rel="noopener noreferrer"><img src="${esc(url)}" alt="${esc(place.name)}实景照片 ${index+1}" loading="lazy" decoding="async" referrerpolicy="no-referrer"><span class="photo-fallback" hidden>照片暂时无法加载</span></a></figure>`).join('')}</div>
      <div class="photo-controls"><span>高德照片 · <span data-photo-counter>1</span> / ${photos.length}</span>${photos.length > 1 ? `<div><button class="photo-arrow" data-action="photo-prev" aria-label="${esc(place.name)}上一张照片">←</button><button class="photo-arrow" data-action="photo-next" aria-label="${esc(place.name)}下一张照片">→</button></div>` : ''}</div>
      ${photos.length < 3 ? `<p class="photo-shortage">目前仅取得 ${photos.length} 张实景照片</p>` : ''}</div>` : `<div class="attraction-media photo-placeholder"><span>暂无可用实景照片</span><small>可以点开景点资料继续了解</small></div>`;
    const sources = (place.web_sources || []).map(source =>
      `<li><a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title)}</a></li>`).join('');
    return `<article class="card attraction-card${chosen ? ' attraction-card-selected' : ''}">
      ${gallery}<div class="attraction-content">
      <div class="attraction-card-head"><h2>${esc(place.name)}</h2>${place.rating != null ? tag('高德评分 '+esc(place.rating)) : ''}</div>
      <p class="muted small">${esc(place.area || place.address || S.draft.city)}</p>
      ${place.web_summary ? `<p class="attraction-summary">${esc(place.web_summary)}</p>` : '<p class="muted small">暂未取得可靠的景点介绍。</p>'}
      ${place.audience ? `<p class="attraction-audience"><strong>适合</strong> ${esc(place.audience.replace(/^适合[：:\s]*/,''))}</p>` : '<p class="muted small">资料不足，适合人群待确认。</p>'}
      ${place.recommendation_reason ? `<p class="muted small">推荐理由 · ${esc(place.recommendation_reason)}</p>` : ''}
      ${sources ? `<details class="attraction-sources"><summary>${place.web_summary ? '介绍来源' : '网页资料'}</summary><ul>${sources}</ul></details>` : ''}
      <div class="actions attraction-card-actions"><a href="https://www.baidu.com/s?wd=${esc(query)}" target="_blank" rel="noopener noreferrer">自行查资料 ↗</a>
      ${button(chosen ? '已选为必去' : '选为必去', 'toggle-attraction', chosen ? '' : 'secondary', `data-id="${esc(place.id)}" aria-pressed="${chosen}" aria-label="${chosen ? '取消选择' : '选为必去'}：${esc(place.name)}"`)}</div></div></article>`;
  }).join('');
  const toolbar = preview ? `<div class="attraction-toolbar">
    <div class="attraction-selection" aria-live="polite"><strong>已选 ${selected.length} / 5</strong>
    ${selected.length ? `<div class="selected-chips">${selected.map(place=>`<button data-action="toggle-attraction" data-id="${esc(place.id)}" aria-label="取消选择：${esc(place.name)}">${esc(place.name)} ×</button>`).join('')}</div>` : '<p>换批会保留已选景点。</p>'}</div>
    <div class="actions">${button(preview.exhausted ? '本轮候选已看完' : '换一批 · 5 个', 'next-attractions', 'secondary', preview.exhausted ? 'disabled' : '')}
    ${button(`确认已选 ${selected.length} 个并生成`, 'confirm-attractions', '', selected.length && !duplicateSelection ? '' : 'disabled')}</div>
  </div>` : '';
  const history = earlier.length ? `<details class="attraction-history"><summary>回看已浏览的 ${earlier.length} 个景点</summary>
    <div>${earlier.map(place => `<article><div><strong>${esc(place.name)}</strong><p class="muted small">${esc(place.web_summary || place.recommendation_reason || place.area)}</p></div>
      ${button(preview.selected.has(place.id) ? '取消选择' : '选为必去', 'toggle-attraction', 'secondary tiny', `data-id="${esc(place.id)}" aria-pressed="${preview.selected.has(place.id)}" aria-label="${preview.selected.has(place.id) ? '取消选择' : '选为必去'}：${esc(place.name)}"`)}</article>`).join('')}</div></details>` : '';
  const empty = S.busy
    ? '<div class="busy-panel" role="status"><span class="spinner"></span>正在准备选景候选和网页资料…</div>'
    : `<div class="empty"><h2>还没有当前需求的选景草稿</h2><p>可以查询景点，或先回去调整旅行偏好。</p><div class="actions">${button('返回偏好', 'constraints', 'secondary')}${button('查询景点', 'choose-attractions')}</div></div>`;
  return `<div class="page-heading"><div><p class="eyebrow">DISCOVER YOUR NEXT STOP</p><h1>先挑喜欢的，再安排旅程。</h1><p class="muted">每批最多 5 个，结合你的偏好、景点评分和城市代表性推荐。</p></div>${button('调整旅行偏好', 'attraction-preferences', 'secondary')}</div>
    ${preview?.webNotice ? `<p class="notice">${esc(preview.webNotice)}</p>` : ''}
    ${preview?.rankingNotice ? `<p class="notice">${esc(preview.rankingNotice)}</p>` : ''}
    ${attractionTask && S.busy ? `<div class="busy-panel" role="status"><span class="spinner"></span>${esc(attractionTask)}</div>` : ''}
    ${preview?.exhausted ? '<p class="notice">本轮检索到、符合偏好的候选已展示完，不代表整座城市没有其他值得去的景点。可以回看已浏览的景点，或调整偏好重新查询。</p>' : ''}
    ${duplicateSelection ? '<p class="notice">保留的选择中有景点属于同一游玩内容，请只保留其中一个，再确认生成。</p>' : ''}
    ${S.draft.must_visit?.length ? `<p class="muted small">手填必去景点也会保留：${S.draft.must_visit.map(esc).join('、')}</p>` : ''}
    ${preview ? `<p class="muted small">第 ${preview.batchNumber} 批 · 已看 ${preview.seenCount || options.length} 个 · 勾选自动保存</p>
      <section class="attraction-workspace"><div class="attraction-grid">${cards || '<p class="muted">本轮没有新的候选，可以查看已保留的选择。</p>'}</div>${history}${toolbar}</section>` : empty}`;
}

function moveAttractionPhoto(el, step) {
  const media = el.closest('.attraction-media');
  const gallery = media.querySelector('[data-photo-gallery]');
  const count = gallery.children.length;
  const index = (Math.round(gallery.scrollLeft / gallery.clientWidth) + step + count) % count;
  gallery.scrollTo({left:index * gallery.clientWidth, behavior:'smooth'});
}

document.addEventListener('scroll', event => {
  const gallery = event.target;
  if (!gallery.matches?.('[data-photo-gallery]') || !gallery.clientWidth) return;
  const counter = gallery.closest('.attraction-media').querySelector('[data-photo-counter]');
  counter.textContent = String(Math.round(gallery.scrollLeft / gallery.clientWidth) + 1);
}, true);
document.addEventListener('error', event => {
  const image = event.target;
  if (!image.matches?.('.photo-frame img')) return;
  image.hidden = true;
  image.nextElementSibling.hidden = false;
}, true);
function hotelCard(h,index){return `<article class="card hotel-card ${S.draft.hotel_id===h.id?'selected':''}"><div class="hotel-art" aria-hidden="true">0${index+1}</div><div class="section-title">${tag(index===0?'位置推荐':'备选住宿')}${S.draft.hotel_id===h.id?tag('已手选'):''}</div><h3>${esc(h.name)}</h3><p class="muted small">${esc(h.area||'')} · ${esc(h.address)}</p><p class="small subtle">${esc(h.selection_reason||'按真实地点的位置衔接推荐；房价与空房待确认。')}</p><div class="price">房价待查询 <small>/ 非实时预订系统</small></div><p class="muted small">${Math.ceil(S.draft.party_size/2)}间 × ${nightsFor()}晚，按上限预留 ${money(S.draft.hotel_budget*Math.ceil(S.draft.party_size/2)*nightsFor())}</p><ul>${(h.benefits||['来自高德地点查询','需核实房型、空房和取消规则']).map(t=>`<li>${esc(t)}</li>`).join('')}</ul>${button(S.draft.hotel_id===h.id?'已选择此住宿':'选择这家住宿','hotel','secondary',`data-id="${esc(h.id)}"`)}</article>`;}
function hotels(){const hotels=currentHotels();return `<div class="page-heading"><div><p class="eyebrow">A PLACE TO RETURN TO</p><h1>给旅途，选一个落脚点。</h1><p class="muted">${esc(S.draft.city)} · ${daysLabel()} · ${S.draft.party_size}人 · 每间每晚预算 ${money(S.draft.hotel_budget)}</p></div><div class="actions">${button('调整住宿预算','constraints','secondary')}${button('查询真实住宿 ↗','query-hotels', '',!S.config.map_configured||!nightsFor()?'disabled':'')}</div></div><div class="notice">自动推荐主要核对酒店位置与路线衔接。高德地点搜索不保证实时房价、空房、早餐或房型，预算上限仅作为预留。</div>${!nightsFor()?`<div class="empty"><span class="empty-mark">↗</span><h2>这趟行程无需住宿</h2><p>可以直接进入每日安排。</p>${button('进入每日安排','itinerary')}</div>`:hotels.length?`<div class="actions">${button('让系统自动推荐','auto-hotel','ghost')}${S.draft.hotel_id?tag('手选住宿将在重新规划时保留'):tag('自动推荐已开启')}</div><div class="grid3" style="margin-top:20px">${hotels.slice(0,6).map(hotelCard).join('')}</div><div class="actions">${button('按当前住宿生成行程 →','generate')}</div>`:`<div class="empty"><span class="empty-mark">⌂</span><h2>${S.config.map_configured?'从真实酒店候选开始':'接入地图后，查询真实酒店'}</h2><p>系统会查询目的地酒店，比较与你出发地点、固定预约的距离。这里不会放置虚构酒店或占位房价。</p>${button(S.config.map_configured?'开始查询住宿':'查看接入设置',S.config.map_configured?'query-hotels':'settings')}</div>`}`;}
function costPanel(plan) {
  const cost = plan.api_cost_estimate || plan.api_cost_preview;
  if (!cost) return '<p class="muted small">此记录未保存人民币费用估算。</p>';
  const cash = value => typeof value === 'string' && /^\d+\.\d{1,8}$/.test(value) ? '¥'+esc(value.replace(/0+$/,'').replace(/\.$/,'.00')) : '未知';
  const amount = part => !part || part.status === 'unavailable' ? '暂不可算' : part.status === 'estimated' ? '约 '+cash(part.min_cny) : cash(part.min_cny)+' – '+cash(part.max_cny);
  const legacy = cost.basis === 'current_tariff_legacy_assumption';
  return `<div class="usage-cost"><h3>API 费用估算 · 人民币</h3>${legacy ? '<p class="small">旧记录按当前官方 DeepSeek 价格回算，假设使用官方接口；缓存拆分、逐次时段及搜索是否计费未记录，以下是条件区间，并非历史扣费。</p>' : ''}
    <div class="cost-row"><span>模型调用</span><strong>${amount(cost.models)}</strong></div>
    <div class="cost-row"><span>定向网页搜索</span><strong>${amount(cost.search)}</strong></div>
    <div class="cost-row"><span>模型＋搜索小计</span><strong>${amount(cost.subtotal)}</strong></div>
    ${cost.subtotal?.status === 'unavailable' ? `<p class="notice">${esc((cost.model_calls || []).filter(part=>part.status === 'unavailable').map(part=>part.reason).filter(Boolean).join('；') || '部分用量或价格未知，无法给出完整小计。')}</p>` : ''}
    <p class="muted small">不含地图服务、之前选景及账户其他调用。按记录的价格估算，实际扣费以供应商账单为准。</p>
    <details><summary>计算口径与价格版本</summary><p class="small">模型费用＝（缓存命中 tokens × 命中价＋未命中 tokens × 未命中价＋输出 tokens × 输出价）÷ 1,000,000；各单价单位为元/百万 tokens。思考已包含在输出中，不再加一次。搜索仅成功且非空计费。</p><p class="muted small">${esc(cost.tariff?.version || '未记录')} · 核对 ${esc(cost.tariff?.verified_date || '未记录')} · 按北京时间判断时段，节假日历不足时给区间。</p><ul class="small">${Object.entries(cost.tariff?.models || {}).map(([model,tiers])=>`<li>${esc(model)}：空闲 ${esc((tiers.off_peak||[]).join(' / '))}；高峰 ${esc((tiers.peak||[]).join(' / '))}（依次为命中、未命中、输出价）</li>`).join('')}</ul><a class="small" href="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/" target="_blank" rel="noopener noreferrer">查看官方价格 ↗</a></details></div>`;
}
function usagePanel(plan) {
  const observation = plan.usage_observation;
  const stats = plan.usage;
  const valid = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const number = value => valid(value) ? value.toLocaleString('zh-CN') : '未记录';
  const noCalls = observation?.model_http_attempts === 0;
  const calls = observation?.model_calls || [];
  const partial = observation?.usage_complete === false;
  const tokens = key => {
    if (!partial) return stats?.[key] ?? (noCalls ? 0 : null);
    const known = calls.map(call=>call[key]).filter(valid);
    return known.length ? known.reduce((sum,value)=>sum+value,0) : null;
  };
  const input = tokens('input_tokens');
  const output = tokens('output_tokens');
  const total = valid(input) && valid(output) ? input + output : null;
  const requests = stats?.requests ?? (noCalls ? 0 : null);
  const budget = observation?.budget;
  const progress = (label, actual, limit, unit) => `<div class="usage-progress"><span>${label} <strong>${number(actual)} / ${number(limit)} ${unit}</strong></span>${valid(actual) && valid(limit) && limit > 0 ? `<progress max="${limit}" value="${Math.min(actual,limit)}" aria-label="${label}"></progress>` : ''}</div>`;
  const reason = {stop:'正常结束',tool_calls:'调用工具 / 结构化结果',function_call:'调用工具',length:'触及长度限制',content_filter:'内容过滤',insufficient_system_resource:'供应商资源不足',other:'其他结束原因'};
  return `<section class="card usage-panel"><div class="section-title"><h2>模型用量与预算</h2>${tag('本轮规划')}</div>
    <p class="muted small">${esc(plan.model || '模型未记录')} · 仅统计这次规划及内部研究、校验协商、工具往返与修订；不包含之前的选景推荐。</p>
    <div class="usage-totals">${[['模型请求',requests],['输入 tokens'+(partial?'（已记录）':''),input],['输出 tokens'+(partial?'（已记录）':''),output],['合计 tokens'+(partial?'（已记录）':''),total]].map(([label,value])=>`<div><span>${label}</span><strong>${number(value)}</strong></div>`).join('')}</div>
    ${valid(total) && total > 0 ? `<div class="usage-stack" role="img" aria-label="输入${input}、输出${output} tokens"><span style="width:${input/total*100}%"></span><span style="width:${output/total*100}%"></span></div><p class="small muted">输入占 ${Math.round(input/total*100)}% · 输出占 ${Math.round(output/total*100)}%（这是用量构成，不是预算百分比）</p>` : ''}
    ${costPanel(plan)}
    ${budget ? `<div class="usage-budget">${progress('模型请求',requests,budget.max_model_requests,'次')}${progress('整轮耗时',observation.elapsed_seconds,budget.run_timeout_seconds,'秒')}</div>
      <p class="small muted">单次输出上限：排程 ${number(budget.planner_max_output_tokens)} / 研究 ${number(budget.research_max_output_tokens)}${budget.reviewer_max_output_tokens != null ? ' / 校验 '+number(budget.reviewer_max_output_tokens) : ''} tokens。输入 ${budget.input_token_limit == null ? '未设硬上限' : '上限 '+number(budget.input_token_limit)+' tokens'}。这些是本轮启动时的预算。</p>` : '<p class="small muted">这条历史记录未保存预算与逐次明细，不使用当前预算替代历史预算。</p>'}
    ${observation?.usage_complete === false ? '<p class="notice">部分请求未取得完整用量，总量可能不完整；明细中“未返回”不能当作 0。</p>' : ''}
    ${calls.some(call=>call.finish_reason === 'length') ? '<p class="notice">有请求返回 length，输出可能被截断；可能触及输出或上下文长度限制，请结合本轮结果检查。</p>' : ''}
    ${observation ? `<p class="small muted">模型 HTTP 尝试 ${number(observation.model_http_attempts)} 次；SDK 请求数和 HTTP 尝试数在接口错误时可能不同。</p>` : ''}
    ${calls.length ? `<details><summary>查看每次调用明细（${calls.length}次）</summary><div class="usage-table-wrap"><table class="usage-table"><caption>供应商逐次返回的用量；思考属于输出、缓存命中属于输入，不重复相加。</caption><thead><tr>${['序号 / 角色','输入','输出','思考（输出内）','缓存命中（输入内）','缓存未命中（输入内）','耗时','结束情况','输出上限'].map(label=>`<th scope="col">${label}</th>`).join('')}</tr></thead><tbody>${calls.map(call=>`<tr><th scope="row">${number(call.number)} · ${{planner:'排程',research:'研究',reviewer:'校验'}[call.role] || '未记录'}</th><td>${valid(call.input_tokens)?number(call.input_tokens):'未返回'}</td><td>${valid(call.output_tokens)?number(call.output_tokens):'未返回'}</td><td>${valid(call.reasoning_tokens)?number(call.reasoning_tokens):'未返回'}</td><td>${valid(call.cache_read_tokens)?number(call.cache_read_tokens):'未返回'}</td><td>${valid(call.cache_miss_tokens)?number(call.cache_miss_tokens):'未返回'}</td><td>${valid(call.elapsed_ms)?number(call.elapsed_ms/1000)+'秒':'未记录'}</td><td>${esc(call.status === 'http_error' ? 'HTTP '+number(call.http_status) : call.status === 'no_complete_response' ? '未收到完整响应' : reason[call.finish_reason] || '未返回结束原因')}</td><td>${number(call.max_output_tokens)}</td></tr>`).join('')}</tbody></table></div></details>` : ''}
    <p class="muted small">地图请求 ${number(plan.map_calls)} 次 · 定向网页搜索 ${number(plan.research_search_calls)} 次（本轮计数；工具服务费用另计）。</p>
    <p class="muted small">总量来自模型 API usage，由 SDK 累加，不按字数估算。历史上下文和工具结果每次发送都会计入输入。用量不等于最终账单，失败响应可能未返回统计。</p>
  </section>`;
}
function currentBudgetPanel() {
  const budget = S.config.planning_budget;
  if (!budget) return '';
  return `<section class="card usage-panel"><h2>当前规划预算</h2><p>排程、研究、校验三个角色共享 ${esc(budget.max_model_requests)} 次模型请求 / ${esc(budget.run_timeout_seconds)} 秒。</p><p>单次输出：排程 ${esc(budget.planner_max_output_tokens)}、研究 ${esc(budget.research_max_output_tokens)}、校验 ${esc(budget.reviewer_max_output_tokens ?? '未记录')} tokens；输入未设硬上限。</p><p class="muted small">每次规划会保存自己的预算和用量，之后修改配置不会改变历史记录。</p></section>`;
}
function reviewPanel(plan) {
  const review = plan.review;
  if (!review) return '';
  const disputed = hasReviewDisagreement(plan);
  const status = ({pending:'尚未开始校验',awaiting_planner:'等待规划回应',awaiting_adjudication:'等待校验复核',
    approved:'校验认同',disputed:'规划分歧 · 待你协调',incomplete:'协商未完成'})[review.status] || '校验记录未完成';
  const places = new Map((plan.places || []).map(place => [place.id,place.name]));
  const positions = values => (values || []).map(position =>
    `第 ${esc(position.day)} 天 · 第 ${esc(position.stop_number)} 站 · ${esc(places.get(position.place_id) || position.place_id)}`).join('；');
  const initial = review.initial?.suggestions || [];
  const final = review.final?.suggestions || [];
  const decisions = review.planner_response?.decisions || [];
  const renderSuggestion = (suggestion, introduced=false) => {
    const decision = decisions.find(item => item.suggestion_id === suggestion.suggestion_id);
    const unresolved = final.find(item => item.suggestion_id === suggestion.suggestion_id);
    return `<article class="review-opinion"><h3>${introduced ? '复核发现的新问题' : '校验建议'} · ${esc(suggestion.suggestion_id)}</h3>
      <p class="review-position">${introduced ? '复核引用位置' : '原草稿位置'}：${positions(suggestion.positions)}</p>
      <dl class="review-exchange"><div><dt>校验 Agent</dt><dd><p>${esc(suggestion.concern)}</p><p class="small"><strong>依据：</strong>${esc(suggestion.requirement_basis)}</p><p class="small"><strong>建议：</strong>${esc(suggestion.proposed_change)}</p></dd></div>
      ${decision ? `<div><dt>规划 Agent</dt><dd><p><strong>${decision.action === 'revise' ? '采纳并修订' : '保留原安排'}</strong></p><p>${esc(decision.reason)}</p></dd></div>` : '<div><dt>规划 Agent</dt><dd class="muted">尚无逐项回应记录。</dd></div>'}
      ${unresolved ? `<div class="review-unresolved"><dt>仍有分歧</dt><dd><p>${esc(unresolved.concern)}</p><p class="small">引用位置：${positions(unresolved.positions)}。若站点已被移除，请对照原草稿。</p><p class="small"><strong>依据：</strong>${esc(unresolved.requirement_basis)}</p><p class="small"><strong>建议：</strong>${esc(unresolved.proposed_change)}</p></dd></div>`
        : review.status === 'approved' ? '<div><dt>复核结果</dt><dd>校验 Agent 已认同当前安排及解释。</dd></div>' : ''}</dl></article>`;
  };
  const draftDetails = (draft, title) => {
    if (!draft?.stops?.length) return '';
    const days = [...new Set(draft.stops.map(stop => stop.day))];
    return `<details class="review-draft"><summary>${title}</summary><p>${esc(draft.summary)}</p><p class="small muted">以下为未通过完整流程的草稿，仅供核对，不能直接接受。</p>${days.map(day => `<section><h3>第 ${esc(day)} 天</h3><ol>${draft.stops.filter(stop => stop.day === day).map((stop,index) => `<li><strong>第 ${index+1} 站 · ${esc(stop.start_time)}–${esc(stop.end_time)} · ${esc(places.get(stop.place_id) || stop.place_id)}</strong><span class="small">${esc(({activity:'城市体验',booking:'固定预约',meal:'用餐建议',hotel:'住宿安排'})[stop.kind] || stop.kind)}</span><p>${esc(stop.reason)}</p></li>`).join('')}</ol></section>`).join('')}</details>`;
  };
  const effort = plan.thinking_configuration?.reviewer?.effort;
  const stage = ({initial_review:'首次校验',planner_response:'规划回应',final_review:'校验复核'})[review.pending_stage];
  return `<section class="card review-panel"><div class="section-title"><h2>行程校验与协商</h2>${tag(status,review.status !== 'approved')}</div>
    ${effort ? `<p class="muted small">本轮校验 Agent · 思考强度 ${esc(effort)}</p>` : ''}
    ${disputed ? '<div class="notice">双方仍有规划分歧，需要你协调。这是模型意见不一致，不代表需求已被证明无解。当前草稿已保存，尚不可接受；协调后的继续处理方式待讨论。</div>'
      : review.status !== 'approved' ? `<p class="notice">校验协商尚未完成${stage ? '，停在'+stage : ''}，暂不可接受。预算或接口中断不作为双方冲突。</p>` : '<p class="small">校验 Agent 已认同当前方案，接受仍需通过程序硬约束和版本检查。</p>'}
    ${review.initial?.summary ? `<p><strong>首次校验：</strong>${esc(review.initial.summary)}</p>` : ''}
    ${initial.map(suggestion => renderSuggestion(suggestion)).join('')}
    ${final.filter(suggestion => !initial.some(item => item.suggestion_id === suggestion.suggestion_id)).map(suggestion => renderSuggestion(suggestion,true)).join('')}
    ${review.final?.summary ? `<p><strong>校验复核：</strong>${esc(review.final.summary)}</p>` : ''}
    ${!isReadyPlan(plan) ? draftDetails(plan.draft,'查看最新完整草稿（尚不可接受）') : ''}
    ${disputed && review.planner_response?.revised_itinerary ? draftDetails(review.original_draft,'对照首次校验时的原草稿') : ''}
  </section>`;
}
function researchPanel(plan) {
  const requests = plan.research_requests || [], diagnostics = plan.research_diagnostics || [];
  if (!requests.length && !diagnostics.length) return '';
  const names = {opening_hours:'开放时段',last_admission:'最晚入场时间',reservation_rules:'预约规则'};
  const places = new Map((plan.places || []).map(p=>[p.id,p.name]));
  return `<section class="card usage-panel"><h2>资料补查记录</h2><details><summary>查看补查原因与逐字段诊断（${diagnostics.length}条）</summary>
    ${requests.map((request,index)=>`<h3>补查请求 ${index+1}</h3><p>${esc(request.reason)}</p><ul>${(request.needs || []).map(need=>`<li>第 ${esc(need.day)} 天 · ${esc(places.get(need.place_id) || need.place_id)} · ${(need.fields || []).map(field=>esc(names[field] || field)).join('、')}<p>${esc(need.question)}</p></li>`).join('')}</ul>`).join('')}
    ${diagnostics.length ? `<ol>${diagnostics.map(d=>`<li><strong>第 ${esc(d.day)} 天 · ${esc(places.get(d.place_id) || d.place_id)} · ${esc(names[d.field] || d.field)}</strong><p>${esc(d.origin === 'model' ? '研究 Agent 判断（待核实）' : '程序检测')}${d.attempt ? ' · 第 '+esc(d.attempt)+' 次核检' : ''}${d.resolved ? ' · 已解决' : d.severity === 'warning' ? ' · 提示' : ''}</p><p>${esc(d.message)}</p>${d.actual == null ? '' : `<p class="small">提取/返回值：${esc(Array.isArray(d.actual)?d.actual.join('、'):String(d.actual))}</p>`}</li>`).join('')}</ol>` : '<p class="muted small">尚无逐字段诊断记录。</p>'}
    <p class="muted small">来源可追溯不等于现实已核实；研究判断不当作程序已确认的事实。预算耗尽与资料未知分别显示。</p></details></section>`;
}
function savedDraftDetails(draft, places, title='查看未核实草稿') {
  if (!draft?.stops?.length) return '<p class="muted small">尚未保存行程草稿。</p>';
  const names = new Map((places || []).map(place=>[place.id,place.name]));
  return `<details class="saved-draft"><summary>${esc(title)}</summary><p class="notice">以下是过程草稿，时间和事实仍可能待核实，不能直接接受。</p><p>${esc(draft.summary)}</p><ol>${draft.stops.map(stop=>`<li><strong>第 ${esc(stop.day)} 天 · ${esc(stop.start_time)}–${esc(stop.end_time)} · ${esc(names.get(stop.place_id) || stop.place_id)}</strong><p>${esc(stop.reason)}</p></li>`).join('')}</ol></details>`;
}
function queryRecords(items, places) {
  if (!items?.length) return '<p class="muted small">尚无查询记录。</p>';
  const names = new Map((places || []).map(place=>[place.id,place.name]));
  return `<ol class="query-records">${items.map(item=>{
    const query=item.request || item.query || item, result=item.result || {}, kind=query.type || query.kind;
    const location=query.origin_id && query.destination_id
      ? (names.get(query.origin_id)||query.origin_id)+' → '+(names.get(query.destination_id)||query.destination_id)
      : names.get(query.place_id || query.anchor_id) || query.query || query.place_id || query.anchor_id || '指定查询';
    const label=({route:'路线',nearby_restaurants:'附近餐饮',place_search:'地点查询',search_place:'地点查询'})[kind] || '资料查询';
    const status=result.status || item.status;
    const cacheHit=item.cache_hit || result.cache_hit;
    const resultLabel=(cacheHit ? '复用已查结果 · ' : '')+(({ok:'已返回',success:'已返回',cached:'复用已查结果',error:'查询失败',invalid:'参数无效',budget_exhausted:'额度不足，未执行',duplicate:'复用已有结果',not_executed:'未执行'})[status] || status);
    const phase=({first_draft:'首稿',revision:'修订',review_revision:'协商修订',evidence:'核实资料'})[item.phase] || '';
    const callInfo=[item.model_round != null ? '模型第 '+item.model_round+' 轮' : '',phase,item.elapsed_ms != null ? item.elapsed_ms+'毫秒' : ''].filter(Boolean).join(' · ');
    const error=result.error?.message || result.error?.code || result.error || item.error?.message || item.error;
    return `<li><strong>${esc(label)} · ${esc(location)}</strong>${query.transport?`<span class="small"> · ${esc(({mixed:'综合交通',walking:'步行'})[query.transport] || query.transport)}</span>`:''}${query.reason?`<p>${esc(query.reason)}</p>`:''}${callInfo?`<p class="small muted">${esc(callInfo)}</p>`:''}${status?`<p class="small">${esc(resultLabel)}</p>`:''}${error?`<p class="danger small">${esc(error)}</p>`:''}</li>`;
  }).join('')}</ol>`;
}
function draftUncertainties(record) {
  const needs=Array.isArray(record.pending_information)?record.pending_information:record.pending_information?.needs || [];
  const assumptions=record.assumptions || [];
  if (!needs.length && !assumptions.length) return '';
  const names={opening_hours:'开放时段',last_admission:'最晚入场',reservation_rules:'预约规则'};
  return `<div class="small"><h4>待确认事项</h4><ul>${needs.map(need=>`<li>第 ${esc(need.day)} 天 · ${(need.fields || []).map(field=>esc(names[field] || field)).join('、')}<p>${esc(need.question)}</p></li>`).join('')}${assumptions.map(text=>`<li>${esc(text)}</li>`).join('')}</ul></div>`;
}
function draftQueryPanel(plan) {
  if (plan.planning_protocol !== 'draft_first') return '';
  return `<section class="card usage-panel"><h2>草稿与定向查询</h2><p class="muted small">先保留安排，再按结构化清单查询和修订；只有通过完整检查的候选可以接受。</p>${plan.draft && !plan.review?.initial ? savedDraftDetails(plan.draft,plan.places) : ''}${draftUncertainties(plan)}<details><summary>查看结构化查询记录</summary>${queryRecords(plan.query_log?.length?plan.query_log:plan.query_requests,plan.places)}</details></section>`;
}
function planningRunsPanel() {
  if (!S.trip) return '';
  return `<section class="card planning-runs"><div class="section-title"><div><h3>过程草稿</h3><p class="muted small">首稿和修订产生后保存，可在中断后回看；这里的草稿不能直接接受。</p></div>${button(S.planningRunsOpen?'收起过程草稿':'查看过程草稿','planning-runs','secondary tiny')}</div>${S.planningRunsOpen
    ? S.planningRuns.length ? S.planningRuns.map(run=>{
      const stale=run.requirements_revision !== S.trip.requirements_revision || run.base_version !== S.trip.accepted_version;
      const status=({running:'执行尚未结束',completed:'流程已完成',incomplete:'流程未完成',interrupted:'执行已中断'})[run.status] || '未完成记录';
      return `<article class="saved-run"><h4>${esc(run.requirements?.city || '旅行')} · ${esc(run.requirements?.date || '')}</h4><p class="muted small">${esc(displayTime(run.updated_at || run.created_at))} · 依据需求 v${esc(run.requirements_revision)}</p>${tag(status,run.status!=='completed')}${stale?tag('基于旧需求或旧行程',true):''}${run.error?`<p class="danger">${esc(run.error)}</p>`:''}${run.status==='running'?'<p class="muted small">若服务已经退出，此记录不会自动继续执行。</p>':''}${savedDraftDetails(run.draft,run.places)}${draftUncertainties(run)}<details><summary>查看本次查询清单与结果</summary>${queryRecords(run.query_log?.length?run.query_log:run.query_requests,run.places)}</details>${run.candidate_id?'<p class="muted small">最终结果已存入规划记录，请在那里查看候选及检查状态。</p>':''}</article>`;
    }).join('') : '<p class="muted small">尚无过程草稿记录，旧候选仍可在规划记录查看。</p>'
    : ''}</section>`;
}
function pacingSummary(plan) {
  if (!plan.daily_metrics?.length) return '<p class="muted small">此记录未保存每日节奏统计。</p>';
  const cell = (value, limit, unit) => `${value == null ? '未核实' : esc(Number(value.toFixed(2))) + unit} / ${limit == null ? '不限' : esc(limit) + unit}`;
  return `<h3>每日节奏 · 实际 / 上限</h3><div class="pacing-metrics">${plan.daily_metrics.map(row=>`<section class="pacing-day"><strong>第 ${esc(row.day)} 天${isReadyPlan(plan)?'':' · 草稿'}</strong><dl>
    <div><dt>景点数</dt><dd>${cell(row.attraction_count,row.limits.max_attractions,'个')}</dd></div>
    <div><dt>游玩时长</dt><dd>${cell(row.sightseeing_minutes,row.limits.max_sightseeing_minutes,'分钟')}</dd></div>
    <div><dt>游玩占比</dt><dd>${cell(row.sightseeing_ratio,row.limits.max_sightseeing_ratio,'%')}</dd></div>
    <div><dt>地图交通</dt><dd>${cell(row.transport_minutes,row.limits.max_transport_minutes,'分钟')}</dd></div>
    </dl><small class="muted">出发至结束窗口 ${esc(row.available_minutes)} 分钟；交通缓冲另留。</small></section>`).join('')}</div>`;
}
function itinerarySummary(plan, requirements, issues) {
  const stops = isReadyPlan(plan) ? plan.itinerary?.stops || [] : [];
  const places = new Map((plan.places || []).map(place => [place.id, place.name]));
  const required = plan.must_visit_ids?.map(id => {
    const stop = stops.find(item => item.place_id === id && ['activity', 'booking'].includes(item.kind));
    return `<li>${esc(places.get(id) || '必去景点')} · ${stop ? '第 ' + stop.day + ' 天' : '未安排'}</li>`;
  }).join('');
  const pending = (requirements.must_visit || []).map(name => `<li>${esc(name)} · 暂无安排记录</li>`).join('');
  const check = planningOutcomeLabel(plan);
  return `<section class="card summary-card">
    <div class="section-title"><h2>行程摘要</h2>${tag(check, !isReadyPlan(plan))}</div>
    ${plan.revision_notes ? `<p><strong>本次修改要求</strong> ${esc(plan.revision_notes)}</p>` : ''}
    ${required || pending ? `<h3>必去景点</h3><ul>${required || pending}</ul>` : ''}
    ${planningOutcomeMessage(plan) ? `<p class="small">${esc(planningOutcomeMessage(plan))}</p>` : ''}
    ${issues.length ? `<div class="notice danger">${issues.map(esc).join('<br>')}</div>` : ''}
    ${pacingSummary(plan)}
    ${!isReadyPlan(plan) && plan.draft ? '<p class="muted small">草稿已保存，尚不可接受。你可以导出当前记录查看未完成草稿。</p>' : ''}
  </section>`;
}
function itinerary(){const p=shownPlan(),snapshot=planRequirements(p),d=p?(snapshot||{city:'历史',date:'原日期未保存',days:Math.max(1,...(p.itinerary?.stops||[]).map(s=>s.day||1)),party_size:'未记录'}):S.draft;const busy=S.busy?`<div class="busy-panel"><span class="spinner"></span>正在查询地点与路线、安排住宿和活动，请稍候…</div>`:'';const heading=`<div class="page-heading"><div><p class="eyebrow">DAY BY DAY / 一天一天，慢慢出发</p><h1>${esc(d.city)}，${daysLabel(d)}的旅行安排</h1><p class="muted">${esc(d.date)}出发 · ${d.party_size}人 · ${p?'依据需求 v'+p.requirements_revision:S.trip?'需求 v'+S.trip.requirements_revision:'尚未保存需求'}</p></div><div class="actions">${button('修改旅行偏好','preferences','secondary')}${button(p?'用 DeepSeek 重新规划':'用 DeepSeek 生成行程','generate')}</div></div>`;
if(!p)return heading+planningRecords()+planningRunsPanel()+busy+`<div class="empty"><span class="empty-mark">↗</span><h2>一份属于你的行程，还差一步。</h2><p>${S.config.map_configured&&S.config.model_configured?'DeepSeek 将根据旅行偏好和高德查询结果生成候选，再由程序校验路线和时间。':'请在接入设置保存高德 Web 服务 Key 和 DeepSeek API Key。'}</p><div class="actions" style="justify-content:center">${button('查看旅行偏好','preferences','secondary')}${S.config.map_configured&&S.config.model_configured?'':button('去接入设置','settings')}</div></div>`;
const day=Math.min(S.day,d.days),stops=isReadyPlan(p)?(p.itinerary?.stops?.filter(s=>(s.day||1)===day)||[]):[];const cost=p.costs||{};const isAccepted=S.trip?.accepted_plan?.id===p.id;const eligible=canAccept(p);const issues=[...(p.error?[p.error]:[]),...(p.issues||[]).map(i=>planningIssueText(i,p))];
return heading+busy+(stalePlan(p)?'<div class="notice">旅行偏好已修改，下面保留的是原方案及原日期，不能作为新需求的结果接受。请重新规划。</div>':'')+`<div class="actions">${tag(p.data_mode)}${tag(stalePlan(p)?'旧方案 · 待重新规划':isAccepted?'已接受 v'+S.trip.accepted_version:isReadyPlan(p)?'候选 · 待确认':planningOutcomeLabel(p),!isReadyPlan(p))}${isReadyPlan(p) ? button('基于此方案修改','revise','secondary') : ''}${button('导出所示方案','export','tiny secondary')}</div>${itinerarySummary(p,d,issues)}${reviewPanel(p)}${researchPanel(p)}${draftQueryPanel(p)}${usagePanel(p)}${planningRecords()}${planningRunsPanel()}<div class="day-tabs">${Array.from({length:d.days},(_,i)=>`<button class="${day===i+1?'active':''}" aria-pressed="${day===i+1}" data-day="${i+1}">DAY 0${i+1}<small>${dateFor(i+1,snapshot||{})}</small></button>`).join('')}</div><div class="itinerary-layout"><section><div class="timeline">${stops.length?stops.map((s,index)=>{const place=p.places?.find(t=>t.id===s.place_id)||{};const leg=p.routes?.find(r=>r.day===day && (r.stop_index!=null?r.stop_index===index:r.destination_id===s.place_id && r.origin_id===(index?stops[index-1].place_id:r.origin_id)));return `<article class="stop"><div class="stop-time">${esc(s.start_time)}<small>至 ${esc(s.end_time)}</small></div><div>${tag(({activity:'城市体验',booking:'固定预约',meal:'用餐建议',hotel:'住宿安排'})[s.kind]||'活动')}<h3>${esc(stopHeading(s,place))}</h3><p>安排建议：${esc(s.reason)}</p><div class="stop-meta">${esc(place.address||'')} · ${esc(place.source||'来源待确认')}</div>${leg?`<div class="leg">↗ 从上一站${esc(leg.mode)}约${esc(leg.duration_minutes)}分钟${leg.distance_meters?' · '+(leg.distance_meters/1000).toFixed(1)+'公里':''} · ${esc(leg.source)}</div>`:''}</div></article>`;}).join(''):'<p class="muted">当前没有可接受安排，请查看上方处理说明。</p>'}</div><div class="card" style="margin-top:20px"><h3>本次查询与检查</h3><ol class="trace">${(p.trace||[]).filter(t=>t.detail).map(t=>`<li>${esc(t.detail)}</li>`).join('')}</ol></div></section><aside class="card summary-card"><h3>这趟旅行的预留</h3><p class="muted small">全体${d.party_size}人 · ${daysLabel(d)}</p>${[['住宿预算预留',cost.accommodation],['餐饮预算预留',cost.food],['门票实际费用',cost.tickets],['市内交通费用',cost.transport],['住宿＋餐饮预留',cost.subtotal],['你的整趟预算',cost.budget]].map(([a,b])=>`<div class="cost-row"><span>${a}</span><strong>${money(b)}</strong></div>`).join('')}<p class="muted small">${esc(cost.scope||'费用未核实')}</p><div class="divider"></div><h3>住宿方案</h3><p class="small">${esc(p.hotel?.name||'当日往返 / 尚未确定')}</p>${button('查看住宿详情','hotels','secondary tiny')}<div class="divider"></div><h3>出发前再确认</h3><ul>${(p.warnings||[]).map(t=>`<li>${esc(t)}</li>`).join('')}</ul>${!isAccepted?button('接受当前候选行程','accept','',!eligible?'disabled':''):tag('当前已接受方案已保存')}<p class="muted small">接受只保存本应用内的版本，不代表完成酒店或门票预订。</p></aside></div>`;}
function settings(){const c=S.config;return `<div class="page-heading"><div><p class="eyebrow">CONNECTED, WITH CLEAR BOUNDARIES</p><h1>接入真实信息，开始真实规划。</h1><p class="muted">在本机工作台填写 Key，即可开始联调。</p></div>${button('刷新配置状态','refresh','secondary')}</div><div class="settings-grid"><section class="card"><h2>配置 API Key</h2>
<form id="provider-keys" method="post" autocomplete="off">
  <label class="field">高德 Web 服务 Key<input id="amap-key" type="password" maxlength="512" autocomplete="off"></label>
  <label class="field">DeepSeek API Key<input id="deepseek-key" type="password" maxlength="512" autocomplete="off"></label>
  <p class="muted small">可以只填一个，空白项保留。网页填写的 Key 在服务重启后需要重新输入。</p>
  <button class="btn" type="submit">保存 API Key</button>
</form><div class="actions">${button('回到行程工作台 →','itinerary')}${button('查看旅行偏好','preferences','secondary')}</div></section><section class="card"><h2>两项服务，各司其职</h2><div class="status-card"><div><strong><i class="status-dot ${c.map_configured?'ok':''}"></i>高德 Web 服务</strong><p class="muted small">真实地点、酒店位置与路段耗时</p></div>${tag(c.map_configured?'已配置':'待配置',!c.map_configured)}</div><div class="status-card"><div><strong><i class="status-dot ${c.model_configured?'ok':''}"></i>DeepSeek</strong><p class="muted small">${esc(c.model)} · 理解额外要求与修订方案</p></div>${tag(c.model_configured?'已配置':'待配置',!c.model_configured)}</div><p class="muted small">“已配置”表示检测到本地Key，不代表接口权限或额度已经验证。</p><div class="divider"></div><h3>开始联合测试</h3><p class="muted small">先保存高德与 DeepSeek 的 Key，再确认旅行偏好。进入行程页后，点击「用 DeepSeek 生成行程」才会发起规划。</p></section></div>${currentBudgetPanel()}`;}
function revision() {
  const plan = shownPlan();
  if (plan?.status !== 'ready') return `<div class="notice">请先打开一份已通过检查的方案。${button('回到行程','itinerary','secondary')}</div>`;
  const input = planRequirements(plan);
  return `<div class="page-heading"><div><h1>修改这次行程</h1>
    <p class="muted">${esc(input?.city || '原行程')} · ${esc(displayTime(plan.created_at))}</p></div></div>
    <section class="card form-card">
      <p>以当前查看的方案为参考，结合已保存的旅行偏好，生成一份新候选。原方案可在规划记录中查看。</p>
      <label class="field">这次想改什么？
        <textarea id="revision-text" maxlength="1500" required rows="5"
          placeholder="例如：保留上午的博物馆，把下午的行程移到同一区域，多留半小时吃饭。">${esc(S.revisionText)}</textarea>
      </label>
      <div class="actions">${button('返回原方案','itinerary','secondary')}${button('生成修改后的方案','submit-revision')}</div>
    </section>`;
}
const pages = {overview, preferences, hotels, itinerary, settings, revision, attractions, profile};
function render() {
  if (!S.draft) return;
  document.querySelectorAll('nav a').forEach(a => {
    const active = a.hash === '#' + S.page;
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  $('connection').textContent = S.config.map_configured
    ? (S.config.model_configured ? '地图与模型已配置' : '高德已配置 · 待接入 DeepSeek') : '连接地图与 DeepSeek';
  $('app').innerHTML = (S.error ? `<div class="notice danger" role="alert">${esc(S.error)}</div>` : '')
    + (pages[S.page] || overview)();
  $('app').setAttribute('aria-busy', String(S.busy));
  if (S.busy) document.querySelectorAll('#app input, #app select, #app textarea, #app button').forEach(el => {
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
  const visitError = visitConstraintError();
  if (visitError) { toast(visitError); return false; }
  if (S.draft.must_visit?.length > 5 || S.draft.must_visit?.some(name => name.length > 80)) {
    toast('必去景点最多 5 个，每个名称最多 80 字。');
    return false;
  }
  if (S.draft.days == null || (S.draft.nights != null && S.draft.nights >= S.draft.days)) {
    toast('请填写旅行天数，住宿晚数须少于旅行天数。');
    return false;
  }
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
  attractionPreview = null;
  S.trip = trip;
  S.draft = {...draftFrom(trip.requirements), days:trip.requirements.days || 1};
  S.candidate = null;
  S.planHistory = [];
  S.historyOpen = false;
  S.planningRuns = [];
  S.planningRunsOpen = false;
  S.day = 1;
  S.step = 1;
  S.visitDayInputs = Object.create(null);
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
async function generate(revisionRequest = {}) {
  if (!S.config.map_configured || !S.config.model_configured) {
    S.error = !S.config.map_configured ? '请先配置高德Web服务Key，再生成真实行程。' : '请先配置DeepSeek Key。';
    toast(S.error);
    go('settings');
    return;
  }
  if (!revisionRequest.source_plan_id && !visibleFieldsValid()) return;
  if (!revisionRequest.source_plan_id) go('itinerary');
  await busy(async () => {
    if (!revisionRequest.source_plan_id) await save();
    const input = {...S.trip.requirements};
    const candidate = await api(`/trips/${S.trip.id}/plan`, 'POST', {
      mode:'deepseek', expected_requirements_revision:S.trip.requirements_revision, ...revisionRequest,
    });
    S.revisionText = '';
    S.candidate = {...candidate, requirements:candidate.requirements || input};
    S.planHistory = [S.candidate, ...S.planHistory.filter(p => p.id !== candidate.id)].slice(0,20);
    S.day = 1;
    S.hotels = S.candidate.hotel_options || [];
    S.hotelContext = hotelQueryContext(input);
    S.recommended = S.candidate.hotel;
    if (revisionRequest.source_plan_id) go('itinerary');
    toast(isReadyPlan(S.candidate) ? '行程已生成，请查看每日安排与待确认事项。'
      : S.candidate.error || planningOutcomeMessage(S.candidate) || '发现未解决的约束，请查看处理说明。');
  });
}
const actions = {
  overview:() => go('overview'), preferences:() => go('preferences'),
  'add-visit-constraint':() => {
    const items = S.draft.visit_constraints || [];
    if (items.length >= 10) { toast('景点时间要求最多 10 条。'); return; }
    const id = 'visit-'+Date.now().toString(36)+'-'+(++visitConstraintCounter);
    changeDraft('visit_constraints',[...items,{constraint_id:id,place_id:null,place_query:'',
      windows:[{day:1,start_time:S.draft.start_time,end_time:S.draft.end_time}],min_duration_minutes:60,fixed:false}]);
    render();
  },
  'remove-visit-constraint':el => {
    const id = el.dataset.id;
    delete S.visitDayInputs[id];
    changeDraft('visit_constraints',(S.draft.visit_constraints || []).filter(item=>item.constraint_id!==id));
    render();
  },
  hotels:() => go('hotels'), itinerary:() => go('itinerary'), settings:() => go('settings'),
  next:() => { if (visibleFieldsValid()) { S.step = Math.min(3, S.step + 1); render(); } },
  prev:() => { S.step = Math.max(1, S.step - 1); render(); },
  constraints:() => { S.step = 3; go('preferences'); },
  'attraction-preferences':() => { S.step = 2; go('preferences'); },
  finish:el => {
    if (!visibleFieldsValid()) return;
    return busy(async () => { await save(); toast('旅行偏好已保存。'); go(nightsFor() && el.dataset.next === 'hotels' ? 'hotels' : 'itinerary'); });
  },
  'choose-attractions':() => {
    if (!visibleFieldsValid()) return;
    return busy(async () => {
      try {
        attractionTask = '正在按偏好挑选景点，准备介绍和照片…';
        await save();
        const path = attractionDraftPath();
        go('attractions');
        adoptAttractionDraft(path, await api(path, 'POST'));
      } finally { attractionTask = ''; }
    });
  },
  'next-attractions':() => {
    const preview = currentAttractionPreview();
    if (!preview || preview.exhausted) return;
    attractionTask = '正在准备下一批，已选景点会保留…';
    return busy(async () => {
      try {
        const saved = await api(preview.path+'/next', 'POST', {selection_revision:preview.revision});
        adoptAttractionDraft(preview.path, saved);
        window.scrollTo({top:0, behavior:'smooth'});
      } catch (error) {
        if (error.status === 409) await restoreAttractionDraft().catch(() => {});
        throw error;
      } finally { attractionTask = ''; }
    });
  },
  'photo-prev':el => moveAttractionPhoto(el, -1),
  'photo-next':el => moveAttractionPhoto(el, 1),
  'toggle-attraction':async el => {
    const preview = currentAttractionPreview();
    const id = el.dataset.id;
    if (!preview || (!preview.selected.has(id) && !preview.seenIds.has(id))) return;
    const selected = new Set(preview.selected);
    if (!selected.delete(id)) {
      if (selected.size >= 5) { toast('最多选 5 个景点。'); return; }
      selected.add(id);
    }
    await busy(async () => {
      try {
        const saved = await api(preview.path, 'PUT', {selection_revision:preview.revision, selected_ids:[...selected]});
        adoptAttractionDraft(preview.path, saved);
      } catch (error) {
        if (error.status === 409) await restoreAttractionDraft().catch(() => {});
        throw error;
      }
    });
    if (S.page === 'attractions') {
      [...document.querySelectorAll('#app [data-action="toggle-attraction"]')]
        .find(button => button.dataset.id === id)?.focus({preventScroll:true});
    }
  },
  'confirm-attractions':async () => {
    const preview = currentAttractionPreview();
    if (!preview?.selected.size) return;
    if (conflictingSelectedExperiences(preview)) { toast('有景点属于同一游玩内容，请只保留其中一个。'); return; }
    let confirmed = false;
    await busy(async () => {
      try {
        const trip = await api(preview.path+'/confirm', 'POST', {selection_revision:preview.revision});
        adoptTrip(trip); confirmed = true;
      } catch (error) {
        if (error.status === 409) await restoreAttractionDraft().catch(() => {});
        throw error;
      }
    });
    if (confirmed) return generate();
  },
  'clear-attractions':() => { changeDraft('selected_attractions', []); render(); },
  profile:() => busy(async () => {
    S.travelProfile = await api('/preferences');
    S.profileDraft = null;
    go('profile');
  }),
  new:() => busy(async () => {
    S.travelProfile = await api('/preferences');
    attractionPreview = null;
    S.trip = null; S.candidate = null; S.error = null; S.planHistory = []; S.historyOpen = false; resetHotels();
    S.draft = draftFrom(S.travelProfile.values); S.step = 1; S.day = 1; S.visitDayInputs = Object.create(null);
    localStorage.removeItem('tripweave-v2-id'); go('preferences');
  }),
  generate:() => generate(),
  revise:() => { S.revisionText = ''; go('revision'); },
  'submit-revision':() => {
    const field = $('revision-text');
    if (!field.reportValidity()) return;
    S.revisionText = field.value.trim();
    if (!S.revisionText) { toast('请输入具体的修改要求。'); return; }
    const plan = shownPlan();
    if (!isReadyPlan(plan)) return;
    return generate({source_plan_id:plan.id, revision_notes:S.revisionText});
  },
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
  'planning-runs':() => busy(async () => {
    if (!S.trip) return;
    S.planningRunsOpen = !S.planningRunsOpen;
    if (S.planningRunsOpen) {
      const tripId=S.trip.id, data=await api(`/trips/${tripId}/planning-runs`);
      if (S.trip?.id === tripId) S.planningRuns=data.runs || [];
    }
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
    S.draft.selected_attractions = [];
    if (S.draft.visit_constraints?.length) toast('目的地已变更，原城市的景点时间要求已清空，请重新填写。');
    S.draft.visit_constraints = [];
    S.visitDayInputs = Object.create(null);
    resetHotels();
    document.querySelectorAll('[data-field="origin_query"], [data-field="booking_query"]')
      .forEach(el => { el.value = ''; });
  }
  if (['origin_query', 'booking_query', 'hotel_preference', 'custom_hotel', 'days', 'nights'].includes(key)) resetHotels();
  if (key === 'days') {
    if (value >= 1) S.draft.booking_day = Math.max(1, Math.min(S.draft.booking_day, value));
    const bookingDay = document.querySelectorAll('[data-field="booking_day"]')[0];
    if (bookingDay) bookingDay.max = value;
  }
  if ((key === 'days' || key === 'nights') && !nightsFor()) {
    S.draft.hotel_id = null;
  }
}
document.addEventListener('click', event => {
  const el = event.target.closest('button');
  if (!el || el.disabled || S.busy) return;
  if (el.dataset.fieldChoice) {
    const key = el.dataset.fieldChoice;
    if (key === 'days') { S.draft.nights = null; resetHotels(); }
    changeDraft(key, ['days', ...pacingKeys].includes(key) ? Number(el.dataset.value) : el.dataset.value);
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
    if (visibleFieldsValid()) { S.step = Number(el.dataset.step); render(); }
  } else if (el.dataset.day) {
    S.day = Number(el.dataset.day); render();
  } else if (actions[el.dataset.action]) actions[el.dataset.action](el);
});
document.addEventListener('submit', event => {
  if (event.target.id === 'travel-profile') {
    event.preventDefault();
    if (S.busy) return;
    const values = Object.fromEntries(new FormData(event.target));
    values.hotel_budget = Number(values.hotel_budget);
    for (const key of pacingKeys) values[key] = values[key] === '' ? null : Number(values[key]);
    S.profileDraft = values;
    return busy(async () => {
      S.travelProfile = await api('/preferences', 'PUT', {expected_revision:S.travelProfile.revision, values});
      S.profileDraft = null;
      toast('长期偏好已保存，下次新建旅行时会自动填入。');
    });
  }
  if (event.target.id !== 'provider-keys') return;
  event.preventDefault();
  if (S.busy) return;
  const changes = {};
  const mapKey = $('amap-key').value.trim();
  const modelKey = $('deepseek-key').value.trim();
  if (mapKey) changes.amap_api_key = mapKey;
  if (modelKey) changes.api_key = modelKey;
  if (!mapKey && !modelKey) { toast('请至少填写一个 API Key。'); return; }
  return busy(async () => {
    S.config = await api('/config/keys', 'PUT', changes);
    toast('API Key 已保存。');
  });
});
document.addEventListener('input', event => {
  const key = event.target.dataset.field;
  if (S.busy) return;
  if (event.target.dataset.visitField) {
    updateVisitConstraint(event.target.dataset.constraintId,event.target.dataset.visitField,event.target.value);
    return;
  }
  if (event.target.id === 'revision-text') S.revisionText = event.target.value;
  if (!key) return;
  const value = event.target.value;
  if (key === 'must_visit') {
    changeDraft(key, value.split(/\r?\n/).map(name => name.trim()).filter(Boolean));
  } else {
    changeDraft(key, event.target.type === 'number' ? (value === '' ? null : Number(value)) : value);
  }
  const nights = document.querySelectorAll('[data-field="nights"]')[0];
  if (nights) {
    nights.placeholder = String(Math.max(0, S.draft.days - 1));
    nights.setCustomValidity(S.draft.nights != null && S.draft.nights >= S.draft.days ? '住宿晚数须少于旅行天数' : '');
  }
});
window.addEventListener('hashchange', () => {
  const page = location.hash.slice(1);
  S.page = Object.hasOwn(pages, page) ? page : 'overview'; render();
  if (S.page === 'attractions' && !currentAttractionPreview()) void busy(restoreAttractionDraft);
});
(async () => {
  try {
    [S.config, S.travelProfile, S.history] = await Promise.all([
      api('/config'), api('/preferences'), api('/trips'),
    ]);
    S.draft = draftFrom(S.travelProfile.values);
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
    if (S.page === 'attractions') await busy(restoreAttractionDraft);
  } catch (error) {
    $('app').replaceChildren();
    const message = document.createElement('p');
    message.textContent = '无法加载工作台：' + error.message; $('app').append(message);
  }
})();
