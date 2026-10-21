let state, courseId = null, lectureId = null, planDate = '', selectedDate = '';
let curriculumPreview = null, curriculumLoading = false;
const AI_REFLECTION_DAYS = 14;
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
function dateKey(date) {
 return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
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
  loadAiReflection();
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
 if (!state.courses.length) courses.append(el('p', '講座はまだありません。「新しい講座を登録」から始めましょう。', 'muted'));
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
 const course = state.courses.find(c => c.id === courseId);
 const overview = $('#overview');
 overview.replaceChildren();
 overview.append(el('h2', course?.name || '講座の学習'));
 if (course && lectures.length) {
  const reviewed = lectures.map(lecture => latest(lecture.id)).filter(Boolean);
  const metrics = el('div', undefined, 'learning-metrics');
  const addMetric = (label, value, detail) => {
   const item = el('div', undefined, 'learning-metric');
   item.append(el('span', label), el('strong', value));
   if (detail) item.append(el('small', detail));
   metrics.append(item);
  };
  addMetric('視聴の進捗', `${watched} / ${lectures.length}`, '単元');
  addMetric('振り返り', `${reviewed.length} / ${lectures.length}`, '記録した単元');
  if (reviewed.length) {
   const average = reviewed.reduce((sum, reflection) => sum + reflection.understanding, 0) / reviewed.length;
   addMetric('最新の理解度平均', `${average.toFixed(1)} / 5`, `振り返り ${reviewed.length}件の最新評価 · 視聴進捗とは別`);
  }
  overview.append(metrics);
  const progress = el('progress');
  progress.max = lectures.length;
  progress.value = watched;
  progress.setAttribute('aria-label', `視聴の進捗: ${watched} / ${lectures.length}単元`);
  overview.append(progress);
  const progressNote = el('p', '視聴済みは再生チェック、理解度は振り返りの自己評価を表します。', 'muted');
  overview.append(progressNote);
 } else if (course) {
  overview.append(el('p', '単元はまだありません。「講座登録」で単元を追加しましょう。', 'muted'));
 }
 const weak = lectures.filter(l => {
  const r = latest(l.id);
  return r && (r.understanding <= 2 || r.unclear.trim());
 });
 if (weak.length) {
  const reviewButton = el('button', `要見直し ${weak.length}単元`);
  reviewButton.type = 'button';
  reviewButton.className = 'review-shortcut';
  reviewButton.onclick = () => { $('#filter').value = 'weak'; renderLearning(); };
  overview.append(reviewButton);
 }
 const nextLecture = lectures.find(lecture => !lecture.watched);
 if (nextLecture) {
  const next = el('button', `次の単元: ${nextLecture.title}`, 'next-lecture');
  next.type = 'button';
  next.onclick = () => {
   lectureId = nextLecture.id;
   $('#filter').value = 'all';
   renderLearning();
   $('#journal').scrollIntoView({behavior: 'smooth', block: 'start'});
  };
  overview.append(next);
 }
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
 $('#journal-title').textContent = selected ? selected.title + ' / 振り返り' : '単元の振り返り';
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
  selectCourseRegistrationMode('manual');
  message(`${error.message} 手入力に切り替えて登録できます。`, true);
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
 const weekdays = ['日', '月', '火', '水', '木', '金', '土'];
 const weekdayRow = el('div', undefined, 'heatmap-weekdays');
 weekdayRow.setAttribute('aria-hidden', 'true');
 for (let i = 0; i < 7; i++) {
  const weekday = weekdays[(start.getDay() + i) % 7];
  weekdayRow.append(el('span', weekday, 'heatmap-weekday'));
 }
 grid.append(weekdayRow);
 if (!selectedDate) selectedDate = today();
 for (let i = 0; i < 28; i++) {
  const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
  const date = dateKey(d);
  const recorded = completedDates.has(date);
  const isToday = date === today();
  const cell = el('button', undefined, `heat-cell ${recorded ? 'completed' : 'empty'}${isToday ? ' today' : ''}${date === selectedDate ? ' selected' : ''}`);
  cell.type = 'button';
  cell.dataset.date = date;
  cell.dataset.recorded = String(recorded);
  cell.setAttribute('aria-pressed', String(date === selectedDate));
  cell.setAttribute('aria-label', `${dateLabel(date)}・${recorded ? '1日の振り返りあり' : '振り返りなし'}${isToday ? '・今日' : ''}`);
  cell.append(el('span', String(d.getDate()), 'heat-day-number'));
  if (isToday) cell.append(el('span', '今日', 'heat-day-today'));
  cell.onclick = () => {
   selectedDate = date;
   renderHeatmap();
  };
  grid.append(cell);
 }
 renderSelectedDayReflections();
}
function renderSelectedDayReflections() {
 const container = $('#selected-day-reflections');
 container.replaceChildren();
 const dateTitle = el('h3', `${dateLabel(selectedDate)}の振り返り`);
 dateTitle.id = 'selected-day-title';
 container.append(dateTitle);
 const entries = state.daily_reflections
  .filter(reflection => reflection.reflection_date === selectedDate)
  .sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at) || b.id - a.id);
 if (!entries.length) {
  container.append(el('p', 'この日の学習振り返りはありません。', 'muted'));
  return;
 }
 entries.forEach(reflection => {
  const item = el('article', undefined, 'entry');
  item.append(el('h4', new Date(reflection.recorded_at).toLocaleString('ja-JP')));
  item.append(el('p', `計画通り: ${reflection.plan_completed ? 'はい' : 'いいえ'} · 実際の学習時間: ${reflection.study_minutes}分 · 自己評価: ${reflection.self_rating}/5`));
  item.append(el('strong', '改善点'), el('p', reflection.improvement), el('strong', '明日の計画'), el('p', reflection.tomorrow_plan));
  container.append(item);
 });
}
async function loadAiReflection(actionError = '') {
 const container = $('#ai-reflection');
 if (!container) return;
 try {
  let data = await api('/api/ai-reflection');
  const shouldAutoGenerate = data.consented && data.api_key_configured &&
   !data.auto_checked && !data.in_progress && !data.latest_result;
  if (shouldAutoGenerate) {
   renderAiReflection({...data, in_progress: true});
   try {
    await api('/api/ai-reflection/auto', {});
   } catch (error) {
    actionError = error.message;
   }
   data = await api('/api/ai-reflection');
  }
  renderAiReflection(data, actionError);
 } catch (error) {
  renderAiReflection(null, error.message);
 }
}
function renderAiReflection(data, loadError = '') {
 const container = $('#ai-reflection');
 container.replaceChildren();
 const title = el('h2', 'AI学習リフレクション');
 title.id = 'ai-reflection-title';
 container.append(title);
 if (!data) {
  container.append(el('p', `AI分析の状態を取得できませんでした: ${loadError}`, 'error'));
  return;
 }
 container.append(el(
  'p',
  `接続状態: OpenAI APIキー ${data.api_key_configured ? '設定済み' : '未設定'} · 送信同意 ${data.consented ? '有効' : '未同意'} · 分析 ${data.latest_result ? '生成済み' : '未生成'}`,
  'muted',
 ));
 container.append(el('p', '対象期間: ' + dateLabel(data.period_start) + '〜' + dateLabel(data.period_end) + ' · 自動分析はAsia/Tokyoの日付ごとに最大1回です。', 'muted'));
 if (!data.consented) {
  container.append(el('p', '現在は未接続・未生成です。分析を有効にするには、記録をOpenAIへ送信することへの明示的な同意が必要です。', 'ai-reflection-status'));
 } else if (!data.api_key_configured) {
  container.append(el('p', '同意済みですが、サーバーにOPENAI_API_KEYが設定されていないため、APIは呼び出していません。キーはブラウザーへ送信されません。', 'ai-reflection-status'));
 } else {
  container.append(el('p', 'OpenAIへの送信に同意済み · APIキーはサーバー環境変数から読み込みます。', 'ai-reflection-status'));
 }
 if (!data.yesterday_has_record) {
  container.append(el('p', '昨日の学習記録はありません。', 'ai-reflection-yesterday-missing'));
 }
 if (data.analysis_record_count === 0) {
  container.append(el('p', '分析対象となる直近14日間の学習記録はありません。分析対象なしのため、APIは呼び出しません。', 'ai-reflection-no-data'));
 } else {
  container.append(el('p', `直近${AI_REFLECTION_DAYS}日間の記録 ${data.analysis_record_count}件 · 対象日: ${data.analysis_dates.map(dateLabel).join('、')}。記録のある日だけを分析し、空白日は否定的に評価しません。`, 'muted'));
 }
 if (data.auto_count) {
  container.append(el('p', `本日の自動API呼び出し: ${data.auto_count}/1`, 'muted'));
 }
 if (data.in_progress) container.append(el('p', '分析を実行中です。画面を再読み込みすると保存済みの状態を確認できます。', 'muted'));
 if (data.stale) {
  container.append(el('p', '保存後に学習記録または今日の計画が変わったため、この分析結果は古くなっています。既存結果は保持しています。自動再分析は行いません。', 'ai-reflection-stale'));
 }
 if (data.last_error) container.append(el('p', `最新の分析に失敗しました: ${data.last_error} 自動再試行は行いません。`, 'error'));
 if (loadError && loadError !== data.last_error) container.append(el('p', `分析操作に失敗しました: ${loadError}`, 'error'));
 const sections = [
  [
   'A. 昨日の振り返り',
   data.latest_result?.sections.yesterday_reflection || (data.yesterday_has_record ? '分析結果はまだありません。' : '昨日の学習記録はありません。'),
   data.yesterday_has_record ? `根拠日: ${dateLabel(data.yesterday_date)}` : '根拠日: 昨日の記録なし',
  ],
  [
   'B. 過去の記録から見えること',
   data.latest_result?.sections.past_observations || '分析結果はまだありません。',
   `分析対象日: ${data.analysis_dates.map(dateLabel).join('、') || 'なし'}`,
  ],
  [
   'C. 今日の学習に向けて',
   data.latest_result?.sections.today_suggestions || '分析結果はまだありません。',
   `対象日: ${dateLabel(data.target_date)}`,
  ],
 ];
 sections.forEach(([title, description, evidence]) => {
  const section = el('section', undefined, 'ai-reflection-section');
  section.append(el('h3', title), el('p', description), el('small', evidence, 'muted'));
  container.append(section);
 });
 if (data.latest_result) {
  const run = data.latest_result;
  container.append(el('p', `生成日時: ${new Date(run.generated_at).toLocaleString('ja-JP')} · ${run.run_type === 'auto' ? '自動生成' : '手動再分析'} · ${run.model} · 根拠日: ${data.analysis_dates.map(dateLabel).join('、') || 'なし'}`, 'muted'));
 }
 if (data.consented && data.api_key_configured) {
  if (data.analysis_record_count > 0 && !data.in_progress) {
   const manualButton = el('button', '手動で再分析（追加のAPI利用料金が発生します）');
   manualButton.type = 'button';
   manualButton.onclick = async () => {
    if (!window.confirm('記録内容をOpenAIへ再送信して再分析します。追加のAPI利用料金が発生する可能性があります。続行しますか？')) return;
    manualButton.disabled = true;
    let actionError = '';
    try {
     await api('/api/ai-reflection/manual', {});
    } catch (error) {
     actionError = error.message;
    }
    await loadAiReflection(actionError);
   };
   container.append(manualButton);
  }
  const revoke = el('button', 'OpenAI送信への同意を取り消す');
  revoke.type = 'button';
  revoke.className = 'ai-consent-revoke';
  revoke.onclick = async () => {
   try {
    await api('/api/ai-reflection/consent', {consent: false});
    await loadAiReflection();
   } catch (error) {
    renderAiReflection(data, error.message);
   }
  };
  container.append(revoke);
 } else if (!data.consented) {
  const form = el('form', undefined, 'ai-consent-form');
  const label = el('label');
  const checkbox = el('input');
  checkbox.type = 'checkbox';
  checkbox.required = true;
  checkbox.name = 'consent';
  const copy = el('span', '日次振り返りの内容（記録日時、自己評価、実際の学習時間、計画達成、改善点、明日の計画）と今日の学習計画（講座名・単元名）がOpenAIへ送信されることに同意します。空白日は否定的な評価に使いません。');
  label.append(checkbox, copy);
  form.append(el('p', '有効化後、対象記録がある日の初回アクセス時に自動分析を1回行い、OpenAIの利用料金が発生する可能性があります。手動再分析は追加リクエストです。', 'ai-cost-notice'));
  const submit = el('button', '内容の送信に同意して有効化');
  submit.type = 'submit';
  submit.disabled = true;
  checkbox.onchange = () => { submit.disabled = !checkbox.checked; };
  form.append(label, submit);
  form.onsubmit = async event => {
   event.preventDefault();
   submit.disabled = true;
   try {
    await api('/api/ai-reflection/consent', {consent: true});
    await loadAiReflection();
   } catch (error) {
    renderAiReflection(data, error.message);
   }
  };
  container.append(form);
 }
 container.append(el('p', '分析は記録内容に基づく参考情報です。自己評価は能力の証明ではありません。手動再分析はその都度追加費用が発生し得ます。', 'muted'));
}
function selectCourseRegistrationMode(mode, focus = false) {
 const selectedTabId = mode === 'udemy' ? 'udemy-course-tab' : 'manual-course-tab';
 const tabs = [...document.querySelectorAll('.registration-tabs [role=tab]')];
 tabs.forEach(tab => {
  const selected = tab.id === selectedTabId;
  tab.setAttribute('aria-selected', String(selected));
  tab.tabIndex = selected ? 0 : -1;
  const panel = document.getElementById(tab.getAttribute('aria-controls'));
  panel.hidden = !selected;
  if (selected && focus) tab.focus();
 });
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
document.querySelectorAll('.registration-tabs [role=tab]').forEach((tab, index, tabs) => {
 tab.onclick = () => selectCourseRegistrationMode(tab.id === 'udemy-course-tab' ? 'udemy' : 'manual');
 tab.onkeydown = event => {
  const nextIndex = event.key === 'ArrowRight' ? (index + 1) % tabs.length :
   event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length :
   event.key === 'Home' ? 0 :
   event.key === 'End' ? tabs.length - 1 : -1;
  if (nextIndex >= 0) {
   event.preventDefault();
   selectCourseRegistrationMode(tabs[nextIndex].id === 'udemy-course-tab' ? 'udemy' : 'manual', true);
  }
 };
});
selectCourseRegistrationMode('manual');
refresh().catch(e => message('読み込めませんでした: ' + e.message, true));
