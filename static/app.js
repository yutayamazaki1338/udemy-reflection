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
  const row = el('div', undefined, 'course-row');
  const b = el('button', c.name, c.id === courseId ? 'selected' : 'course');
  b.type = 'button';
  b.onclick = () => { courseId = c.id; lectureId = null; curriculumPreview = null; render(); };
  const remove = el('button', '削除', 'delete-course');
  remove.type = 'button';
  remove.setAttribute('aria-label', `${c.name}を削除`);
  remove.onclick = () => openCourseDeletion(c.id);
  row.append(b, remove);
  courses.append(row);
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
  const b = el('button', l.title, l.id === lectureId ? 'selected' : 'lecture-title');
  b.onclick = () => { lectureId = l.id; render(); };
  const metadata = latest(l.id) ? `理解 ${latest(l.id).understanding}/5` : '未記録';
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
function appendDeletionTargets(parent, label, records, getText) {
 const details = el('details');
 const summary = el('summary', `${label}の対象 (${records.length}件)`);
 details.append(summary);
 if (records.length) {
  const list = el('ul');
  records.forEach(record => list.append(el('li', getText(record))));
  details.append(list);
 } else {
  details.append(el('p', '対象はありません。', 'muted'));
 }
 parent.append(details);
}
async function openCourseDeletion(courseIdToDelete) {
 let details;
 try {
  details = await api(`/api/course-delete-preview?course_id=${encodeURIComponent(courseIdToDelete)}`);
 } catch (error) {
  message(error.message, true);
  return;
 }
 const backdrop = el('div', undefined, 'modal-backdrop');
 backdrop.setAttribute('role', 'presentation');
 const dialog = el('section', undefined, 'delete-dialog');
 dialog.setAttribute('role', 'dialog');
 dialog.setAttribute('aria-modal', 'true');
 dialog.setAttribute('aria-labelledby', 'delete-dialog-title');
 dialog.append(el('h2', `講座「${details.course.name}」の削除内容を確認`));
 dialog.querySelector('h2').id = 'delete-dialog-title';
 dialog.append(el('p', '講座と関連データは一括で完全削除され、元に戻せません。対象をすべて確認して同意してください。1つでも未選択なら講座削除は中止され、データは削除されません。'));
 appendDeletionTargets(dialog, 'セクション', details.sections, section => section.title);
 appendDeletionTargets(dialog, '単元・セクション情報', details.lectures, lecture => `${lecture.section_title ? lecture.section_title + ' / ' : ''}${lecture.title}${lecture.watched ? '（視聴済み）' : ''}（振り返り ${lecture.reflection_count}件・計画 ${lecture.plan_count}件）`);
 appendDeletionTargets(dialog, '単元の振り返り', details.reflections, reflection => `${reflection.lecture_title} · ${new Date(reflection.recorded_at).toLocaleString('ja-JP')}`);
 appendDeletionTargets(dialog, '講座評価', details.course_ratings, rating => `${new Date(rating.recorded_at).toLocaleString('ja-JP')} · 理解度 ${rating.understanding}/5・講師とのマッチ度 ${rating.instructor_match}/5`);
 appendDeletionTargets(dialog, '学習計画', details.study_plans, plan => `${plan.plan_date} · ${plan.lecture_title}`);
 appendDeletionTargets(dialog, '日次振り返り（関連する計画日）', details.daily_reflections, reflection => `${reflection.reflection_date} · ${new Date(reflection.recorded_at).toLocaleString('ja-JP')}`);
 const groups = [
  ['course', '講座本体', 1],
  ['sections_lectures', '単元・セクション情報', details.lectures.length + details.sections.length],
  ['reflections', '単元の振り返り', details.reflections.length],
  ['course_ratings', '講座評価', details.course_ratings.length],
  ['study_plans', '学習計画', details.study_plans.length],
  ['daily_reflections', '日次振り返り（関連する計画日）', details.daily_reflections.length],
 ];
 const choices = el('fieldset', undefined, 'delete-categories');
 choices.append(el('legend', '削除する対象すべてに同意（未選択があると削除を中止）'));
 const checkboxes = [];
 groups.forEach(([key, label, count]) => {
  const choice = el('label', undefined, 'delete-choice');
  const checkbox = el('input');
  checkbox.type = 'checkbox';
  checkbox.value = key;
  checkbox.setAttribute('aria-label', `${label} ${count}件を削除`);
  checkbox.onchange = () => { confirmDelete.disabled = checkboxes.some(item => !item.checked); };
  checkboxes.push(checkbox);
  choice.append(checkbox, el('span', `${label} (${count}件)`));
  choices.append(choice);
 });
 dialog.append(choices);
 dialog.append(el('p', '関連する計画日の日次振り返りも削除対象です。選択した対象はアーカイブされません。', 'muted'));
 const actions = el('div', undefined, 'delete-actions');
 const cancel = el('button', 'キャンセル');
 cancel.type = 'button';
 cancel.onclick = () => backdrop.remove();
 const confirmDelete = el('button', '同意して講座と関連データを削除', 'danger');
 confirmDelete.type = 'button';
 confirmDelete.disabled = true;
 confirmDelete.onclick = async () => {
  const selected = checkboxes.filter(input => input.checked).map(input => input.value);
  if (selected.length !== groups.length) return;
  const selectedLabels = groups.map(([, label, count]) => `${label} ${count}件`);
  const summary = [
   `講座「${details.course.name}」と関連データを完全削除します。`,
   `削除対象: ${selectedLabels.join('、')}`,
   'アーカイブは作成されません。この操作は元に戻せません。',
   '削除を実行しますか？',
  ].join('\n');
  if (!window.confirm(summary)) return;
  confirmDelete.disabled = true;
  try {
   const result = await api('/api/courses/delete', {preview_token: details.preview_token, delete_categories: selected, confirm_delete: true});
   backdrop.remove();
   if (courseId === courseIdToDelete) {
    courseId = null;
    lectureId = null;
   }
   await refresh();
   message(`講座「${result.course_name}」と関連データを削除しました（関連する日次振り返り ${result.daily_reflections_deleted}件を含む）。`);
  } catch (error) {
   confirmDelete.disabled = false;
   message(error.message, true);
  }
 };
 actions.append(cancel, confirmDelete);
 dialog.append(actions);
 backdrop.append(dialog);
 backdrop.onclick = event => { if (event.target === backdrop) backdrop.remove(); };
 document.body.append(backdrop);
 cancel.focus();
}
function renderCurriculumImport() {
 const input = $('#curriculum-url');
 const fetchButton = $('#fetch-curriculum');
 const preview = $('#curriculum-preview');
 if (!input || !fetchButton || !preview) return;
 input.disabled = curriculumLoading;
 fetchButton.disabled = !input.value.trim() || curriculumLoading;
 preview.replaceChildren();
 if (!curriculumPreview) return;
 input.value = curriculumPreview.course_url;
 preview.append(el('h3', curriculumPreview.course_name));
 preview.append(el('p', `公開件数の照合済み: ${curriculumPreview.expected_lectures} レクチャー`, 'curriculum-count'));
 const list = el('ol');
 curriculumPreview.lectures.forEach(lecture => list.append(el('li', lecture.title)));
 preview.append(list);
 const register = el('button', 'この講座と単元を登録');
 register.type = 'button';
 register.onclick = async () => {
  register.disabled = true;
  try {
   const result = await api('/api/course-curriculum/register', {token: curriculumPreview.token});
   curriculumPreview = null;
   courseId = result.course_id;
   lectureId = null;
   await refresh();
   message(`${result.course_created ? '講座を登録し' : '既存講座に追加し'}、${result.lectures_added}件の単元を追加しました。既存の単元と進捗は変更していません。`);
  } catch (error) {
   register.disabled = false;
   message(error.message, true);
  }
 };
 preview.append(register);
}
async function fetchCurriculumPreview() {
 const input = $('#curriculum-url');
 curriculumLoading = true;
 curriculumPreview = null;
 renderCurriculumImport();
 try {
  curriculumPreview = await api('/api/course-curriculum/preview', {url: input.value});
  input.value = curriculumPreview.course_url;
  renderCurriculumImport();
  message('公開カリキュラムを取得し、件数を照合しました。内容を確認してから登録できます。');
 } catch (error) {
  renderCurriculumImport();
  message(error.message, true);
 } finally {
  curriculumLoading = false;
  renderCurriculumImport();
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
  item.append(el('p', `計画通り: ${r.plan_completed ? 'はい' : 'いいえ'} · 実際の学習時間: ${r.study_minutes}分 · 自己評価: ${r.self_rating}/5`));
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
if ($('#fetch-curriculum')) {
 $('#fetch-curriculum').onclick = fetchCurriculumPreview;
 $('#curriculum-url').oninput = () => { curriculumPreview = null; renderCurriculumImport(); };
}
refresh().catch(e => message('読み込めませんでした: ' + e.message, true));
