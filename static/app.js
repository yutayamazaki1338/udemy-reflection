let state, courseId = null, lectureId = null, planDate = '';
let curriculumPreview = null, curriculumLoading = false;
const $ = s => document.querySelector(s);
const el = (tag, text, cls) => {
 const e = document.createElement(tag);
 if (text !== undefined) e.textContent = text;
 if (cls) e.className = cls;
 return e;
};
function message(text, error = false) {
 $('#message').textContent = text;
 $('#message').className = error ? 'error' : '';
}
async function api(path, data) {
 const r = await fetch(path, data ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)} : {});
 const result = await r.json();
 if (!r.ok) throw new Error(result.error);
 return result;
}
const latest = id => state.reflections.filter(r => r.lecture_id === id).sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at) || b.id - a.id)[0];
function localNow() {
 const d = new Date();
 return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function today() {
 const d = new Date();
 return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function dateLabel(date) {
 return new Date(`${date}T00:00:00`).toLocaleDateString('ja-JP', {year: 'numeric', month: 'long', day: 'numeric'});
}
async function refresh() {
 state = await api('/api/state');
 if (courseId === null && state.courses.length) courseId = state.courses[0].id;
 if (!planDate) planDate = today();
 render();
}
function render() {
 const page = document.body.dataset.page;
 if (page === 'home') {
  renderHeatmap();
  return;
 }
 if (page === 'planning') {
  renderPlan();
  return;
 }
 renderLearning();
}
function renderLearning() {
 const courses = $('#courses');
 courses.replaceChildren();
 state.courses.forEach(c => {
  const b = el('button', c.name, c.id === courseId ? 'selected' : 'course');
  b.onclick = () => { courseId = c.id; lectureId = null; curriculumPreview = null; render(); };
  courses.append(b);
 });
 $('#lecture-form button').disabled = courseId === null;
 const lectures = state.lectures.filter(l => l.course_id === courseId);
 const watched = lectures.filter(l => l.watched).length;
 const overview = $('#overview');
 overview.replaceChildren();
 overview.append(el('h2', state.courses.find(c => c.id === courseId)?.name || 'まずは講座を登録しましょう'));
 overview.append(el('p', `視聴済み ${watched} / ${lectures.length} 単元 · 振り返り ${lectures.filter(l => latest(l.id)).length} 単元`));
 const progress = el('progress');
 progress.max = lectures.length || 1;
 progress.value = watched;
 progress.setAttribute('aria-label', '講座の視聴進捗');
 overview.append(progress);
 const weak = lectures.filter(l => {
  const r = latest(l.id);
  return r && (r.understanding <= 2 || r.unclear.trim());
 });
 overview.append(el('p', `見直し候補 ${weak.length} 単元（最新の理解度1〜2、または不明点あり）`, 'muted'));
 renderCourseRatings();
 renderCurriculumImport();

 const list = $('#lectures');
 list.replaceChildren();
 const mode = $('#filter').value;
 lectures.filter(l => mode === 'all' || mode === 'unwatched' && !l.watched || mode === 'weak' && weak.includes(l) || mode === 'unreflected' && l.watched && !latest(l.id)).forEach(l => {
  const row = el('div', undefined, 'lecture');
  const label = el('label', undefined, 'check');
  const check = el('input');
  check.type = 'checkbox';
  check.checked = !!l.watched;
  check.setAttribute('aria-label', l.title + ' 視聴済み');
  check.onchange = async () => {
   try { await api('/api/watched', {id: l.id, watched: check.checked}); await refresh(); }
   catch (e) { check.checked = !check.checked; message(e.message, true); }
  };
  label.append(check);
  const section = state.course_sections.find(s => s.id === l.section_id);
  const title = section ? `${section.title} / ${l.title}` : l.title;
  const b = el('button', title, l.id === lectureId ? 'selected' : 'lecture-title');
  b.onclick = () => { lectureId = l.id; render(); };
  const metadata = [l.duration_seconds ? `${Math.floor(l.duration_seconds / 60)}分` : '', latest(l.id) ? `理解 ${latest(l.id).understanding}/5` : '未記録'].filter(Boolean).join(' · ');
  row.append(label, b, el('span', metadata, 'badge'));
  list.append(row);
 });
 if (!list.children.length) list.append(el('p', lectures.length ? 'この条件の単元はありません。' : '学習順に単元を追加してください。', 'muted'));
 const selected = state.lectures.find(l => l.id === lectureId);
 $('#reflection-fields').disabled = !selected;
 $('#journal-title').textContent = selected ? selected.title + ' / 振り返り' : '振り返り';
 $('#selection-hint').hidden = !!selected;
 const history = $('#history');
 history.replaceChildren();
 if (selected) {
  const refs = state.reflections.filter(r => r.lecture_id === lectureId).sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at) || b.id - a.id);
  if (refs.length) {
   const oldest = refs[refs.length - 1];
   history.append(el('p', `理解度の変化 ${oldest.understanding} → ${refs[0].understanding} / 5（${refs.length}回の記録）`));
  }
  function renderCurriculumImport() {
   const course = state.courses.find(c => c.id === courseId);
   const display = $('#course-url-display');
   const fetchButton = $('#fetch-curriculum');
   display.replaceChildren();
   if (course && course.course_url) {
    display.append(el('span', '登録URL: '));
    const link = el('a', course.course_url);
    link.href = course.course_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    display.append(link);
   } else {
    display.textContent = course ? 'この講座にUdemy URLは登録されていません。講座登録時のURL入力、または単元の手動入力をご利用ください。' : '講座を選択してください。';
   }
   fetchButton.disabled = !course || !course.course_url || curriculumLoading;
   const preview = $('#curriculum-preview');
   preview.replaceChildren();
   if (!curriculumPreview || curriculumPreview.course_id !== courseId) return;
   const summary = el('p', `公開件数の照合済み: ${curriculumPreview.expected_sections} セクション / ${curriculumPreview.expected_lectures} レクチャー`, 'curriculum-count');
   preview.append(summary);
   curriculumPreview.sections.forEach(section => {
    const group = el('section', undefined, 'curriculum-section');
    group.append(el('h3', `${section.position}. ${section.title}`));
    const list = el('ol');
    section.lectures.forEach(lecture => {
     const duration = lecture.duration_seconds ? ` · ${Math.floor(lecture.duration_seconds / 60)}分${lecture.duration_seconds % 60 ? ` ${lecture.duration_seconds % 60}秒` : ''}` : ' · 時間情報なし';
     list.append(el('li', `${lecture.title}${duration}`));
    });
    group.append(list);
    preview.append(group);
   });
   const register = el('button', 'プレビューの内容を単元として一括登録');
   register.type = 'button';
   register.onclick = async () => {
    register.disabled = true;
    try {
     const result = await api('/api/course-curriculum/register', {token: curriculumPreview.token});
     curriculumPreview = null;
     await refresh();
     message(`${result.sections_added}セクション、${result.lectures_added}レクチャーを追加しました。既存の進捗は変更していません。`);
    } catch (error) {
     register.disabled = false;
     message(error.message, true);
    }
   };
   preview.append(register);
  }
  async function fetchCurriculumPreview() {
   const button = $('#fetch-curriculum');
   curriculumLoading = true;
   button.disabled = true;
   curriculumPreview = null;
   renderCurriculumImport();
   try {
    curriculumPreview = await api('/api/course-curriculum/preview', {course_id: courseId});
    renderCurriculumImport();
    message('公開カリキュラムを取得し、公開件数との照合が完了しました。内容を確認してから一括登録できます。');
   } catch (error) {
    renderCurriculumImport();
    message(error.message, true);
   } finally {
    curriculumLoading = false;
    renderCurriculumImport();
   }
  }
  refs.forEach(r => {
   const box = el('article', undefined, 'entry');
   box.append(el('h4', new Date(r.recorded_at).toLocaleString('ja-JP')), el('p', `理解度 ${r.understanding}/5 · 満足度 ${r.satisfaction}/5`));
   [['理解したこと', 'understood'], ['不明点', 'unclear'], ['評価の理由', 'reason'], ['自由記述（追加で自身で調べて理解したこと）', 'notes']].forEach(([label, key]) => {
    if (r[key]) box.append(el('strong', label), el('p', r[key]));
   });
   history.append(box);
  });
  if (!refs.length) history.append(el('p', '最初の気づきを残しましょう。', 'muted'));
 }
}
function renderCourseRatings() {
 const fields = $('#course-rating-fields');
 fields.disabled = courseId === null;
 const form = $('#course-rating-form');
 form.querySelector('[name=recorded_at]').value = form.querySelector('[name=recorded_at]').value || localNow();
 const history = $('#course-rating-history');
 history.replaceChildren();
 const ratings = state.course_ratings.filter(r => r.course_id === courseId).sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at) || b.id - a.id);
 if (!courseId) {
  history.append(el('p', '講座を登録すると評価できます。', 'muted'));
  return;
 }
 if (!ratings.length) history.append(el('p', '講座全体の評価を記録しましょう。', 'muted'));
 ratings.forEach(r => {
  const box = el('article', undefined, 'entry');
  box.append(el('h4', new Date(r.recorded_at).toLocaleString('ja-JP')));
  box.append(el('p', `理解度 ${r.understanding}/5 · 講師とのマッチ度 ${r.instructor_match}/5`));
  box.append(el('strong', '理解度の理由'), el('p', r.understanding_reason), el('strong', '講師とのマッチ度の理由'), el('p', r.instructor_match_reason));
  history.append(box);
 });
}
function updatePlanLectures() {
 const select = $('#plan-lecture');
 select.replaceChildren();
 select.append(new Option('単元を選択', ''));
 state.lectures.filter(l => l.course_id === Number($('#plan-course').value)).forEach(l => select.append(new Option(l.title, l.id)));
}
function renderPlan() {
 const planCourse = $('#plan-course');
 const selectedCourse = planCourse.value;
 planCourse.replaceChildren(new Option('講座を選択', ''));
 state.courses.forEach(c => planCourse.append(new Option(c.name, c.id)));
 planCourse.value = state.courses.some(c => String(c.id) === selectedCourse) ? selectedCourse : '';
 updatePlanLectures();
 $('#plan-date').value = planDate;
 $('#plan-heading').textContent = `${dateLabel(planDate)}の学習計画`;
 $('#daily-reflection-date').textContent = `${dateLabel(planDate)}の1日の学習を終了`;
 const plans = state.study_plans.filter(p => p.plan_date === planDate);
 const planList = $('#plan-list');
 planList.replaceChildren();
 if (!plans.length) planList.append(el('p', 'この日の学習計画はありません。', 'muted'));
 plans.forEach(p => {
  const lecture = state.lectures.find(l => l.id === p.lecture_id);
  const course = lecture && state.courses.find(c => c.id === lecture.course_id);
  if (lecture && course) planList.append(el('p', `${course.name} / ${lecture.title}`, 'plan-item'));
 });
 const dailyHistory = $('#daily-reflection-history');
 dailyHistory.replaceChildren();
 const entries = state.daily_reflections.filter(r => r.reflection_date === planDate).sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at) || b.id - a.id);
 if (!entries.length) dailyHistory.append(el('p', '学習終了後に1日の振り返りを記録できます。', 'muted'));
 entries.forEach(r => {
  const item = el('article', undefined, 'entry');
  item.append(el('h4', new Date(r.recorded_at).toLocaleString('ja-JP')));
  item.append(el('p', `計画通り: ${r.plan_completed ? 'はい' : 'いいえ'} · 学習時間: ${r.study_minutes}分 · 自己評価: ${r.self_rating}/5`));
  item.append(el('strong', '改善点'), el('p', r.improvement), el('strong', '明日の計画'), el('p', r.tomorrow_plan));
  dailyHistory.append(item);
 });
 const todayHasPlan = state.study_plans.some(p => p.plan_date === today());
 $('#plan-prompt').hidden = todayHasPlan;
 $('#plan-prompt').textContent = todayHasPlan ? '' : '今日の学習計画がまだありません。今日の計画を登録しましょう。';
 $('#daily-reflection-form [name=plan_completed]').disabled = plans.length === 0;
}
function renderHeatmap() {
 const grid = $('#heatmap');
 grid.replaceChildren();
 const completedDates = new Set(state.daily_reflections.map(r => r.reflection_date));
 const start = new Date();
 start.setDate(start.getDate() - 27);
 for (let i = 0; i < 28; i++) {
  const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
  const date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const cell = el('span', String(d.getDate()), 'heat-cell' + (completedDates.has(date) ? ' completed' : ''));
  cell.title = `${date}${completedDates.has(date) ? ' · 1日の振り返り完了' : ' · 未完了'}`;
  cell.setAttribute('aria-label', cell.title);
  grid.append(cell);
 }
}
function bindForm(selector, path, build, successMessage = '保存しました。') {
 const target = $(selector);
 if (!target) return;
 target.onsubmit = async e => {
  e.preventDefault();
  const form = e.target, button = form.querySelector('button');
  button.disabled = true;
  try {
   await api(path, build(Object.fromEntries(new FormData(form))));
   form.reset();
   if (selector === '#reflection-form') form.querySelector('[name=recorded_at]').value = localNow();
   if (selector === '#course-rating-form') form.querySelector('[name=recorded_at]').value = localNow();
   await refresh();
   message(successMessage);
  } catch (err) { message(err.message, true); }
  finally { button.disabled = false; }
 };
}
bindForm('#course-form', '/api/courses', d => d);
bindForm('#lecture-form', '/api/lectures', d => ({...d, course_id: courseId}));
bindForm('#reflection-form', '/api/reflections', d => ({...d, lecture_id: lectureId, satisfaction: Number(d.satisfaction), understanding: Number(d.understanding), recorded_at: new Date(d.recorded_at).toISOString()}));
bindForm('#course-rating-form', '/api/course-ratings', d => ({...d, course_id: courseId, understanding: Number(d.understanding), instructor_match: Number(d.instructor_match), recorded_at: new Date(d.recorded_at).toISOString()}), '講座全体の評価を保存しました。');
bindForm('#plan-form', '/api/plans', d => d.lecture_id ? {plan_date: planDate, lecture_id: Number(d.lecture_id)} : {plan_date: planDate, course_name: d.course_name, lecture_title: d.lecture_title}, '学習計画を保存しました。');
bindForm('#daily-reflection-form', '/api/daily-reflections', d => ({...d, reflection_date: planDate, plan_completed: d.plan_completed === 'true', study_minutes: Number(d.study_minutes), self_rating: Number(d.self_rating), recorded_at: new Date().toISOString()}), '1日の振り返りを保存しました。');
if ($('#filter')) $('#filter').onchange = render;
if ($('#plan-course')) $('#plan-course').onchange = updatePlanLectures;
if ($('#plan-date')) $('#plan-date').onchange = () => { planDate = $('#plan-date').value || today(); render(); };
if ($('#reflection-form')) $('#reflection-form [name=recorded_at]').value = localNow();
if ($('#course-rating-form')) $('#course-rating-form [name=recorded_at]').value = localNow();
if ($('#fetch-curriculum')) $('#fetch-curriculum').onclick = fetchCurriculumPreview;
refresh().catch(e => message('読み込めませんでした: ' + e.message, true));
