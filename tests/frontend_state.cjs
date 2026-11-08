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
  booking_end:'15:00', max_activities:3,
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
  const app = vm.runInContext('({S,actions,dirty,stalePlan,canAccept,planRequirements,currentHotels,hotelQueryContext,exportPayload,changeDraft,itinerary,hotels,dateFor,stopHeading,loadPlanHistory,planningRecords,adoptTrip,render})', context);
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
