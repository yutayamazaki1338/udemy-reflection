let state, courseId = null, lectureId = null;
const $ = s => document.querySelector(s);
const el = (tag, text, cls) => {const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e;};
function message(text, error=false){$('#message').textContent=text; $('#message').className=error?'error':'';}
async function api(path, data){const r=await fetch(path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const result=await r.json();if(!r.ok)throw new Error(result.error);return result;}
const latest = id => state.reflections.filter(r=>r.lecture_id===id).sort((a,b)=>Date.parse(b.recorded_at)-Date.parse(a.recorded_at)||b.id-a.id)[0];
function localNow(){const d=new Date(); return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
async function refresh(){state=await api('/api/state');if(courseId===null&&state.courses.length)courseId=state.courses[0].id;render();}
function render(){
 const courses=$('#courses');courses.replaceChildren();
 state.courses.forEach(c=>{const b=el('button',c.name,c.id===courseId?'selected':'course');b.onclick=()=>{courseId=c.id;lectureId=null;render();};courses.append(b);});
 $('#lecture-form button').disabled=courseId===null;
 const lectures=state.lectures.filter(l=>l.course_id===courseId), watched=lectures.filter(l=>l.watched).length;
 const overview=$('#overview');overview.replaceChildren();overview.append(el('h2',state.courses.find(c=>c.id===courseId)?.name||'まずは講座を登録しましょう'));
 overview.append(el('p',`視聴済み ${watched} / ${lectures.length} 単元 · 振り返り ${lectures.filter(l=>latest(l.id)).length} 単元`));
 const progress=el('progress');progress.max=lectures.length||1;progress.value=watched;progress.setAttribute('aria-label','講座の視聴進捗');overview.append(progress);
 const weak=lectures.filter(l=>{const r=latest(l.id);return r&&(r.understanding<=2||r.unclear.trim());});overview.append(el('p',`見直し候補 ${weak.length} 単元（最新の理解度1〜2、または不明点あり）`,'muted'));
 const list=$('#lectures');list.replaceChildren();
 const mode=$('#filter').value;
 lectures.filter(l=> mode==='all'||mode==='unwatched'&&!l.watched||mode==='weak'&&weak.includes(l)||mode==='unreflected'&&l.watched&&!latest(l.id)).forEach(l=>{
 const row=el('div',undefined,'lecture');const label=el('label',undefined,'check');const check=el('input');check.type='checkbox';check.checked=!!l.watched;check.setAttribute('aria-label',l.title+' 視聴済み');check.onchange=async()=>{try{await api('/api/watched',{id:l.id,watched:check.checked});await refresh();}catch(e){check.checked=!check.checked;message(e.message,true);}};label.append(check);const b=el('button',l.title,l.id===lectureId?'selected':'lecture-title');b.onclick=()=>{lectureId=l.id;render();};row.append(label,b);const r=latest(l.id);row.append(el('span',r?`理解 ${r.understanding}/5`:'未記録','badge'));list.append(row);
 });
 if(!list.children.length)list.append(el('p',lectures.length?'この条件の単元はありません。':'学習順に単元を追加してください。','muted'));
 const selected=state.lectures.find(l=>l.id===lectureId);$('#reflection-fields').disabled=!selected;$('#journal-title').textContent=selected?selected.title+' / 振り返り':'振り返り';$('#selection-hint').hidden=!!selected;
 const history=$('#history');history.replaceChildren();
 if(selected){const refs=state.reflections.filter(r=>r.lecture_id===lectureId).sort((a,b)=>Date.parse(b.recorded_at)-Date.parse(a.recorded_at)||b.id-a.id);
 if(refs.length){const oldest=refs[refs.length-1];history.append(el('p',`理解度の変化 ${oldest.understanding} → ${refs[0].understanding} / 5（${refs.length}回の記録）`));}
 refs.forEach(r=>{const box=el('article',undefined,'entry');box.append(el('h4',new Date(r.recorded_at).toLocaleString('ja-JP')),el('p',`理解度 ${r.understanding}/5 · 満足度 ${r.satisfaction}/5`));[['理解したこと','understood'],['不明点','unclear'],['評価の理由','reason'],['メモ','notes']].forEach(([label,key])=>{if(r[key])box.append(el('strong',label),el('p',r[key]));});history.append(box);});if(!refs.length)history.append(el('p','最初の気づきを残しましょう。','muted'));}
}
function bindForm(selector, path, build){$(selector).onsubmit=async e=>{e.preventDefault();const form=e.target, button=form.querySelector('button');button.disabled=true;try{await api(path,build(Object.fromEntries(new FormData(form))));form.reset();$('#reflection-form [name=recorded_at]').value=localNow();await refresh();message('保存しました。');}catch(err){message(err.message,true);}finally{button.disabled=false;}};}
bindForm('#course-form','/api/courses',d=>d);
bindForm('#lecture-form','/api/lectures',d=>({...d,course_id:courseId}));
bindForm('#reflection-form','/api/reflections',d=>({...d,lecture_id:lectureId,satisfaction:Number(d.satisfaction),understanding:Number(d.understanding),recorded_at:new Date(d.recorded_at).toISOString()}));
$('#filter').onchange=render;
$('#reflection-form [name=recorded_at]').value=localNow();refresh().catch(e=>message('読み込めませんでした: '+e.message,true));
