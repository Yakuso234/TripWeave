const $ = id => document.getElementById(id);
const form = $('form');
const names = ['人民广场','上海博物馆','人民公园','南京路步行街','外滩'];
document.querySelectorAll('.places').forEach(select => names.forEach(name => select.add(new Option(name,name))));
form.elements.booking_query.value = '上海博物馆';
let trip = null, candidate = null;
const message = (text,error=false) => { $('message').textContent=text; $('message').className=error?'error':''; };
async function api(path, method='GET', body) {
  const r = await fetch('/api'+path,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const data = await r.json();
  if(!r.ok) throw new Error(typeof data.detail==='string'?data.detail:'输入格式有误，请检查日期与时间');
  return data;
}
function req() { const data=Object.fromEntries(new FormData(form)); data.max_activities=Number(data.max_activities); return data; }
function renderPlan(target,c) {
  target.replaceChildren();
  if(!c?.itinerary){target.textContent='尚无已接受行程';return;}
  const summary=document.createElement('p');summary.className='muted';summary.textContent=c.itinerary.summary;target.append(summary);
  c.itinerary.stops.forEach(stop=>{
    const item=document.createElement('article');item.className='stop';
    const time=document.createElement('time');time.textContent=stop.start_time+'–'+stop.end_time;
    const body=document.createElement('div'), title=document.createElement('h3'), reason=document.createElement('p'), source=document.createElement('p');
    const place=c.places.find(p=>p.id===stop.place_id);
    title.textContent=place?.name||stop.place_id;reason.textContent=stop.reason;source.textContent=place?.source||'来源未知';
    body.append(title,reason,source);item.append(time,body);target.append(item);
  });
}
function sync() {
  if(!trip)return;
  localStorage.setItem('tripweave-id',trip.id);
  $('version').textContent=`需求版本 ${trip.requirements_revision} · 已接受行程 v${trip.accepted_version}`;
  renderPlan($('saved'),trip.accepted_plan);
  if(trip.accepted_plan && trip.accepted_plan.requirements_revision!==trip.requirements_revision){
    const note=document.createElement('p');note.className='error';note.textContent='需求已变化，已接受行程尚未更新。';$('saved').prepend(note);
  }
}
async function save() {
  if(trip && Object.entries(req()).every(([k,v])=>trip.requirements[k]===v))return;
  trip=trip?await api(`/trips/${trip.id}/requirements`,'PUT',{...req(),expected_revision:trip.requirements_revision}):await api('/trips','POST',req());
  sync();
}
async function busy(action) {
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{await action();}catch(e){message(e.message,true);}finally{document.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
form.addEventListener('submit',e=>{e.preventDefault();busy(async()=>{
  $('accept').hidden=true;candidate=null;message('正在读取固定地图并执行检查…');
  await save();candidate=await api(`/trips/${trip.id}/plan`,'POST',{mode:'replay'});
  renderPlan($('candidate'),candidate);$('state').textContent=candidate.status==='ready'?'候选待接受':'存在冲突';
  message(candidate.status==='ready'?'固定数据范围内的已知约束检查通过。请查看后接受。':candidate.issues.map(i=>i.message).join('；')||candidate.error,candidate.status!=='ready');
  $('trace').replaceChildren();candidate.trace.forEach(t=>{const li=document.createElement('li');li.textContent=t.detail;$('trace').append(li);});
  $('accept').hidden=candidate.status!=='ready';
});});
$('later').onclick=()=>busy(async()=>{
  const [h,m]=form.elements.start_time.value.split(':').map(Number);
  if(h>=23)throw new Error('无法跨天，请手动设置当天时间');
  form.elements.start_time.value=String(h+1).padStart(2,'0')+':'+String(m).padStart(2,'0');
  await save();candidate=null;$('accept').hidden=true;$('candidate').replaceChildren();$('state').textContent='需求已修改';message('已晚出门一小时，预约保持不变。点击生成实验行程重新规划。');
});
$('accept').onclick=()=>busy(async()=>{trip=await api(`/trips/${trip.id}/candidates/${candidate.id}/accept`,'POST',{});sync();$('accept').hidden=true;$('state').textContent='已接受';message('行程已保存，刷新页面后可以继续查看。');$('accepted').open=true;});
$('new').onclick=()=>{trip=null;candidate=null;localStorage.removeItem('tripweave-id');$('candidate').replaceChildren();$('saved').replaceChildren();$('trace').replaceChildren();$('accept').hidden=true;$('version').textContent='';$('state').textContent='等待规划';message('新行程已就绪，填写需求后生成。');};
form.addEventListener('input',()=>{candidate=null;$('accept').hidden=true;});
busy(async()=>{const c=await api('/config');form.elements.date.value=c.default_date;const id=localStorage.getItem('tripweave-id');if(id){try{trip=await api('/trips/'+id);Object.entries(trip.requirements).forEach(([k,v])=>{if(form.elements[k])form.elements[k].value=v;});sync();message('已恢复保存的行程与需求。');}catch(e){localStorage.removeItem('tripweave-id');message('之前的行程无法恢复，请新建。',true);}}});
