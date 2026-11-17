// State and rendering regressions in Node VM. This is not browser or live-provider QA.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

const source = readFileSync('tripweave/static/app.js', 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));
const requirements = {
  city:'上海', date:'2026-11-07', days:2, party_size:2, budget_total:3000,
  hotel_budget:450, hotel_preference:'balanced', hotel_id:null,
  pace:'balanced', transport:'mixed', origin_query:'人民广场',
  start_time:'09:00', end_time:'20:30', interests:'历史人文', notes:'',
  booking_query:'上海博物馆', booking_day:1, booking_start:'14:00',
  booking_end:'15:00', max_attractions:3, max_sightseeing_minutes:null,
  max_sightseeing_ratio:null, max_transport_minutes:null,
  visit_constraints:[],
};

function harness() {
  const elements = new Map();
  const listeners = {};
  const storage = new Map();
  const node = () => ({innerHTML:'', textContent:'', className:'', dataset:{},
    setAttribute() {}, replaceChildren() {}, append() {}});
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, node());
      return elements.get(id);
    },
    querySelectorAll:() => [],
    addEventListener:(type, fn) => { listeners[type] = fn; },
    createElement:() => ({...node(), click() {}}),
  };
  const context = vm.createContext({
    document, location:{hash:''},
    window:{addEventListener() {}, scrollTo() {}},
    localStorage:{getItem:key => storage.get(key), setItem:(key,value) => storage.set(key,value),
      removeItem:key => storage.delete(key)},
    // Leave the automatic page bootstrap pending; cases set the exact API boundary they need.
    fetch:() => new Promise(() => {}), setTimeout:() => 0, clearTimeout() {},
    Blob, URL:{createObjectURL:() => 'blob:test', revokeObjectURL() {}},
  });
  vm.runInContext(source, context);
  const app = vm.runInContext('({S,actions,dirty,stalePlan,canAccept,planRequirements,currentHotels,hotelQueryContext,exportPayload,changeDraft,itinerary,hotels,dateFor,stopHeading,loadPlanHistory,planningRecords,adoptTrip,restoreAttractionDraft,render,preferences,visibleFieldsValid,profile})', context);
  app.S.config = {defaults:copy(requirements), map_configured:true, model_configured:true, model:'test-model'};
  app.S.draft = copy(requirements);
  app.S.trip = {id:'trip-1', requirements:copy(requirements), requirements_revision:1,
    accepted_version:0, accepted_plan:null};
  app.S.candidate = {
    id:'candidate-1', trip_id:'trip-1', mode:'basic', status:'ready',
    requirements_revision:1, base_version:0, requirements:copy(requirements),
    created_at:'2026-11-06T15:00:00Z', data_mode:'模拟测试数据',
    itinerary:{stops:[{day:1, kind:'activity', place_id:'p', start_time:'09:10',
      end_time:'10:10', title:'模型虚构标题', reason:'测试建议'}]},
    places:[{id:'p', name:'真实查询名称的模拟值', type:'风景名胜', source:'测试边界', address:'测试地址'}],
    hotel_options:[{id:'h', name:'上海测试酒店'}], routes:[], costs:{}, warnings:[],
  };
  return {...app, context, elements, document, listeners};
}

test('saved requirement changes cannot make an old candidate eligible again', () => {
  const h = harness();
  assert.equal(h.canAccept(), true);
  h.changeDraft('start_time', '10:00');
  assert.equal(h.canAccept(), false);
  h.S.trip.requirements = copy(h.S.draft);
  h.S.trip.requirements_revision = 2;
  assert.equal(h.dirty(), false);
  assert.equal(h.stalePlan(), true);
  assert.equal(h.canAccept(), false);
  assert.match(h.itinerary(), /data-action="accept"[^>]*disabled/);
});

test('plan display and JSON export keep the original city, date and party size', async () => {
  const h = harness();
  h.changeDraft('city', '杭州');
  h.changeDraft('date', '2026-12-24');
  h.changeDraft('days', 3);
  h.changeDraft('party_size', 4);
  const html = h.itinerary();
  assert.match(html, /上海，2天1晚的旅行安排/);
  assert.match(html, /2026-11-07出发 · 2人/);
  assert.doesNotMatch(html, /2026-12-24|杭州|DAY 03/);
  let exported;
  let link;
  h.context.URL.createObjectURL = blob => { exported = blob; return 'blob:test'; };
  h.document.createElement = () => (link = {click() {}});
  h.actions.export();
  const payload = JSON.parse(await exported.text());
  assert.deepEqual(payload.requirements, requirements);
  assert.equal(link.download, 'TripWeave-2026-11-07.json');
});

test('legacy candidates without their old input snapshot never borrow a new draft', () => {
  const h = harness();
  delete h.S.candidate.requirements;
  h.S.trip.requirements_revision = 2;
  h.S.trip.requirements.city = '杭州';
  h.S.draft.city = '杭州';
  assert.equal(h.planRequirements(), null);
  assert.equal(h.exportPayload().requirements, null);
  assert.match(h.itinerary(), /历史，1天0晚/);
  assert.doesNotMatch(h.itinerary(), /杭州/);
});

test('changing destination invalidates hotel results, fixed place and manual hotel', () => {
  const h = harness();
  h.S.hotels = [{id:'h2', name:'查询缓存的上海酒店'}];
  h.S.hotelContext = h.hotelQueryContext(h.S.draft);
  h.S.draft.hotel_id = 'h2';
  assert.equal(h.currentHotels().length, 1);
  h.changeDraft('city', '杭州');
  assert.equal(h.currentHotels().length, 0);
  assert.equal(h.S.draft.booking_query, '');
  assert.equal(h.S.draft.origin_query, '');
  assert.equal(h.S.draft.hotel_id, null);
  assert.doesNotMatch(h.hotels(), /上海测试酒店|查询缓存的上海酒店/);
});

test('hotel ranking input changes hide old suggestions but a new explicit empty result stays empty', () => {
  const h = harness();
  h.changeDraft('hotel_preference', 'value');
  assert.equal(h.currentHotels().length, 0);
  h.changeDraft('hotel_preference', 'balanced');
  assert.equal(h.currentHotels().length, 1);
  h.S.hotels = [];
  h.S.hotelContext = h.hotelQueryContext(h.S.draft);
  assert.equal(h.currentHotels().length, 0);
});

test('accept handler rejects an outdated base without sending an HTTP mutation', async () => {
  const h = harness();
  let called = false;
  h.context.fetch = () => { called = true; throw Error('should not send'); };
  h.S.trip.accepted_version = 1;
  h.S.trip.accepted_plan = {...copy(h.S.candidate), id:'another-candidate'};
  assert.equal(h.stalePlan(), true);
  await h.actions.accept();
  assert.equal(called, false);
  assert.match(h.S.error, /已过时/);
});

test('missing keys lead to configuration without a generated plan or network call', async () => {
  const h = harness();
  h.S.config.map_configured = false;
  h.S.candidate = null;
  let called = false;
  h.context.fetch = () => { called = true; throw Error('should not send'); };
  await h.actions.generate();
  assert.equal(h.S.page, 'settings');
  assert.equal(h.S.candidate, null);
  assert.equal(called, false);
  assert.match(h.S.error, /高德/);
});

test('load history restores latest failed real-mode candidate and can inspect older records', async () => {
  const h = harness();
  const old = copy(h.S.candidate);
  const failed = {...copy(old), id:'failed-2', status:'conflict', error:'地图权限不足',
    itinerary:null, created_at:'2026-11-06T16:10:00Z'};
  h.context.fetch = async () => ({ok:true, json:async () => ({candidates:[failed, old, {...old, mode:'replay'}]})});
  await h.loadPlanHistory(true);
  assert.equal(h.S.planHistory.length, 2);
  assert.equal(h.S.candidate.id, 'failed-2');
  h.S.historyOpen = true;
  assert.match(h.planningRecords(), /地图权限不足/);
  assert.match(h.planningRecords(), /2026\/11\/7 00:10:00/);
  h.actions['show-candidate']({dataset:{id:old.id}});
  assert.equal(h.S.candidate.id, old.id);
  assert.equal(h.S.trip.accepted_plan, null);
});

test('switching trips clears hotel cache and previous candidate state', () => {
  const h = harness();
  h.S.day = 3;
  h.S.planHistory = [h.S.candidate];
  h.S.hotels = [{id:'old'}];
  h.S.hotelContext = h.hotelQueryContext(h.S.draft);
  h.adoptTrip({id:'trip-2', requirements:{...requirements,city:'杭州'},
    requirements_revision:1,accepted_version:0,accepted_plan:null});
  assert.equal(h.S.candidate, null);
  assert.equal(h.S.day, 1);
  assert.equal(h.S.hotels.length, 0);
  assert.equal(h.S.planHistory.length, 0);
  assert.equal(h.S.draft.city, '杭州');
});

test('titles use queried names, meals at a non-restaurant are labeled self-selected', () => {
  const h = harness();
  assert.doesNotMatch(h.itinerary(), /模型虚构标题/);
  assert.match(h.itinerary(), /真实查询名称的模拟值/);
  assert.equal(h.stopHeading({kind:'meal'}, {name:'测试公园',type:'风景名胜'}), '测试公园附近 · 自选用餐');
  assert.equal(h.stopHeading({kind:'meal'}, {name:'测试餐厅',type:'餐饮服务'}), '用餐 · 测试餐厅');
});

test('provider text is escaped and dates use China time across a year boundary', () => {
  const h = harness();
  h.S.candidate.places[0].name = '<img src=x onerror=alert(1)>';
  assert.doesNotMatch(h.itinerary(), /<img/);
  assert.match(h.itinerary(), /&lt;img/);
  assert.equal(h.dateFor(2, {date:'2026-12-31'}), '1月1日');
});

test('input events during a pending action cannot change the planning snapshot', () => {
  const h = harness();
  h.S.busy = true;
  h.listeners.input({target:{dataset:{field:'city'},type:'text',value:'杭州'}});
  assert.equal(h.S.draft.city, '上海');
});

test('saving preferences skips hotel search unless the traveler chooses hotels', async () => {
  const h = harness();
  await h.actions.finish({dataset:{}});
  assert.equal(h.S.page, 'itinerary');
  await h.actions.finish({dataset:{next:'hotels'}});
  assert.equal(h.S.page, 'hotels');

  h.S.draft.nights = 0;
  h.S.trip.requirements.nights = 0;
  await h.actions.finish({dataset:{next:'hotels'}});
  assert.equal(h.S.page, 'itinerary');
});

test('collaborative selection uses current requirements and saves selected POI before planning', async () => {
  const h = harness();
  const calls = [];
  const options = [{id:'museum',name:'博物馆',web_summary:'人文展览，预约待确认。',web_sources:[{title:'开放公告',url:'https://museum.example/visit'}]}];
  const drafts = new Map();
  const planned = copy(h.S.candidate);
  h.context.fetch = async (url, request) => {
    const body = request.body && JSON.parse(request.body);
    calls.push({url, body, method:request.method});
    let data;
    if (url.endsWith('/confirm')) {
      const draft = drafts.get(url.slice(0,-'/confirm'.length));
      assert.equal(body.selection_revision,draft.selection_revision);
      data = {...h.S.trip, requirements:{...h.S.trip.requirements,
        selected_attractions:options.filter(p=>draft.selected_ids.includes(p.id)).map(({id,name})=>({id,name}))},
        requirements_revision:h.S.trip.requirements_revision+1};
    } else if (url.includes('/attractions/')) {
      if (!drafts.has(url)) drafts.set(url, {options, selected_ids:[], selection_revision:1});
      if (request.method === 'PUT') drafts.set(url, {options, selected_ids:body.selected_ids, selection_revision:body.selection_revision+1});
      data = drafts.get(url);
    } else {
      data = url.endsWith('/requirements') ? {...h.S.trip, requirements:body, requirements_revision:h.S.trip.requirements_revision+1}
        : {...planned, requirements_revision:h.S.trip.requirements_revision};
    }
    return {ok:true, json:async () => data};
  };
  await h.actions['choose-attractions']();
  assert.match(h.elements.get('app').innerHTML, /人文展览，预约待确认。/);
  assert.match(h.elements.get('app').innerHTML, /href="https:\/\/museum\.example\/visit"/);
  await h.actions['toggle-attraction']({dataset:{id:'museum'}});
  h.adoptTrip(h.S.trip);
  await h.restoreAttractionDraft();
  h.S.page = 'attractions';
  h.render();
  assert.match(h.elements.get('app').innerHTML, /确认已选 1 个并生成/);
  h.changeDraft('date', '2026-11-08');
  await h.actions['confirm-attractions']();
  assert.equal(calls.length, 3);

  await h.actions['choose-attractions']();
  await h.actions['toggle-attraction']({dataset:{id:'museum'}});
  await h.actions['confirm-attractions']();
  assert.deepEqual(calls.map(call => [call.method, call.url]), [
    ['POST', '/api/trips/trip-1/attractions/1'], ['PUT', '/api/trips/trip-1/attractions/1'],
    ['GET', '/api/trips/trip-1/attractions/1'], ['PUT', '/api/trips/trip-1/requirements'],
    ['POST', '/api/trips/trip-1/attractions/2'], ['PUT', '/api/trips/trip-1/attractions/2'],
    ['POST', '/api/trips/trip-1/attractions/2/confirm'], ['POST', '/api/trips/trip-1/plan'],
  ]);
  assert.deepEqual(calls[6].body, {selection_revision:2});
  assert.deepEqual(h.S.trip.requirements.selected_attractions,[{id:'museum',name:'博物馆'}]);
  assert.equal(calls[7].body.expected_requirements_revision, 3);
});

test('a conflicting selection confirmation does not start a model planning request', async () => {
  const h = harness(), calls = [];
  const saved = {options:[{id:'museum',name:'博物馆'}],selected_ids:['museum'],selection_revision:2};
  h.context.fetch = async (url,request={}) => {
    calls.push(url);
    if(url.endsWith('/confirm')) return {ok:false,status:409,json:async()=>({detail:'选景已更新'})};
    return {ok:true,json:async()=>url.includes('/attractions/')?saved:copy(h.S.trip)};
  };
  await h.actions['choose-attractions']();
  await h.actions['confirm-attractions']();
  assert.ok(!calls.some(url=>url.endsWith('/plan')));
  assert.match(h.elements.get('app').innerHTML,/选景已更新/);
});

test('long-term preferences prefill a new trip without changing the current trip', async () => {
  const h = harness();
  const values = {pace:'relaxed', custom_pace:'午饭后休息一小时'};
  h.context.fetch = async url => {
    assert.equal(url, '/api/preferences');
    return {ok:true, json:async () => ({revision:1, values})};
  };
  await h.actions.profile();
  assert.equal(h.S.draft.pace, 'balanced');
  assert.match(h.elements.get('app').innerHTML, /午饭后休息一小时/);
  await h.actions.new();
  assert.equal(h.S.draft.pace, 'relaxed');
  assert.equal(h.S.draft.custom_pace, '午饭后休息一小时');
  assert.equal(h.S.page, 'preferences');
});

test('nonrepeating batches keep earlier choices, support revisiting and stop at exhaustion', async () => {
  const h = harness();
  const pool = Array.from({length:7}, (_,i) => ({id:'p'+i, name:'测试景点'+i,
    photos:['https://photos.example/'+i+'/1.jpg','https://photos.example/'+i+'/2.jpg','https://photos.example/'+i+'/3.jpg'],
    rating:4.5, web_summary:'看看建筑与展陈', audience:'喜欢人文的慢游旅行者'}));
  let saved = {options:pool.slice(0,5), candidate_pool:pool, selected_ids:[], selection_revision:1,
    seen_ids:pool.slice(0,5).map(p=>p.id), batch_number:1, exhausted:false};
  const calls = [];
  h.context.fetch = async (url, options={}) => {
    const body = options.body && JSON.parse(options.body);
    calls.push({url, body, method:options.method});
    if (url.endsWith('/next')) {
      assert.equal(body.selection_revision, saved.selection_revision);
      saved = {...saved, options:pool.slice(5), seen_ids:pool.map(p=>p.id),
        selection_revision:saved.selection_revision+1, batch_number:2, exhausted:true};
    } else if (options.method === 'PUT') {
      assert.equal(body.selection_revision, saved.selection_revision);
      saved = {...saved, selected_ids:body.selected_ids, selection_revision:saved.selection_revision+1};
    }
    return {ok:true, json:async () => saved};
  };
  await h.actions['choose-attractions']();
  let html = h.elements.get('app').innerHTML;
  assert.equal((html.match(/class="card attraction-card/g)||[]).length, 5);
  assert.match(html, /高德照片/);
  assert.match(html, /喜欢人文的慢游旅行者/);
  assert.match(html, /data-photo-gallery/);
  await h.actions['toggle-attraction']({dataset:{id:'p0'}});
  await h.actions['next-attractions']();
  html = h.elements.get('app').innerHTML;
  assert.equal((html.match(/class="card attraction-card/g)||[]).length, 2);
  assert.match(html, /回看已浏览的 5 个景点/);
  assert.match(html, /已选 1 \/ 5/);
  assert.match(html, /本轮候选已看完/);
  assert.match(html, /data-action="next-attractions"[^>]*disabled/);
  const exhaustedCalls = calls.length;
  await h.actions['next-attractions']();
  assert.equal(calls.length, exhaustedCalls);
  await h.actions['toggle-attraction']({dataset:{id:'p1'}});
  await h.actions['toggle-attraction']({dataset:{id:'p5'}});
  await h.actions['toggle-attraction']({dataset:{id:'p0'}});
  assert.deepEqual(saved.selected_ids, ['p1','p5']);
  h.adoptTrip(h.S.trip);
  await h.restoreAttractionDraft();
  h.S.page = 'attractions'; h.render();
  assert.match(h.elements.get('app').innerHTML, /已选 2 \/ 5/);
  assert.match(h.elements.get('app').innerHTML, /第 2 批/);
  assert.equal(saved.seen_ids.length, 7);
});

test('itinerary summary matches required places by map ID and shows revision notes', () => {
  const h = harness();
  h.S.candidate.must_visit_ids = ['p'];
  h.S.candidate.revision_notes = '第二天晚点出发';
  const page = h.itinerary();
  assert.match(page, /真实查询名称的模拟值 · 第 1 天/);
  assert.match(page, /本次修改要求.*第二天晚点出发/);
  assert.ok(page.indexOf('行程摘要') < page.indexOf('day-tabs'));
});

test('photo and introduction shortages are explicit without duplicated photos or invented summaries', async () => {
  const h = harness();
  const photos = ['https://photos.example/1.jpg','https://photos.example/1.jpg','https://photos.example/2.jpg'];
  const options = [
    {id:'zero',name:'无照片测试',photos:[],recommendation_reason:'与历史兴趣相关'},
    {id:'one',name:'一张测试',photos:photos.slice(0,1),web_summary:'有来源的介绍',web_sources:[{url:'https://museum.example',title:'介绍页'}]},
    {id:'two',name:'两张测试',photos,web_sources:[{url:'https://museum.example',title:'资料页'}]},
  ];
  h.context.fetch = async () => ({ok:true,json:async () => ({options,candidate_pool:options,
    selected_ids:[],selection_revision:1,seen_ids:options.map(p=>p.id),exhausted:true})});
  await h.actions['choose-attractions']();
  const html = h.elements.get('app').innerHTML;
  assert.match(html, /暂无可用实景照片/);
  assert.match(html, /目前仅取得 1 张实景照片/);
  assert.match(html, /目前仅取得 2 张实景照片/);
  assert.equal((html.match(/class="photo-frame"/g)||[]).length, 3);
  assert.match(html, /暂未取得可靠的景点介绍/);
  assert.match(html, /推荐理由 · 与历史兴趣相关/);
  assert.doesNotMatch(html, /attraction-summary">推荐理由/);
  assert.match(html, /资料不足，适合人群待确认/);
  assert.match(html, /不代表整座城市没有其他值得去的景点/);
  const broken = {hidden:false,matches:()=>true,nextElementSibling:{hidden:true}};
  h.listeners.error({target:broken});
  assert.equal(broken.hidden,true);
  assert.equal(broken.nextElementSibling.hidden,false);
});

test('upgraded choices keep their POI IDs and duplicate experiences must be resolved before planning', async () => {
  const h = harness();
  const preserved = [{id:'child',name:'旧建筑群',experience_group:'main'},
    {id:'main',name:'旧主体',experience_group:'main'}];
  const saved = {candidate_pool:[],options:[],selected_items:preserved,selected_ids:['child','main'],
    selection_revision:2,seen_ids:['child','main'],exhausted:true};
  let calls = 0;
  h.context.fetch = async () => { calls++; return {ok:true,json:async () => saved}; };
  await h.actions['choose-attractions']();
  const html = h.elements.get('app').innerHTML;
  assert.match(html, /旧建筑群/);
  assert.match(html, /保留的选择中有景点属于同一游玩内容/);
  assert.match(html, /data-action="confirm-attractions"[^>]*disabled/);
  await h.actions['confirm-attractions']();
  assert.equal(calls,1);
});


const inputVisit = (h, id, key, value) => h.listeners.input({target:{dataset:{visitField:key,constraintId:id},value}});

test('time requirements save alternative days and preserve other requirements without mutating the saved version', async () => {
  const h = harness();
  h.S.step = 3;
  h.actions['add-visit-constraint']();
  const id = h.S.draft.visit_constraints[0].constraint_id;
  assert.equal(h.S.trip.requirements.visit_constraints.length, 0);
  assert.equal(h.S.config.defaults.visit_constraints.length, 0);
  assert.equal(h.visibleFieldsValid(), false);
  inputVisit(h,id,'place_query','测试自然博物馆');
  inputVisit(h,id,'days','1,2');
  inputVisit(h,id,'start_time','13:00');
  inputVisit(h,id,'end_time','17:00');
  inputVisit(h,id,'min_duration_minutes','120');
  assert.equal(h.visibleFieldsValid(), true);
  assert.equal(h.canAccept(), false);
  const html = h.preferences();
  assert.match(html,/完整游玩须在窗口内并达到所填时长/);
  assert.match(html,/任选一天/);
  let saved;
  h.context.fetch = async (url, options) => {
    assert.equal(url,'/api/trips/trip-1/requirements');
    assert.equal(options.method,'PUT');
    const {expected_revision,...body} = JSON.parse(options.body);
    assert.equal(expected_revision,1);
    saved = body;
    return {ok:true,json:async()=>({...h.S.trip,requirements:body,requirements_revision:2})};
  };
  await h.actions.finish({dataset:{}});
  assert.equal(saved.notes,requirements.notes);
  assert.equal(saved.booking_query,requirements.booking_query);
  assert.equal(saved.visit_constraints[0].fixed,false);
  assert.equal(saved.visit_constraints[0].place_query,'测试自然博物馆');
  assert.equal(saved.visit_constraints[0].min_duration_minutes,120);
  assert.deepEqual(saved.visit_constraints[0].windows,[
    {day:1,start_time:'13:00',end_time:'17:00'},
    {day:2,start_time:'13:00',end_time:'17:00'},
  ]);
  assert.equal(h.dirty(),false);
  assert.equal(h.stalePlan(),true);
});

test('unfinished date input blocks planning and survives rendering without replacing valid saved windows', async () => {
  const h = harness();
  h.actions['add-visit-constraint']();
  const id = h.S.draft.visit_constraints[0].constraint_id;
  inputVisit(h,id,'place_query','测试景点');
  inputVisit(h,id,'days','1,');
  assert.equal(h.S.draft.visit_constraints[0].windows[0].day,1);
  assert.equal(h.visibleFieldsValid(),false);
  h.S.step = 3;
  assert.match(h.preferences(),/value="1,"/);
  let called = false;
  h.context.fetch = ()=>{called=true;throw Error('should not submit');};
  await h.actions.generate();
  assert.equal(called,false);
  assert.match(h.elements.get('toast').textContent,/不能重复或留空/);
  inputVisit(h,id,'days','1,2');
  assert.equal(h.visibleFieldsValid(),true);
  inputVisit(h,id,'days','1,1');
  assert.equal(h.visibleFieldsValid(),false);
  inputVisit(h,id,'days','1，2');
  assert.equal(h.visibleFieldsValid(),true);
});

test('changing the city clears city-specific time requirements, reducing days preserves them for correction', () => {
  const h = harness();
  h.actions['add-visit-constraint']();
  const id = h.S.draft.visit_constraints[0].constraint_id;
  inputVisit(h,id,'place_query','测试景点');
  inputVisit(h,id,'days','2');
  h.changeDraft('days',1);
  assert.equal(h.S.draft.visit_constraints[0].windows[0].day,2);
  assert.equal(h.visibleFieldsValid(),false);
  assert.match(h.elements.get('toast').textContent,/须在本次旅行天数内/);
  h.changeDraft('city','杭州');
  assert.equal(h.S.draft.visit_constraints.length,0);
  assert.match(h.elements.get('toast').textContent,/原城市的景点时间要求已清空/);
});

test('complex existing windows are preserved read-only and can be removed explicitly', () => {
  const h = harness();
  const original = {constraint_id:'existing',place_query:'测试馆',place_id:'known',fixed:false,min_duration_minutes:90,
    windows:[{day:1,start_time:'10:00',end_time:'12:00'},{day:2,start_time:'14:00',end_time:'17:00'}]};
  h.S.draft.visit_constraints = [copy(original)];
  h.S.step = 3;
  const html = h.preferences();
  assert.match(html,/第 1 天 · 10:00–12:00/);
  assert.match(html,/第 2 天 · 14:00–17:00/);
  assert.match(html,/已完整保留/);
  assert.doesNotMatch(html,/data-visit-field=/);
  inputVisit(h,'existing','start_time','09:00');
  assert.deepEqual(copy(h.S.draft.visit_constraints[0]),original);
  h.changeDraft('notes','另加普通偏好');
  assert.deepEqual(copy(h.S.draft.visit_constraints[0]),original);
  h.actions['remove-visit-constraint']({dataset:{id:'existing'}});
  assert.equal(h.S.draft.visit_constraints.length,0);
});

test('time requirement inputs cannot edit a pending planning snapshot and adding stops at ten', () => {
  const h = harness();
  for(let i=0;i<10;i++) h.actions['add-visit-constraint']();
  const first = h.S.draft.visit_constraints[0];
  h.actions['add-visit-constraint']();
  assert.equal(h.S.draft.visit_constraints.length,10);
  h.S.step = 3;
  assert.match(h.preferences(),/data-action="add-visit-constraint"[^>]*disabled/);
  h.S.busy = true;
  inputVisit(h,first.constraint_id,'place_query','不可写入');
  assert.equal(h.S.draft.visit_constraints[0].place_query,'');
});

test('all outcome types retain failed drafts without presenting or accepting them as usable itineraries', async () => {
  const h = harness();
  const outcomes = {
    needs_information:'资料待补充',needs_user_decision:'需要调整要求',
    incomplete:'本次未完成',service_error:'接口异常',
  };
  for(const [outcome,label] of Object.entries(outcomes)) {
    h.S.candidate = {...copy(h.S.candidate),status:'conflict',outcome,error:null,itinerary:null,
      draft:{summary:'未完成草稿',stops:[{day:1,place_id:'p',reason:'尚未校验的草稿内容'}]},
      issues:[{category:'missing_fact',day:2,place_id:'p',message:'缺少可靠资料',missing_fields:['opening_hours']}]};
    const html = h.itinerary();
    assert.match(html,new RegExp(label));
    assert.match(html,/草稿已保存，尚不可接受/);
    assert.match(html,/待补充：开放时段/);
    assert.doesNotMatch(html,/尚未校验的草稿内容|data-action="revise"/);
    assert.match(html,/data-action="accept"[^>]*disabled/);
    assert.equal(h.canAccept(),false);
    let requested = false;
    h.context.fetch = ()=>{requested=true;throw Error('should not accept');};
    await h.actions.accept();
    assert.equal(requested,false);
  }
  h.S.candidate.outcome = 'incomplete';
  assert.match(h.itinerary(),/不代表你的旅行需求无解/);
  const exported = h.exportPayload();
  assert.equal(exported.candidate.draft.summary,'未完成草稿');
});

test('history reports the outcome reason and guards inconsistent ready records', () => {
  const h = harness();
  h.S.candidate.outcome = 'needs_information';
  assert.equal(h.canAccept(),false);
  assert.doesNotMatch(h.itinerary(),/data-action="revise"/);
  h.S.historyOpen = true;
  h.S.planHistory = ['needs_information','needs_user_decision','incomplete','service_error'].map((outcome,index)=>
    ({...copy(h.S.candidate),id:'result-'+index,status:'conflict',outcome}));
  const html = h.planningRecords();
  for(const label of ['资料待补充','需要调整要求','本次未完成','接口异常']) assert.match(html,new RegExp(label));
  assert.doesNotMatch(html,/存在冲突/);
});

test('four pacing controls are optional and distinguish cleared values from explicit zero', () => {
  const h = harness();
  h.S.step = 2;
  const html = h.preferences();
  for (const key of ['max_attractions','max_sightseeing_minutes','max_sightseeing_ratio','max_transport_minutes']) {
    assert.match(html,new RegExp(`data-field="${key}"[^>]*placeholder="留空不设目标"`));
    h.listeners.input({target:{dataset:{field:key},type:'number',value:'0'}});
    assert.equal(h.S.draft[key],0);
    h.listeners.input({target:{dataset:{field:key},type:'number',value:''}});
    assert.equal(h.S.draft[key],null);
  }
  assert.doesNotMatch(html,/data-field="max_activities"|每日活动上限/);
  assert.match(html,/固定窗口|分母不因方案调整而改变/);
  h.S.travelProfile = {values:copy(h.S.draft)};
  assert.match(h.profile(),/name="max_transport_minutes"/);
  assert.match(h.profile(),/name="max_sightseeing_ratio"/);
});

test('legacy trip limit is mapped before defaults without dirtying or rewriting history', () => {
  const h = harness();
  const old = copy(h.S.trip);
  delete old.requirements.max_attractions;
  old.requirements.max_activities = 1;
  h.adoptTrip(old);
  assert.equal(h.S.draft.max_attractions,1);
  assert.equal(Object.hasOwn(h.S.draft,'max_activities'),false);
  assert.equal(h.S.trip.requirements.max_activities,1);
  assert.equal(h.dirty(),false);
  h.changeDraft('max_sightseeing_minutes',180);
  assert.equal(h.dirty(),true);
  const modern = copy(old);
  modern.requirements.max_attractions = null;
  h.adoptTrip(modern);
  assert.equal(h.S.draft.max_attractions,null);
  assert.equal(h.dirty(),false);
});

test('displayed pacing statistics belong to the saved candidate, including incomplete drafts', () => {
  const h = harness();
  h.S.candidate.daily_metrics = [{day:1,attraction_count:1,sightseeing_minutes:180,
    available_minutes:720,sightseeing_ratio:25,transport_minutes:30,
    limits:{max_attractions:1,max_sightseeing_minutes:180,max_sightseeing_ratio:25,max_transport_minutes:30}}];
  h.changeDraft('max_attractions',5);
  h.changeDraft('end_time','23:00');
  let html = h.itinerary();
  assert.match(html,/1个 \/ 1个/);
  assert.match(html,/180分钟 \/ 180分钟/);
  assert.match(html,/25% \/ 25%/);
  assert.match(html,/窗口 720 分钟/);
  h.S.candidate.outcome='incomplete';
  h.S.candidate.status='conflict';
  h.S.candidate.daily_metrics[0].transport_minutes=null;
  html=h.itinerary();
  assert.match(html,/第 1 天 · 草稿/);
  assert.match(html,/未核实 \/ 30分钟/);
  assert.equal(h.canAccept(),false);
});

test('historical token totals display without inventing per-call records or current budgets', () => {
  const h=harness();
  h.S.config.planning_budget={max_model_requests:10};
  h.S.candidate.usage={requests:3,input_tokens:13228,output_tokens:3965};
  h.S.candidate.outcome='needs_information';
  h.S.candidate.status='conflict';
  const html=h.itinerary();
  assert.match(html,/模型用量与预算/);
  for(const total of ['13,228','3,965','17,193']) assert.ok(html.includes(total));
  assert.match(html,/历史记录未保存预算与逐次明细/);
  assert.doesNotMatch(html,/aria-label="模型请求"|查看每次调用明细/);
  assert.match(html,/不包含之前的选景推荐/);
});

function observedCalls(h) {
  h.S.candidate.usage={requests:2,input_tokens:1000,output_tokens:100};
  h.S.candidate.usage_observation={model_http_attempts:2,elapsed_seconds:12,usage_complete:true,
    budget:{max_model_requests:6,run_timeout_seconds:90,planner_max_output_tokens:6000,research_max_output_tokens:4000,input_token_limit:null},
    model_calls:[{number:1,role:'planner',status:'response',input_tokens:600,output_tokens:60,
      reasoning_tokens:30,cache_read_tokens:200,elapsed_ms:5000,max_output_tokens:6000,finish_reason:'tool_calls'},
      {number:2,role:'research',status:'response',input_tokens:400,output_tokens:40,
      reasoning_tokens:20,cache_read_tokens:0,elapsed_ms:4000,max_output_tokens:4000,finish_reason:'length'}]};
}

test('budget and call table show original snapshot and subset counts without double-counting', () => {
  const h=harness();observedCalls(h);
  h.S.config.planning_budget={max_model_requests:10,planner_max_output_tokens:9999};
  const html=h.itinerary();
  assert.match(html,/1,100/);
  assert.doesNotMatch(html,/1,350|9,999/);
  assert.match(html,/progress max="6" value="2"/);
  assert.match(html,/progress max="90" value="12"/);
  assert.match(html,/1 · 排程/);
  assert.match(html,/2 · 研究/);
  assert.match(html,/输出可能被截断/);
  assert.match(html,/输入 未设硬上限/);
});

test('missing response usage is unknown while confirmed zero and no-call preflight stay distinct', () => {
  const h=harness();observedCalls(h);
  h.S.candidate.usage_observation.usage_complete=false;
  for(const call of h.S.candidate.usage_observation.model_calls) {
    call.input_tokens=null;call.output_tokens=null;call.reasoning_tokens=null;call.cache_read_tokens=null;
  }
  let html=h.itinerary();
  assert.match(html,/输入 tokens（已记录）<\/span><strong>未记录/);
  assert.match(html,/未返回/);
  assert.match(html,/总量可能不完整/);
  h.S.candidate.usage_observation.model_calls[0].input_tokens=0;
  html=h.itinerary();
  assert.match(html,/输入 tokens（已记录）<\/span><strong>0/);
  h.S.candidate.usage=null;
  h.S.candidate.usage_observation={...h.S.candidate.usage_observation,model_http_attempts:0,model_calls:[],usage_complete:true};
  html=h.itinerary();
  assert.match(html,/模型请求<\/span><strong>0/);
  assert.match(html,/合计 tokens<\/span><strong>0/);
  delete h.S.candidate.usage_observation;
  html=h.itinerary();
  assert.match(html,/合计 tokens<\/span><strong>未记录/);
});

test('fee panel separates conditional legacy ranges, saved estimates and unknown amounts', () => {
  const h=harness();
  const part={status:'estimated',min_cny:'0.00100800',max_cny:'0.00100800'};
  h.S.candidate.api_cost_preview={basis:'current_tariff_legacy_assumption',models:part,
    search:{status:'range',min_cny:'0.00000000',max_cny:'0.00500000'},
    subtotal:{status:'range',min_cny:'0.00100800',max_cny:'0.00600800'},
    tariff:{version:'test-price',verified_date:'2026-11-15'}};
  let html=h.itinerary();
  assert.match(html,/API 费用估算 · 人民币/);
  assert.match(html,/并非历史扣费/);
  assert.match(html,/¥0.001008 – ¥0.006008/);
  assert.match(html,/不含地图服务/);
  h.S.candidate.api_cost_estimate={...h.S.candidate.api_cost_preview,basis:'recorded_run',
    subtotal:{status:'unavailable'}};
  html=h.itinerary();
  assert.doesNotMatch(html,/并非历史扣费/);
  assert.match(html,/模型＋搜索小计<\/span><strong>暂不可算/);
  assert.match(html,/思考已包含在输出中，不再加一次/);
});

function reviewFixture(h, status='disputed') {
  const original=copy(h.S.candidate.itinerary);
  original.summary='首次草稿摘要';
  original.stops[0].reason='首次草稿站点安排';
  const latest=copy(original);
  latest.summary='当前未完成草稿摘要';
  latest.stops[0].start_time='10:00';
  latest.stops[0].reason='最新草稿站点安排';
  const suggestion={suggestion_id:'R1',positions:[{day:1,stop_number:1,place_id:'p'}],
    concern:'连续游玩后缺少休息',requirement_basis:'用户希望轻松游玩',proposed_change:'在午餐前留出休息时间'};
  const second={...copy(suggestion),suggestion_id:'R2',concern:'到达时间早于备注',
    requirement_basis:'说明文字应与时间字段一致',proposed_change:'修改说明中的到达时刻'};
  const review={version:1,status,
    initial:{summary:'发现两处可以改善的安排',suggestions:[suggestion,second]},
    original_draft:original,
    planner_response:{decisions:[
      {suggestion_id:'R1',action:'decline',reason:'用户指定了完整参观时段，午餐后已有一小时自由时间'},
      {suggestion_id:'R2',action:'revise',reason:'已使文字说明与时间字段一致'},
    ],revised_itinerary:latest},
    final:{summary:status==='approved'?'认可原休息安排与说明修订':'仍不能认同休息安排',
      suggestions:status==='approved'?[]:[copy(suggestion)]}};
  h.S.candidate={...h.S.candidate,mode:'deepseek',review,
    thinking_configuration:{reviewer:{enabled:true,effort:'max'}},
    status:status==='approved'?'ready':'conflict',outcome:status==='approved'?'ready':'needs_user_decision',
    itinerary:status==='approved'?latest:null,draft:status==='approved'?null:latest,
    issues:status==='approved'?[]:[{category:'review_disagreement',message:suggestion.concern,
      day:1,stop_number:1,place_id:'p',suggestion_id:'R1'}]};
  return h.S.candidate;
}

test('review disagreement shows both arguments, concrete positions and inspectable drafts without declaring infeasibility', async () => {
  const h=harness();reviewFixture(h);
  const html=h.itinerary();
  for(const text of ['规划分歧 · 待你协调','用户希望轻松游玩','连续游玩后缺少休息',
    '午餐后已有一小时自由时间','已使文字说明与时间字段一致','采纳并修订','保留原安排',
    '原草稿位置：第 1 天 · 第 1 站 · 真实查询名称的模拟值','仍有分歧',
    '最新草稿站点安排','首次草稿站点安排','当前未完成草稿摘要','思考强度 max']) {
    assert.ok(html.includes(text),text);
  }
  assert.match(html,/<details class="review-draft"><summary>查看最新完整草稿（尚不可接受）/);
  assert.match(html,/对照首次校验时的原草稿/);
  assert.match(html,/这不代表你的需求无法满足/);
  assert.doesNotMatch(html,/已确认的要求无法同时满足|data-action="revise"/);
  assert.match(html,/协调后的继续处理方式待讨论/);
  assert.match(html,/data-action="accept"[^>]*disabled/);
  let requested=false;
  h.context.fetch=()=>{requested=true;throw Error('must not accept');};
  await h.actions.accept();
  assert.equal(requested,false);
  assert.deepEqual(copy(h.exportPayload().candidate.review),copy(h.S.candidate.review));
});

test('review approval permits acceptance only together with program readiness and current versions', () => {
  const h=harness();reviewFixture(h,'approved');
  assert.equal(h.canAccept(),true);
  const html=h.itinerary();
  assert.match(html,/校验 Agent 已认同当前安排及解释/);
  assert.match(html,/接受仍需通过程序硬约束和版本检查/);
  assert.doesNotMatch(html,/仍有分歧|当前未完成草稿摘要|尚不可接受/);
  h.S.candidate.outcome='incomplete';
  assert.equal(h.canAccept(),false);
  h.S.candidate.outcome='ready';
  h.changeDraft('notes','用户已修改需求');
  assert.equal(h.canAccept(),false);
});

test('any unapproved review blocks inconsistent ready records while absent and null legacy reviews remain compatible', () => {
  const h=harness();
  for(const review of [{},{status:'pending'},{status:'awaiting_planner'},
    {status:'awaiting_adjudication'},{status:'incomplete'},{status:'disputed'}]) {
    h.S.candidate.review=review;
    assert.equal(h.canAccept(),false,JSON.stringify(review));
    assert.match(h.itinerary(),/data-action="accept"[^>]*disabled/);
    if(review.status!=='disputed') assert.match(h.itinerary(),/校验协商未完成/);
  }
  h.S.candidate.review=null;
  assert.equal(h.canAccept(),true);
  assert.doesNotMatch(h.itinerary(),/行程校验与协商/);
  delete h.S.candidate.review;
  assert.equal(h.canAccept(),true);
});

test('unfinished negotiation identifies the pending stage and never calls budget exhaustion a disagreement', () => {
  const h=harness();reviewFixture(h);
  h.S.candidate.review={version:1,status:'incomplete',pending_stage:'final_review',
    initial:h.S.candidate.review.initial,planner_response:h.S.candidate.review.planner_response};
  h.S.candidate.outcome='incomplete';h.S.candidate.issues=[];
  const html=h.itinerary();
  assert.match(html,/协商未完成/);
  assert.match(html,/停在校验复核/);
  assert.match(html,/不代表你的旅行需求无解/);
  assert.doesNotMatch(html,/规划分歧 · 待你协调|仍有分歧|已确认的要求无法同时满足/);
  assert.equal(h.canAccept(),false);
});

test('new final-review concerns appear even without an earlier matching suggestion or planner response', () => {
  const h=harness();reviewFixture(h);
  h.S.candidate.review.final.suggestions.push({suggestion_id:'R3',
    positions:[{day:1,stop_number:1,place_id:'p'}],concern:'修改后的长距离步行需要再确认',
    requirement_basis:'同行老人希望减少步行',proposed_change:'考虑减少本段步行'});
  const html=h.itinerary();
  assert.match(html,/复核发现的新问题 · R3/);
  assert.match(html,/同行老人希望减少步行/);
  assert.match(html,/尚无逐项回应记录/);
});

test('review text, place names and saved draft content are HTML escaped', () => {
  const h=harness();reviewFixture(h);
  const payload='<img src=x onerror="alert(1)">';
  const review=h.S.candidate.review;
  review.initial.summary=payload;review.final.summary=payload;
  for(const report of [review.initial,review.final]) for(const suggestion of report.suggestions) {
    suggestion.suggestion_id=payload;suggestion.concern=payload;
    suggestion.requirement_basis=payload;suggestion.proposed_change=payload;
  }
  review.planner_response.decisions=[{suggestion_id:payload,action:'decline',reason:payload}];
  h.S.candidate.draft.summary=payload;h.S.candidate.draft.stops[0].reason=payload;
  h.S.candidate.places[0].name=payload;
  const html=h.itinerary();
  assert.ok(html.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'));
  assert.doesNotMatch(html,/<img src=x|<script|onerror="alert/);
});

test('reloading saved negotiation history preserves disagreement and does not attach reviews to legacy plans', async () => {
  const h=harness();const disputed=copy(reviewFixture(h));
  const legacy={...copy(disputed),id:'legacy-accepted',status:'ready',outcome:'ready'};
  delete legacy.review;delete legacy.draft;
  legacy.itinerary={stops:[]};
  h.context.fetch=async()=>({ok:true,json:async()=>({candidates:[disputed,legacy]})});
  await h.loadPlanHistory(true);
  assert.deepEqual(copy(h.S.candidate.review),disputed.review);
  assert.equal(h.canAccept(),false);
  h.S.historyOpen=true;
  assert.match(h.planningRecords(),/规划分歧 · 待你协调/);
  h.actions['show-candidate']({dataset:{id:'legacy-accepted'}});
  assert.equal(h.S.candidate.review,undefined);
  assert.equal(h.canAccept(),true);
  assert.doesNotMatch(h.itinerary(),/行程校验与协商/);
});

test('reviewer usage and its saved output cap are visible without replacing historical budgets', () => {
  const h=harness();observedCalls(h);
  const observation=h.S.candidate.usage_observation;
  observation.budget.reviewer_max_output_tokens=6100;
  observation.model_calls[1]={...observation.model_calls[1],role:'reviewer',max_output_tokens:6100};
  h.S.config.planning_budget={max_model_requests:10,run_timeout_seconds:180,
    planner_max_output_tokens:9000,research_max_output_tokens:8000,reviewer_max_output_tokens:9999};
  const html=h.itinerary();
  assert.match(html,/2 · 校验/);
  assert.match(html,/校验 6,100 tokens/);
  assert.doesNotMatch(html,/9,999/);
  h.S.page='settings';h.render();
  const settings=h.elements.get('app').innerHTML;
  assert.match(settings,/排程、研究、校验三个角色共享 10 次模型请求 \/ 180 秒/);
  assert.match(settings,/校验 9999 tokens/);
});
test('research diagnostics distinguish program rejection from model judgment and keep request reasons', () => {
  const h = harness();
  const p = h.S.candidate;
  p.status='conflict'; p.outcome='needs_information'; p.itinerary=null;
  p.research_requests=[{reason:'固定参观时段需要核对开放',needs:[{place_id:'p',day:1,fields:['opening_hours'],question:'用户要求10点到达，需要明确何时开放。'}]}];
  p.research_diagnostics=[{place_id:'p',day:1,field:'opening_hours',origin:'program',attempt:1,code:'opening_time_not_in_quote',message:'提取钟点无原文依据',actual:['10:00'],resolved:true},
    {place_id:'p',day:1,field:'last_admission',origin:'model',attempt:2,code:'excerpt_insufficient',message:'返回摘要没有完整内容'}];
  p.issues=[{place_id:'p',day:1,message:'资料待补充',missing_fields:['last_admission'],diagnostics:[p.research_diagnostics[1]]}];
  const html=h.itinerary();
  assert.match(html,/资料补查记录/);
  assert.match(html,/用户要求10点到达/);
  assert.match(html,/程序检测/);
  assert.match(html,/研究 Agent 判断（待核实）/);
  assert.match(html,/第 2 次核检/);
  assert.match(html,/已解决/);
  assert.match(html,/返回摘要没有完整内容/);
  assert.equal(h.canAccept(),false);
});

test('research diagnostics are escaped and absent from untouched legacy records', () => {
  const h=harness();
  assert.doesNotMatch(h.itinerary(),/资料补查记录/);
  h.S.candidate.research_diagnostics=[{place_id:'p',day:1,field:'opening_hours',origin:'model',message:'<img src=x onerror=alert(1)>',actual:'<script>bad</script>'}];
  const html=h.itinerary();
  assert.match(html,/&lt;img/);
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<img src=x|<script>bad/);
});
test('saved planning runs load without replacing candidates or enabling acceptance', async () => {
  const h=harness();
  h.S.candidate=null;
  h.context.fetch=async (url,options)=>{
    assert.equal(url,'/api/trips/trip-1/planning-runs');
    assert.equal(options.method,'GET');
    return {ok:true,json:async()=>({runs:[{id:'run-1',trip_id:'trip-1',requirements:copy(requirements),requirements_revision:1,base_version:0,status:'interrupted',draft:{summary:'首稿已保存',stops:[{day:1,place_id:'p',start_time:'10:00',end_time:'11:00',reason:'开放时间待核实'}]},places:[{id:'p',name:'过程草稿景点'}],query_requests:[{type:'route',origin_id:'o',destination_id:'p',transport:'mixed'}],updated_at:'2026-11-16T03:00:00Z'}]})};
  };
  await h.actions['planning-runs']();
  const html=h.itinerary();
  assert.match(html,/首稿已保存/);
  assert.match(html,/开放时间待核实/);
  assert.match(html,/执行已中断/);
  assert.match(html,/o → 过程草稿景点/);
  assert.equal(h.S.candidate,null);
  assert.equal(h.canAccept(),false);
  assert.doesNotMatch(html,/data-action="accept"/);
});

test('execution drafts retain their old input and escape all displayed content', () => {
  const h=harness(), payload='<img src=x onerror=alert(1)>';
  h.S.candidate=null;h.S.planningRunsOpen=true;
  h.S.trip.requirements_revision=2;
  h.S.planningRuns=[{id:'run-old',requirements:{...copy(requirements),city:'旧目的地'},requirements_revision:1,base_version:0,status:'running',draft:{summary:payload,stops:[{day:1,place_id:'p',start_time:'10:00',end_time:'11:00',reason:payload}]},places:[{id:'p',name:payload}],query_requests:[{type:'route',origin_id:'p',destination_id:'q',reason:payload}],error:payload}];
  const html=h.itinerary();
  assert.match(html,/旧目的地/);
  assert.match(html,/基于旧需求或旧行程/);
  assert.match(html,/服务已经退出/);
  assert.match(html,/&lt;img/);
  assert.doesNotMatch(html,/<img src=x/);
  assert.equal(h.canAccept(),false);
});

test('draft-first candidate exposes a provisional draft and structured route result', () => {
  const h=harness();
  Object.assign(h.S.candidate,{planning_protocol:'draft_first',status:'conflict',outcome:'incomplete',itinerary:null,draft:{summary:'根据证据待修订',stops:[{day:1,place_id:'p',start_time:'10:00',end_time:'11:00',reason:'路程待核实'}]},assumptions:['营业时间仍待确认'],query_log:[{query:{kind:'route',origin_id:'p',destination_id:'h',transport:'mixed',reason:'核实返回酒店的路段'},status:'ok',cache_hit:true,model_round:1,phase:'first_draft',elapsed_ms:3}]});
  const html=h.itinerary();
  assert.match(html,/草稿与定向查询/);
  assert.match(html,/根据证据待修订/);
  assert.match(html,/核实返回酒店的路段/);
  assert.match(html,/模型第 1 轮/);
  assert.match(html,/营业时间仍待确认/);
  assert.match(html,/复用已查结果/);
  assert.match(html,/data-action="accept"[^>]*disabled/);
  assert.equal(h.canAccept(),false);
});

test('switching trips clears execution drafts and viewing completed runs never accepts them', () => {
  const h=harness();
  h.S.planningRunsOpen=true;
  h.S.planningRuns=[{id:'old-run',status:'completed',candidate_id:'final-candidate',requirements:copy(requirements),requirements_revision:1,base_version:0,draft:{summary:'流程草稿',stops:[{day:1,place_id:'p',reason:'测试'}]}}];
  assert.match(h.itinerary(),/最终结果已存入规划记录/);
  const oldCandidate=h.S.candidate;
  assert.equal(h.S.candidate,oldCandidate);
  h.adoptTrip({id:'new-trip',requirements:copy(requirements),requirements_revision:1,accepted_version:0,accepted_plan:null});
  assert.equal(h.S.planningRuns.length,0);
  assert.equal(h.S.planningRunsOpen,false);
});


test('every preference step exposes the same escaped extra requirements and expected-money labels', () => {
  const h=harness();
  h.changeDraft('notes','必须保留景点 <script>bad()</script>');
  for (const step of [1,2,3]) {
    h.S.step=step;
    const html=h.preferences();
    assert.equal((html.match(/data-field="notes"/g)||[]).length,1);
    assert.match(html,/额外要求与不可突破条件/);
    assert.match(html,/&lt;script&gt;bad\(\)&lt;\/script&gt;/);
    assert.doesNotMatch(html,/<script>/);
    if(step===1)assert.match(html,/整趟期望金额/);
    if(step===3)assert.match(html,/住宿期望金额/);
  }
  assert.equal(h.S.trip.requirements.notes,'');
});

test('explicit strict choices survive navigation and can be independently removed', () => {
  const h=harness();
  const input=(key,checked)=>h.listeners.input({target:{dataset:{strictPreference:key},type:'checkbox',checked}});
  input('max_transport_minutes',true);input('end_time',true);
  assert.deepEqual(copy(h.S.draft.strict_preferences),['max_transport_minutes','end_time']);
  h.S.step=2;
  assert.match(h.preferences(),/data-strict-preference="max_transport_minutes" checked/);
  assert.match(h.preferences(),/data-strict-preference="end_time" checked/);
  input('max_transport_minutes',false);
  assert.deepEqual(copy(h.S.draft.strict_preferences),['end_time']);
  assert.equal(h.canAccept(),false);
  assert.equal(h.S.trip.requirements.strict_preferences,undefined);
});

test('soft deviations remain visible without being hard conflicts and provider text is escaped', () => {
  const h=harness();
  h.S.candidate.preference_policy='soft_targets_v1';
  h.S.candidate.preference_deviations=[{message:'交通107分钟，期望90分钟 <img onerror=bad>'}];
  h.S.candidate.daily_metrics=[{day:1,attraction_count:1,sightseeing_minutes:60,sightseeing_ratio:10,transport_minutes:107,
    available_minutes:690,strict_preferences:['max_attractions'],limits:{max_attractions:2,max_sightseeing_minutes:90,max_sightseeing_ratio:20,max_transport_minutes:90}}];
  const html=h.itinerary();
  assert.match(html,/与期望目标的差异/);
  assert.match(html,/107分钟，期望90分钟 &lt;img onerror=bad&gt;/);
  assert.match(html,/实际 \/ 期望/);
  assert.match(html,/2个 · 不可调整/);
  assert.equal(h.canAccept(),true);
});

test('profile submission retains multiple strict choices instead of the last checkbox only', async () => {
  const h=harness();
  h.S.travelProfile={revision:2,values:copy(requirements)};
  h.context.FormData=class {
    *[Symbol.iterator](){yield* [['hotel_budget','450'],['max_attractions','2'],['max_sightseeing_minutes',''],['max_sightseeing_ratio',''],['max_transport_minutes','90'],['strict_preferences','max_attractions'],['strict_preferences','max_transport_minutes']];}
    getAll(name){return [...this].filter(([key])=>key===name).map(([,value])=>value);}
  };
  let sent;
  h.context.fetch=async(url,options)=>{sent=JSON.parse(options.body);return {ok:true,json:async()=>({revision:3,values:sent.values})};};
  await h.listeners.submit({target:{id:'travel-profile'},preventDefault(){}});
  assert.deepEqual(sent.values.strict_preferences,['max_attractions','max_transport_minutes']);
  assert.equal(sent.values.max_transport_minutes,90);
  assert.equal(sent.values.max_sightseeing_minutes,null);
});


test('structured departure arrival rests and buffer are visible and escaped', () => {
  const h=harness();
  h.S.candidate.itinerary.stops[0].transfer={departure_time:'17:40',arrival_time:'18:16',buffer_minutes:10,
    rest_before:[{start_time:'15:20',end_time:'17:40'}],rest_after:[{start_time:'18:16',end_time:'18:20<script>'}]};
  const html=h.itinerary();
  assert.match(html,/17:40 出发 → 18:16 到达/);
  assert.match(html,/建议缓冲 10 分钟/);
  assert.match(html,/出发前休息 15:20–17:40/);
  assert.match(html,/18:20&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>/);
});

test('missing explicit transfer records are disclosed without inventing rest times', () => {
  const h=harness();
  h.S.candidate.timing_coverage={stop_count:2,explicit_count:1};
  const html=h.itinerary();
  assert.match(html,/已提供 1 \/ 2 站/);
  assert.match(html,/备注中的休息尚不能视为已核对/);
});


test('low-buffer advisory is visible but never blocks an otherwise acceptable plan', () => {
  const h=harness();
  h.S.candidate.buffer_advisories=[{message:'第1天缓冲约3分钟，可酌情多留一点。'}];
  const html=h.itinerary();
  assert.match(html,/缓冲约3分钟/);
  assert.equal(h.canAccept(),true);
  assert.doesNotMatch(html,/data-action="accept"[^>]*disabled/);
});


test('completion-first budgets show omitted caps without null or a false zero', () => {
  const h=harness();observedCalls(h);
  const budget={max_model_requests:null,run_timeout_seconds:600,planner_max_output_tokens:null,
    research_max_output_tokens:null,reviewer_max_output_tokens:null,input_token_limit:null};
  h.S.config.planning_budget=budget;
  h.S.candidate.usage_observation.budget=budget;
  h.S.candidate.usage_observation.model_calls[0].max_output_tokens=null;
  const html=h.itinerary();
  assert.match(html,/供应商默认/);
  assert.match(html,/不设本地上限/);
  assert.doesNotMatch(html,/NaN|undefined|null/);
  h.S.page='settings';h.render();
  const settings=h.elements.get('app').innerHTML;
  assert.match(settings,/不设本地上限/);
  assert.match(settings,/600 秒/);
});
