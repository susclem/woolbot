/* 羊毛日历前端 —— 无框架、无 CDN，NAS 断网也能用。 */
'use strict';

// ---------------------------------------------------------------- 工具

const $ = (id) => document.getElementById(id);

const pad = (n) => String(n).padStart(2, '0');
const fmtDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const fmtMonth = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;

function parseDay(s) {
  const [y, m, d] = s.slice(0, 10).split('-').map(Number);
  return new Date(y, m - 1, d, 12, 0, 0); // 中午，避开夏令时/时区边界
}

const WEEKDAY = ['日', '一', '二', '三', '四', '五', '六'];

function dayLabel(d) {
  return `${d.getMonth() + 1}月${d.getDate()}日 周${WEEKDAY[d.getDay()]}`;
}

function hhmm(minuteStr) {
  return minuteStr ? minuteStr.slice(11, 16) : '';
}

function relDay(dayStr, today) {
  const diff = Math.round((parseDay(dayStr) - parseDay(today)) / 86400000);
  if (diff === 0) return '今天';
  if (diff === 1) return '明天';
  if (diff === 2) return '后天';
  if (diff === -1) return '昨天';
  return diff > 0 ? `${diff}天后` : `${-diff}天前`;
}

// ---------------------------------------------------------------- 白天 / 夜间

const THEME_KEY = 'woolbot_theme';

/** 现在这套是白天还是夜间 —— 只认 <html data-theme>，那是唯一权威。 */
function themeNow() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function themeIsSaved() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === 'dark' || t === 'light';
  } catch (e) {
    return false; // 无痕模式下 localStorage 会抛，当成「没存过」
  }
}

/** 按钮上的符号跟着**当前**模式走：白天 ☀️、夜间 🌙。
    光看符号容易读反，所以 title/aria-label 写的是点下去会发生什么。 */
function paintTheme() {
  const dark = themeNow() === 'dark';
  const btn = $('btnTheme');
  if (!btn) return;
  btn.textContent = dark ? '🌙' : '☀️';
  const tip = dark ? '切换到白天模式' : '切换到夜间模式';
  btn.title = tip;
  btn.setAttribute('aria-label', tip);
}

function setTheme(mode, save) {
  const next = mode === 'dark' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  if (save) {
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* 存不下就只在这次生效 */ }
  }
  paintTheme();
}

function toggleTheme() {
  setTheme(themeNow() === 'dark' ? 'light' : 'dark', true);
}

/** 没自己选过的时候跟着系统走：白天在办公室、晚上回家，系统切了这里也跟着切。 */
function watchSystemTheme() {
  if (!window.matchMedia) return;
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const follow = () => { if (!themeIsSaved()) setTheme(mq.matches ? 'dark' : 'light', false); };
  if (mq.addEventListener) mq.addEventListener('change', follow);
  else if (mq.addListener) mq.addListener(follow);
}

// ---------------------------------------------------------------- token

const TOKEN_KEY = 'woolbot_token';
(function grabToken() {
  const url = new URL(location.href);
  const t = url.searchParams.get('token');
  if (t) {
    localStorage.setItem(TOKEN_KEY, t);
    url.searchParams.delete('token');
    history.replaceState(null, '', url.pathname + (url.search || '') + url.hash);
  }
})();

/** 把「当前看哪个月 / 哪个视图」写回地址栏，刷新和分享都不丢。 */
function syncUrl() {
  const url = new URL(location.href);
  const m = fmtMonth(state.cursor);
  if (m === fmtMonth(new Date())) url.searchParams.delete('month');
  else url.searchParams.set('month', m);
  if (state.view === 'month') url.searchParams.delete('view');
  else url.searchParams.set('view', state.view);
  // 选了哪一天也写回去：别人打开这个链接看到的是同一天（默认的今天不写，保持地址干净）
  if (state.selected && state.selected !== state.today) url.searchParams.set('day', state.selected);
  else url.searchParams.delete('day');
  history.replaceState(null, '', url.pathname + (url.search || ''));
}

async function api(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers['X-Token'] = token;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  let body = null;
  try { body = await res.json(); } catch (_) { /* 可能不是 JSON */ }
  if (!res.ok || (body && body.ok === false)) {
    if (res.status === 401 && body && body.login_required) {
      // 会话没了（刚在设置里换了秘钥、或者 7 天到期）。整个页面重拉一次，
      // 服务端会把登录表单发下来 —— 前端自己再画一个登录框，等于把鉴权
      // 逻辑写第二遍，两处迟早对不上。
      location.reload();
    }
    if (res.status === 403 && body && body.banned) {
      // 被「秘钥错误保护」关了 24 小时。同样整页重拉：服务端会把那张
      // 「暂时不许访问」的页面发下来，前端不用自己编这句话。
      location.reload();
    }
    const msg = (body && body.error) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return body;
}

// ---------------------------------------------------------------- 状态

const state = {
  view: 'month',
  cursor: new Date(),          // 当前月份游标
  selected: null,              // 选中的某天 'YYYY-MM-DD'
  today: fmtDay(new Date()),
  rows: [],
  days: {},                    // 日历附注：'YYYY-MM-DD' → {lunar,term,festival,holiday,text,kind}
  filters: { status: 'all', platform: 'all', keyword: '' },
  editing: null,               // 正在编辑的活动对象（null = 新增）
  detailId: null,              // 右栏正摊开哪条活动的详情（null = 显示当天清单）
};

let toastTimer = null;
function toast(msg, ms = 2200) {
  const el = $('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

// ---------------------------------------------------------------- 取数

function visibleRange() {
  const first = new Date(state.cursor.getFullYear(), state.cursor.getMonth(), 1, 12);
  const last = new Date(state.cursor.getFullYear(), state.cursor.getMonth() + 1, 0, 12);
  // 月网格会补齐前后整周，多取几天让格子里也有数据
  const from = new Date(first); from.setDate(from.getDate() - 7);
  const to = new Date(last); to.setDate(to.getDate() + 7);
  // 网格式固定 42 格（周一开头），最后一行的尾巴最长能伸到下个月 14 天（28 天的
  // 二月并且 1 号正好是周一）。农历那行是按这两个日期单独取的 —— 用上面那个
  // ±7 天的活动区间会漏掉末尾几格，格子里只剩一个孤零零的几号。
  const gridStart = new Date(first);
  gridStart.setDate(gridStart.getDate() - ((gridStart.getDay() + 6) % 7));
  const gridEnd = new Date(gridStart);
  gridEnd.setDate(gridStart.getDate() + 41);
  return {
    from: fmtDay(from), to: fmtDay(to),
    calFrom: fmtDay(gridStart), calTo: fmtDay(gridEnd),
    first, last,
  };
}

async function load() {
  const { from, to, calFrom, calTo } = visibleRange();
  const q = new URLSearchParams({ from, to });
  if (state.filters.status !== 'all') q.set('status', state.filters.status);
  if (state.filters.platform !== 'all') q.set('platform', state.filters.platform);
  if (state.filters.keyword) q.set('keyword', state.filters.keyword);
  try {
    // 活动与日历附注（农历/节气/节日/休班）一起取：附注是后端算好的，取不到就
    // 只是格子里少一行小字，不该连累整页报「加载失败」。
    const [body, cal] = await Promise.all([
      api('/api/activities?' + q.toString()),
      api(`/api/calendar?from=${calFrom}&to=${calTo}`).catch(() => ({ days: {} })),
    ]);
    state.rows = body.data || [];
    state.today = body.today || state.today;
    state.days = cal.days || {};
    refreshPlatformOptions();
    render();
  } catch (err) {
    $('grid').innerHTML = `<div class="empty">加载失败：${err.message}</div>`;
    $('agenda').innerHTML = '';
  }
}

function refreshPlatformOptions() {
  const sel = $('fPlatform');
  const cur = sel.value;
  const platforms = [...new Set(state.rows.map((r) => r.platform).filter(Boolean))].sort();
  sel.innerHTML = '<option value="all">全部平台</option>' +
    platforms.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
  if (platforms.includes(cur)) sel.value = cur;
}

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

// ---------------------------------------------------------------- 渲染

function dayOf(row) {
  return row.grab_at ? row.grab_at.slice(0, 10) : (row.start_at || row.end_at || '').slice(0, 10);
}

// 详情里的时间：`8月3日 10:00`（不补零，跟日历标题、机器人回复一致）、
// 有效期带上年份 `2026年12月31日 23:59`（券的有效期可能跨年，光写月日会误读）。
// **名字不能叫 `fmtDay`** —— 上面那个 `fmtDay(date)` 接的是 Date 对象，
// 同文件里再声明一个同名函数不会报错，只会把前面那个静默顶掉（踩过，整页白屏）。
function cnDay(day) {
  return `${Number(day.slice(5, 7))}月${Number(day.slice(8, 10))}日`;
}

function cnWhen(iso) {
  return iso ? `${cnDay(iso.slice(0, 10))} ${iso.slice(11, 16)}` : '';
}

function cnUntil(iso) {
  return iso ? `${iso.slice(0, 4)}年${cnDay(iso.slice(0, 10))} ${iso.slice(11, 16)}` : '';
}

// 一条活动落在哪些天的哪些时刻。重复活动（每天/每周）由后端给出 occurrences；
// 一次性活动退化成它自己那一天。前端**不**自己算规则 —— 一旦两边算法分叉，
// 日历和机器人就又会对不上（这正是之前那个 bug 的成因）。
//
// 每一场还带着**它自己那一场**的状态：重复活动没有「整条完成」这回事，
// 「每周二、四、周限 1 次」的券这周二领到了，本周四就该显示已完成。
function occurrencesOf(row) {
  if (Array.isArray(row.occurrences) && row.occurrences.length) {
    return row.occurrences.map((o) => ({
      row,
      day: o.day,
      at: o.at || '',
      status: o.status || row.status || 'pending',
      done: !!o.done,
      quota: o.quota || null,
    }));
  }
  const d = dayOf(row);
  if (!d) return [];
  return [{ row, day: d, at: hhmm(row.grab_at), status: row.status || 'pending', done: false, quota: null }];
}

function occStatus(row, occ) {
  return (occ && occ.status) || row.status || 'pending';
}

/** 「本周 1/2 · 本月 3/8」这类进度。已领几天由后端算好，前端只翻译。 */
function quotaText(q) {
  const bits = [];
  [['week', '本周'], ['month', '本月']].forEach(([k, label]) => {
    const s = q && q[k];
    if (!s || !s.limit) return;
    bits.push(`${label} ${s.done}/${s.limit}`);
  });
  return bits.join(' · ');
}

function occOn(dayStr) {
  return state.rows
    .reduce((all, r) => all.concat(occurrencesOf(r)), [])
    .filter((o) => o.day === dayStr)
    .sort((a, b) => (a.at || '~').localeCompare(b.at || '~'));
}

// 「这一天还在有效期内、但不是这天开抢」的那些。哪些天算，由后端按机器人用的
// 同一套判据算好（row.claimable_days）—— 前端不自己看 start_at/end_at 推。
function claimableOn(dayStr) {
  return state.rows.filter((r) => (r.claimable_days || []).indexOf(dayStr) >= 0);
}

function render() {
  const { first, last } = visibleRange();
  $('periodLabel').textContent = `${first.getFullYear()}年${first.getMonth() + 1}月`;
  // 顶栏不放日期了（用户 2026-10-07：「确定没有任何关联，就直接把那个10月7日周三去掉，
  // 桌面端也不要」）：它固定显示真实今天、不跟选中的日期走，哪个格子是今天由日历上的
  // 橙色高亮表示，旁边那个「回到今天」按钮才是能点的那个。
  $('countLabel').textContent = state.rows.length ? `本月范围 ${state.rows.length} 条` : '';
  // 月视图 = 日历 + 右栏（那天的清单 / 某条的详情）；列表视图整月铺开，这两块都收起来。
  const month = state.view === 'month';
  $('monthView').hidden = !month;
  $('sidePane').hidden = !month;
  $('calNav').hidden = !month;
  $('agendaView').hidden = month;
  if (month) {
    renderMonth(first, last);
    renderSidePane();
  } else {
    renderAgenda(first, last);
  }
}

function renderMonth(first, last) {
  const grid = $('grid');
  const start = new Date(first);
  const offset = (start.getDay() + 6) % 7; // 周一为一周第一天
  start.setDate(start.getDate() - offset);

  const cells = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const key = fmtDay(d);
    const inMonth = d.getMonth() === first.getMonth();
    const occs = occOn(key);
    const cls = ['cell'];
    if (!inMonth) cls.push('out');
    if (key === state.today) cls.push('today');
    if (key === state.selected) cls.push('sel');

    // 格子里现在三处信息：几号（大，左上）、农历/节气/节日（小，左下，只显示一条）、
    // 条数（小，右下）；法定节假日的「休/班」小标在右上角。时刻挪走了（用户 2026-10-07：
    // 「把日历上的时间去掉，只保留日期右上角的活动数量小数字」），条数也从右上角挪到
    // 右下角（同一天又说「挪到右下角吧，现在看着有点眼花。把日历上的日期数字大一点，
    // 日历上的数量数字小一点」）—— 两个数字挨着才显得挤。农历/节气/节日/休班是
    // 2026-10-07 又要的（「给日历视图加入农历、二十四节气、中国节日、法定节假日」），
    // 具体显示哪一条由后端 `app/lunar.py` 按 节气 > 节日 > 农历日 定好。
    const info = state.days[key] || {};
    const label = occs.length ? `${occs.length} 条活动` : '没有活动';
    const tip = [dayLabel(d), dayNote(key), label].filter(Boolean).join('　');
    const rest = info.holiday
      ? `<span class="cell-rest ${info.holiday === '休' ? 'off' : 'work'}">${info.holiday}</span>`
      : '';
    cells.push(`<div class="${cls.join(' ')}" data-day="${key}" title="${tip}">
      <div class="cell-top"><span class="cell-day">${d.getDate()}</span>${rest}</div>
      <div class="cell-foot"><span class="cell-note k-${info.kind || 'none'}">${esc(info.text || '')}</span>${
  occs.length ? `<span class="cell-cnt">${occs.length}</span>` : ''}</div>
    </div>`);
  }
  grid.innerHTML = cells.join('');
  grid.querySelectorAll('.cell').forEach((el) => {
    el.addEventListener('click', () => {
      state.selected = el.dataset.day;
      state.detailId = null;   // 换天就回到清单：详情属于上一天那条，留着会串
      render();
    });
  });
}

function renderAgenda(first, last) {
  const lo = fmtDay(first);
  const hi = fmtDay(last);
  const all = state.rows
    .reduce((acc, r) => acc.concat(occurrencesOf(r)), [])
    .filter((o) => o.day >= lo && o.day <= hi);
  if (!all.length) {
    $('agenda').innerHTML = '<div class="empty">这个月还没有活动。把活动发给机器人，或者点「＋ 新增」。</div>';
    return;
  }
  const byDay = {};
  all.forEach((o) => { (byDay[o.day] ||= []).push(o); });

  $('agenda').innerHTML = Object.keys(byDay).sort().map((day) => {
    const occs = byDay[day].sort((a, b) => (a.at || '~').localeCompare(b.at || '~'));
    const isToday = day === state.today;
    const note = dayNote(day);
    return `<div class="agenda-day ${isToday ? 'today' : ''}">
      <h3>${dayLabel(parseDay(day))}${isToday ? ' · 今天' : ''}　<span style="font-weight:400">${relDay(day, state.today)}${note ? ` · ${esc(note)}` : ''}</span></h3>
      <div class="cards">${occs.map((o) => card(o.row, o)).join('')}</div>
    </div>`;
  }).join('');
  bindCards($('agenda'));
}

function card(r, occ, dayStr) {
  const st = occStatus(r, occ);
  const STATUS_CN = { pending: '待抢', done: '已完成', expired: '已过期', missed: '未抢到' };
  const meta = [r.platform, r.category].filter(Boolean).map(esc).join(' · ');
  const at = occ && occ.at ? occ.at : hhmm(r.grab_at);
  // 「还能领的」那节里摆的是**别的日子**开抢的活动：光写「10:00 开抢」会被读成
  // 今天 10:00（用户已经为这类歧义报过一次）。跨天就把日子写出来。
  const grabDay = r.grab_at ? r.grab_at.slice(0, 10) : '';
  let atLabel = at;
  if (at && grabDay && dayStr && grabDay !== dayStr) {
    const gd = parseDay(grabDay);
    atLabel = `${gd.getMonth() + 1}月${gd.getDate()}日 ${at}`;
  }
  const thisOnce = at ? `🎯 ${atLabel} 开抢` : (r.start_at ? '全天' : '时间未定');
  // 重复活动先说规则（它为什么天天在这），再说这一场是几点
  const when = r.repeat ? `${esc(r.repeat)}${occ && occ.at ? `（本场 ${occ.at}）` : ''}` : thisOnce;
  // 配额进度：这周/这月已经领几次、还剩几次。领满了就把原因说出来，
  // 不然用户看到一张「已完成」的每天券会以为是误标。
  const q = quotaText((occ && occ.quota) || r.quota);
  const qLine = q ? `<div class="quota">📊 ${q}${occ && !occ.done && st === 'done' ? '　本期已领满' : ''}</div>` : '';
  const end = r.end_at ? `　有效期至 ${r.end_at.slice(5, 16).replace('T', ' ')}` : '';
  // 重复活动的按钮**按天**：撤销的是「这一场」，不是整条。配额领满导致的那张
  // 「已完成」没有可撤销的东西，就不摆按钮。
  let actions;
  if (r.repeat) {
    if (occ && occ.done) actions = '<button class="mini" data-act="pending">撤销本次</button>';
    else if (st === 'done') actions = '';
    else actions = '<button class="mini" data-act="done">标记完成</button>';
  } else {
    actions = st !== 'done'
      ? '<button class="mini" data-act="done">标记完成</button>'
      : '<button class="mini" data-act="pending">恢复待抢</button>';
  }
  return `<div class="card ${st}" data-id="${r.id}" data-day="${occ ? esc(occ.day) : ''}">
    <div class="when">${when}${end}</div>
    <h4>${esc(r.title)}</h4>
    ${meta ? `<div class="meta">${meta}</div>` : ''}
    ${qLine}
    ${r.note ? `<div class="note">📝 ${esc(r.note)}</div>` : ''}
    ${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.url)}</a>` : ''}
    <div class="card-actions">
      <span class="badge ${st}">${STATUS_CN[st] || st}</span>
      ${actions}
      <button class="mini" data-act="edit">编辑</button>
    </div>
  </div>`;
}

function bindCards(root) {
  root.querySelectorAll('.card').forEach((el) => {
    const id = Number(el.dataset.id);
    const day = el.dataset.day || '';
    const row = state.rows.find((r) => r.id === id);
    el.querySelector('[data-act="edit"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      openForm(row);
    });
    el.querySelector('[data-act="done"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      setDone(row, day, true);
    });
    el.querySelector('[data-act="pending"]')?.addEventListener('click', (e) => {
      e.stopPropagation();
      setDone(row, day, false);
    });
    el.addEventListener('click', () => openForm(row));
  });
}

/** 农历/节气/节日/休班拼成一行，右栏标题、列表视图和格子提示共用。
 *  「清明」这个节气与「清明节」这个节日是同一天，只写一个。 */
function dayNote(day) {
  const info = state.days[day] || {};
  const bits = [info.lunar_full, info.term];
  if (info.festival && info.festival !== `${info.term}节`) bits.push(info.festival);
  if (info.holiday === '休') bits.push('放假');
  else if (info.holiday === '班') bits.push('上班');
  return bits.filter(Boolean).join(' · ');
}

/** 「今天/明天」这类相对说法 + 日期，右栏标题用。 */
function dayTitle(day) {
  const d = parseDay(day);
  const note = dayNote(day);
  return `${day === state.today ? '今天 · ' : ''}${dayLabel(d)}${note ? ` · ${note}` : ''}`;
}

/** 右栏里的一行活动标题（点开看详情）。重复活动传 occ，徽章按**那一场**算。 */
function actRow(r, occ) {
  const st = occStatus(r, occ);
  const STATUS_CN = { pending: '待抢', done: '已完成', expired: '已过期', missed: '未抢到' };
  const q = quotaText((occ && occ.quota) || r.quota);
  return `<button class="act ${st}" type="button" data-id="${r.id}">
    <span class="t">${esc(r.title)}${q ? `<span class="q">📊 ${q}</span>` : ''}</span>
    <span class="badge ${st}">${STATUS_CN[st] || st}</span><span class="chev">›</span>
  </button>`;
}

/** 右栏：选中那天的活动标题，按时刻分组（同一个时刻只写一遍）。 */
function renderSidePane() {
  if (state.detailId != null) { renderDetail(); return; }
  const day = state.selected || state.today;
  const occs = occOn(day);
  const claim = claimableOn(day);

  const groups = {};
  occs.forEach((o) => { (groups[o.at || ''] ||= []).push(o); });
  const body = Object.keys(groups)
    .sort((a, b) => (a || '~').localeCompare(b || '~'))
    .map((t) => (t ? `<div class="tgroup">${t}<span class="line"></span></div>` : '')
      + groups[t].map((o) => actRow(o.row, o)).join(''))
    .join('');

  // 措辞要和机器人对齐：机器人说「📭 没有开抢安排」+「📌 不过这一天还能领的」，
  // 日历就不能把「今天没得抢但券还有效」说成「这天没有活动」。
  const main = body || '<div class="empty">这天没有开抢安排</div>';
  const extra = claim.length
    ? '<div class="sect">📌 这天还能领的（不是这天开抢）</div>'
      + claim.map((r) => actRow(r)).join('')
    : '';

  $('sideHead').innerHTML = `<h3>${esc(dayTitle(day))}</h3>`
    + `<span class="n">${occs.length} 条</span>`
    + '<button class="btn primary sm" id="sideAdd" type="button">＋ 新增</button>';
  $('sideBody').innerHTML = main + extra
    + '<div class="hint">点标题看详情（平台、有效期、完整备注、链接）。</div>';
  $('sideAdd').addEventListener('click', () => openForm(null, day));
  $('sideBody').querySelectorAll('.act').forEach((el) => {
    el.addEventListener('click', () => { state.detailId = Number(el.dataset.id); renderDetail(); });
  });
}

/** 右栏就地变成某条活动的详情 —— 日历还看得见，「‹ 返回清单」一步回来。 */
function renderDetail() {
  const row = state.rows.find((r) => r.id === state.detailId);
  if (!row) { state.detailId = null; renderSidePane(); return; }
  const STATUS_CN = { pending: '待抢', done: '已完成', expired: '已过期', missed: '未抢到' };
  const day = state.selected || state.today;
  const occ = occOn(day).find((o) => o.row.id === row.id);
  const st = occStatus(row, occ);
  const at = (occ && occ.at) || hhmm(row.grab_at);
  const grabDay = dayOf(row);
  // 从「还能领的」那一节点进来的，开抢日不是这天：光写「10:00」会被读成今天
  const showDay = at && grabDay && grabDay !== day ? grabDay : day;
  const whenLabel = at ? `${cnDay(showDay)} ${at}` : '时间未定';
  const badges = `<span class="badge ${st}">${STATUS_CN[st] || st}</span>`
    + [row.platform, row.category, row.repeat].filter(Boolean)
      .map((x) => `<span class="badge plain">${esc(x)}</span>`).join('');

  // 「本周/本月已领几次、还剩几次」—— 用户要的就是这一行（2026-10-08 提的）。
  // 周期的起止由后端给（自然周从周一起、自然月按日历月），前端不自己推。
  const quotaBlock = [['week', '本周'], ['month', '本月']].map(([k, label]) => {
    const s = row.quota && row.quota[k];
    if (!s || !s.limit) return '';
    const left = s.left > 0 ? `还剩 <b>${s.left}</b> 次` : '<b>已领满</b>';
    return `<dt>${label}</dt><dd>${cnDay(s.from)}–${cnDay(s.to)}　`
      + `已领 ${s.done}/${s.limit} 次，${left}</dd>`;
  }).join('');

  // 重复活动的按钮按天：撤销的是「这一场」。配额领满导致的「已完成」没东西可撤销。
  let statusBtn;
  if (row.repeat) {
    if (occ && occ.done) statusBtn = `<button class="btn" id="dDone" data-done="0" type="button">↩️ 撤销本次完成</button>`;
    else if (st === 'done') statusBtn = '';
    else statusBtn = `<button class="btn" id="dDone" data-done="1" type="button">✅ 标记本次完成</button>`;
  } else {
    statusBtn = `<button class="btn" id="dStatus" type="button">${st === 'done' ? '恢复待抢' : '标记完成'}</button>`;
  }

  $('sideHead').innerHTML = '<button class="back" id="sideBack" type="button">‹ 返回清单</button>';
  $('sideBody').innerHTML = `<div class="detail">
    <h3>${esc(row.title)}</h3>
    <div class="badges">${badges}</div>
    <dl class="kv">
      <dt>开抢</dt><dd><span class="big">${whenLabel}</span></dd>
      ${quotaBlock}
      ${row.start_at ? `<dt>开始</dt><dd>${esc(cnWhen(row.start_at))}</dd>` : ''}
      ${row.end_at ? `<dt>有效期至</dt><dd>${esc(cnUntil(row.end_at))}</dd>` : ''}
      ${row.source ? `<dt>来源</dt><dd>${row.source === 'image' ? '微信截图识别' : esc(row.source)}</dd>` : ''}
    </dl>
    ${row.note ? `<div class="note">📝 ${esc(row.note)}</div>` : ''}
    ${row.url ? `<div class="d-actions"><a href="${esc(row.url)}" target="_blank" rel="noopener noreferrer">🔗 ${esc(row.url)}</a></div>` : ''}
    <div class="d-actions">
      <button class="btn" id="dEdit" type="button">✏️ 编辑</button>
      ${statusBtn}
      <button class="btn danger" id="dDelete" type="button">🗑 删除</button>
    </div>
  </div>`;
  $('sideBack').addEventListener('click', backToList);
  $('dEdit').addEventListener('click', () => openForm(row));
  $('dStatus')?.addEventListener('click', () => setStatus(row.id, st === 'done' ? 'pending' : 'done'));
  $('dDone')?.addEventListener('click', (e) => {
    // 没点进某一天时，用这条活动离今天最近的那一场，免得「标记完成」变成空操作
    const target = (occ && occ.day) || (row.occurrences && row.occurrences[0] && row.occurrences[0].day) || day;
    setDone(row, target, e.currentTarget.dataset.done === '1');
  });
  $('dDelete').addEventListener('click', () => deleteRow(row));
}

function backToList() {
  state.detailId = null;
  renderSidePane();
}

async function deleteRow(row) {
  if (!confirm(`确定删除「${row.title}」？`)) return;
  try {
    await api(`/api/activity/${row.id}`, { method: 'DELETE' });
    toast('已删除');
    state.detailId = null;
    await load();
  } catch (err) { toast('删除失败：' + err.message); }
}

async function setStatus(id, status) {
  try {
    await api(`/api/activity/${id}/status`, { method: 'POST', body: JSON.stringify({ status }) });
    toast(status === 'done' ? '已标记完成' : '已恢复待抢');
    await load();
  } catch (err) { toast('操作失败：' + err.message); }
}

/** 标记「某一天领到了」/ 撤销。
 *
 * 一次性活动没有「哪一天」这回事，还是走行级 status（行为和以前一模一样）；
 * 重复活动必须按天记 —— 一行有几十场，一格 status 装不下，今天点了完成
 * 明天整条都成完成，用户就再也看不到「这周还剩几次」。
 */
async function setDone(row, day, done) {
  if (!row) return;
  if (!row.repeat || !day) { await setStatus(row.id, done ? 'done' : 'pending'); return; }
  try {
    await api(`/api/activity/${row.id}/${done ? 'done' : 'undo'}`, {
      method: 'POST',
      body: JSON.stringify({ day }),
    });
    toast(done ? '已记下这天领到了' : '已撤销这天的记录');
    await load();
  } catch (err) { toast('操作失败：' + err.message); }
}

// ---------------------------------------------------------------- 表单

function toInput(minuteStr) {
  return minuteStr ? minuteStr.slice(0, 16) : '';
}

function openForm(row, presetDay) {
  state.editing = row || null;
  const f = $('form');
  const el = f.elements; // 用 elements 取值：避免与 HTMLFormElement 自身的同名属性打架
  f.reset();
  $('formTitle').textContent = row ? '编辑活动' : '新增活动';
  $('btnDelete').hidden = !row;
  if (row) {
    el.title.value = row.title || '';
    el.platform.value = row.platform || '';
    el.category.value = row.category || '';
    el.grab_at.value = toInput(row.grab_at);
    el.start_at.value = toInput(row.start_at);
    el.end_at.value = toInput(row.end_at);
    // 回填用后端给的 repeat_input（**纯排期**那句）。直接拿 row.repeat 会把
    // 「· 周限1次」一起填回输入框，再存一次就会被当成规则文本重新解析。
    el.repeat.value = row.repeat_input || (row.repeat ? row.repeat.replace(/^🔁\s*/, '') : '');
    el.quota_week.value = row.quota_week == null ? '' : String(row.quota_week);
    el.quota_month.value = row.quota_month == null ? '' : String(row.quota_month);
    el.url.value = row.url || '';
    el.note.value = row.note || '';
    el.status.value = row.status || 'pending';
  } else if (presetDay) {
    el.grab_at.value = presetDay + 'T09:00';
  }
  $('modal').hidden = false;
  el.title.focus();
}

function closeForm() {
  $('modal').hidden = true;
  state.editing = null;
}

async function submitForm(ev) {
  ev.preventDefault();
  const el = $('form').elements;
  const payload = {
    title: el.title.value.trim(),
    platform: el.platform.value.trim() || null,
    category: el.category.value.trim() || null,
    grab_at: el.grab_at.value || null,
    start_at: el.start_at.value || null,
    end_at: el.end_at.value || null,
    repeat: el.repeat.value.trim(),
    url: el.url.value.trim() || null,
    note: el.note.value.trim() || null,
    status: el.status.value,
  };
  // 配额：留空＝不限。但**只有**「箱子里有数字」或「原来就有配额、现在被清空」
  // 才把这个 key 发出去 —— 否则新增时一个空箱子会把后端从备注里认出来的
  // 「单用户单周限领1张」一并抹掉。
  const was = state.editing || {};
  const qw = el.quota_week.value.trim();
  const qm = el.quota_month.value.trim();
  if (qw || was.quota_week) payload.quota_week = qw;
  if (qm || was.quota_month) payload.quota_month = qm;
  try {
    if (state.editing) {
      await api(`/api/activity/${state.editing.id}`, { method: 'POST', body: JSON.stringify(payload) });
      toast('已保存');
    } else {
      await api('/api/activity', { method: 'POST', body: JSON.stringify(payload) });
      toast('已新增');
    }
    closeForm();
    await load();
  } catch (err) { toast('保存失败：' + err.message); }
}

async function deleteEditing() {
  if (!state.editing) return;
  if (!confirm(`确定删除「${state.editing.title}」？`)) return;
  try {
    await api(`/api/activity/${state.editing.id}`, { method: 'DELETE' });
    toast('已删除');
    closeForm();
    await load();
  } catch (err) { toast('删除失败：' + err.message); }
}

// ---------------------------------------------------------------- 事件

function step(delta) {
  state.cursor = new Date(state.cursor.getFullYear(), state.cursor.getMonth() + delta, 1, 12);
  state.selected = null;
  syncUrl();
  load();
}

function init() {
  // 地址栏里带的视图/月份优先（刷新、分享链接时保持原位）
  const params = new URLSearchParams(location.search);
  const m = params.get('month');
  if (m && /^\d{4}-\d{2}$/.test(m)) {
    const d = parseDay(m + '-01');
    state.cursor = new Date(d.getFullYear(), d.getMonth(), 1, 12);
  } else {
    state.cursor = new Date(state.cursor.getFullYear(), state.cursor.getMonth(), 1, 12);
  }
  if (params.get('view') === 'agenda') state.view = 'agenda';
  state.selected = state.today;

  document.querySelectorAll('.seg-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === state.view);
    btn.addEventListener('click', () => {
      document.querySelectorAll('.seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
      state.view = btn.dataset.view;
      syncUrl();
      render();
    });
  });

  // 白天/夜间：模式本身是 index.html 里那段内联脚本定的，这里只管按钮和「跟随系统」
  $('btnTheme').addEventListener('click', toggleTheme);
  paintTheme();        // 初始符号得和已经定好的模式对上，不能写死在 HTML 里
  watchSystemTheme();

  $('btnPrev').addEventListener('click', () => step(-1));
  $('btnNext').addEventListener('click', () => step(1));
  $('btnToday').addEventListener('click', () => {
    state.cursor = new Date(new Date().getFullYear(), new Date().getMonth(), 1, 12);
    state.selected = state.today;
    syncUrl();
    load();
  });

  let qTimer = null;
  $('q').addEventListener('input', (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => { state.filters.keyword = e.target.value.trim(); load(); }, 250);
  });
  $('fStatus').addEventListener('change', (e) => { state.filters.status = e.target.value; load(); });
  $('fPlatform').addEventListener('change', (e) => { state.filters.platform = e.target.value; load(); });

  $('btnAdd').addEventListener('click', () => openForm(null, state.selected || state.today));
  $('btnCancel').addEventListener('click', closeForm);
  $('btnDelete').addEventListener('click', deleteEditing);
  $('form').addEventListener('submit', submitForm);
  $('modal').addEventListener('click', (e) => { if (e.target === $('modal')) closeForm(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeForm(); });

  $('btnRefreshQr').addEventListener('click', async () => {
    // 光把 qr_token 清掉只是重下磁盘上同一张旧图 —— 用户点了看不出任何变化，
    // 所以这里真的去让机器人重新申请一张。图片本身由 5 秒轮询自动换新。
    const btn = $('btnRefreshQr');
    btn.disabled = true;
    btn.textContent = '正在换…';
    try {
      const body = await api('/api/qr/refresh', { method: 'POST' });
      toast(body.note || '已请求换一张二维码，稍等几秒');
    } catch (err) {
      toast('刷新失败：' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '刷新二维码';
      setTimeout(pollBotStatus, 800); // 给机器人一点时间把新图写到磁盘
    }
  });

  $('btnSettings').addEventListener('click', openSettings);
  $('btnLogout').addEventListener('click', logout);
  $('btnWxLogout').addEventListener('click', logoutWechat);
  $('btnSetup').addEventListener('click', openSettings);
  $('btnSettingsCancel').addEventListener('click', () => { $('settingsModal').hidden = true; });
  $('settingsModal').addEventListener('click', (e) => {
    if (e.target === $('settingsModal')) $('settingsModal').hidden = true;
  });
  $('btnTest').addEventListener('click', testModel);
  $('btnSettingsSave').addEventListener('click', saveSettings);
  // 设置弹窗顶上的三个页签（功能 / 模型 / 安全 —— 用户 2026-10-10 要的三个同级小页）。
  $('settingsTabs').querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => showSheetPane(tab.dataset.pane));
  });

  // 使用帮助：内容问后端要（＝微信里发「使用帮助」那段字），只取一次
  $('btnHelp').addEventListener('click', openHelp);
  $('btnHelpClose').addEventListener('click', () => { $('helpModal').hidden = true; });
  $('helpModal').addEventListener('click', (e) => {
    if (e.target === $('helpModal')) $('helpModal').hidden = true;
  });

  // ?id=25 直接把某条活动的详情摊在右栏 —— 和 ?settings=1 一样，方便把链接发给别人。
  // 得赶在第一次渲染之前设好：load() 取完数据就会照着它渲染。
  const deepId = Number(new URL(location.href).searchParams.get('id'));
  if (deepId) state.detailId = deepId;
  // ?day=2026-10-09 直接看那一天（点日历换天时 syncUrl() 也会把它写回地址栏）
  const deepDay = new URL(location.href).searchParams.get('day');
  if (/^\d{4}-\d{2}-\d{2}$/.test(deepDay || '')) {
    state.selected = deepDay;
    const d = parseDay(deepDay);
    state.cursor = new Date(d.getFullYear(), d.getMonth(), 1, 12);
  }

  load();
  pollBotStatus();
  pollSettings();
  setInterval(pollBotStatus, 5000);
  // 模型红条也得自己刷：微信那边真调用失败时，页面正开着却一直显示旧状态，
  // 用户就得刷新页面才知道出事了。15 秒一次，一次只读一个很小的 JSON。
  setInterval(pollSettings, 15000);

  // ?settings=1 直接打开设置弹层 —— 方便把链接发给别人，也方便截图验证
  if (new URL(location.href).searchParams.get('settings')) openSettings();
  // ?help=1 同理，直接摊开使用帮助
  if (new URL(location.href).searchParams.get('help')) openHelp();
}

// --- 使用帮助 -------------------------------------------------------------

async function openHelp() {
  $('helpModal').hidden = false;
  const box = $('helpText');
  // 帮助是静态文字，取到一次就够，别每次点都打一次接口
  if (box.dataset.loaded) return;
  try {
    const body = await api('/api/help');
    box.textContent = (body && body.data && body.data.text) || '（没取到帮助文字）';
    box.dataset.loaded = '1';
  } catch (err) {
    box.textContent = '取不到帮助：' + err.message;
  }
}

// --- AI 模型设置 ----------------------------------------------------------
// 这个面板是「机器人能不能干活」的开关。没有它，用户就得 SSH 上 NAS 改文件，
// 改完还得重启容器 —— 对他来说是走不通的路。

// 页面上有两处 `last_error`：模型设置里的是对象（{kind,message,at}），微信长轮询里
// 的是字符串。只认得对象太脆了 —— 一旦拿到字符串或少了 message 的旧载荷，红条就会
// 退化成一句通用话，偏偏它旁边可能正显示「✅ 通了」，看着就像这软件在自相矛盾。
function lastErrorText(err, fallback) {
  if (!err) return '';
  if (typeof err === 'string') return err;
  return err.message || fallback || '';
}

// 「这条记录里到底有没有东西」。空对象 `{}` / 空串都算没有。
// 曾经这里只写 `data.last_error`，而后端那时回的是 `{}` —— 空对象是真值，
// 于是模型好好的、红条却一直亮着「模型调用失败了」。判据必须落在**内容**上。
function hasError(err) {
  if (!err) return false;
  if (typeof err === 'string') return !!err.trim();
  return !!(String(err.message || '').trim() || String(err.kind || '').trim());
}

async function pollSettings() {
  let body;
  try {
    body = await api('/api/settings');
  } catch (err) {
    return; // 拿不到设置不影响看日历
  }
  const data = body.data || {};
  // 两种情况都得在页面上说清楚，直到修好为止：
  //   ① 一个 key 都没有 —— 机器人读不懂消息；
  //   ② 有 key 但用不了（401/404/400）—— 机器人同样读不懂，而且这时候比①更坑：
  //      黄条消失了，用户以为一切正常，只会在微信里收到一句报错。
  // 宁可说一句通用话，也不能在 `configured` 为真时说「还没配」—— 那是一句假话。
  const problem = data.configured && hasError(data.last_error)
    ? (lastErrorText(data.last_error, '') || '模型调用失败了，到设置里检查一下。')
    : '';
  $('setupPanel').hidden = !!data.configured && !problem;
  $('setupPanel').className = problem ? 'setup-panel danger' : 'setup-panel';
  if (problem) {
    $('setupTitle').textContent = '⚠️ AI 模型用不了';
    $('setupBody').textContent = problem;
  } else {
    $('setupTitle').textContent = '⚠️ 还没配 AI 模型';
    $('setupBody').textContent = '日历能用，但机器人还读不懂你发的消息。填一个 API key 就能用了'
      + ' —— 存了立刻生效，不用重启。';
  }
  // 这里以前还有一段：把微信那颗胶囊的 title 改成「模型：xxx」，因为它在顶栏、
  // 别处一眼看不到模型。2026-10-11 那颗胶囊挪进了设置里「微信」旁边（用户要求），
  // 再拿模型名去盖掉它的连接说明就成了驴唇不对马嘴 —— 模型在「模型」页摆着，
  // 胶囊只讲微信连没连上。
}

function fillSettingsForm(data) {
  $('sBaseUrl').value = data.base_url || '';
  $('sModel').value = data.model || '';
  $('sTemp').value = (data.temperature === undefined || data.temperature === null)
    ? '' : data.temperature;
  $('sTokenRequired').checked = !!data.token_required;
  $('sChat').checked = !!data.chat_enabled;
  $('sSayings').checked = !!data.sayings_enabled;
  // 「一言来源」不是秘密，原样回填；placeholder 只在没填的时候提示默认值 ——
  // 默认那四类由服务端给（`sayings_source_default`），页面不自己抄一份分类表。
  $('sSayingsSource').value = data.sayings_source || '';
  $('sSayingsSource').placeholder = `留空＝${data.sayings_source_default || 'd,i,k,h'}`;
  // 分类字母表由服务端给（权威只有 `app/sayings.py::CATEGORIES` 一处），
  // 页面不自己抄一份。用户 2026-10-10：「把一言来源的字母分类注释到「一言来源」后面」。
  $('sSayingsCats').textContent = data.sayings_categories || '';
  $('sRemind').checked = !!data.remind_enabled;
  $('sRemindMinutes').value = (data.remind_minutes === undefined || data.remind_minutes === null)
    ? '' : data.remind_minutes;
  // 口令和密码都**不回显真值**，只把掩码放进 placeholder 告诉你「已经设过了」。
  // 输入框留空 = 这个框我没动（后端也这么认），所以绝不会把掩码存成新口令。
  $('sWebToken').value = '';
  $('sWebToken').placeholder = data.web_token_masked
    ? `已保存：${data.web_token_masked}（要换就填新的）`
    : '留空则不改动';
  $('sDomain').checked = !!data.domain_enabled;
  $('sDomainUrl').value = data.domain_url || '';
  $('sLogin').checked = !!data.login_enabled;
  // 用户名没有了 —— 整页只剩这一把秘钥。存过就只显示掩码，绝不把明文回填进输入框。
  $('sLoginKey').value = '';
  $('sLoginKey').placeholder = data.login_key_set
    ? `已保存：${data.login_key_masked}（要换就填新的）`
    : (data.login_key_hint || '留空则不改动');
  // 「退出」只在登录真开着的时候露出来 —— 没开的时候点了也没意义。
  // 它现在住在设置弹窗里（跟秘钥放一起），这个函数正好就是「打开弹窗时」跑的。
  const out = $('btnLogout');
  if (out) out.hidden = !data.login_ready;
  $('sApiKey').value = '';
  $('sApiKey').placeholder = data.api_key_masked
    ? `已保存：${data.api_key_masked}（要换就填新的）`
    : 'sk-…';
  // 失败原因单独一行：塞进 `#sKeyState` 会把「留空则不改动已存的 key」粘成
  // 一句话的后半截，读起来像是模型在说这句。
  const box = $('sKeyError');
  if (data.configured && hasError(data.last_error)) {
    const err = lastErrorText(data.last_error, '上次调用模型失败了，到「设置」里检查一下。');
    const at = (data.last_error && data.last_error.at) ? `（${data.last_error.at}）` : '';
    box.textContent = `⚠️ 上次调用模型失败${at}：${err}`;
    box.hidden = false;
  } else {
    box.textContent = '';
    box.hidden = true;
  }
  $('sKeyState').textContent = data.configured
    ? '模型已就绪。'
    : '还没配 key，机器人现在读不懂消息。';
  $('sKeyState').className = data.configured ? 'ok' : 'warn';
  $('testResult').textContent = '';
}

// 设置弹窗里的三个同级小页（用户 2026-10-10：「不能做成三个在『设置』内的同级别小
// 独立页吗」）：一次只露一个，页签跟着亮。翻页只是切 `hidden` —— 藏起来的控件照样
// 带着自己的值，保存时 `settingsPayload()` 读的是整张弹窗，不分页。
function showSheetPane(which) {
  $('settingsTabs').querySelectorAll('.tab').forEach((tab) => {
    const on = tab.dataset.pane === which;
    tab.classList.toggle('on', on);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('settingsModal').querySelectorAll('.sheet-pane').forEach((pane) => {
    pane.hidden = pane.id !== which;
  });
}

async function openSettings() {
  $('settingsModal').hidden = false;
  let configured = true;
  try {
    const body = await api('/api/settings');
    const data = body.data || {};
    configured = !!data.configured;
    fillSettingsForm(data);
  } catch (err) {
    toast('读设置失败：' + err.message);
  }
  // 打开时落在哪一页：模型还没配好就直接翻到「模型」页并聚焦 key（那是一定要动的东西，
  // 从顶栏那个「还没配 AI 模型」的提醒点进来时尤其顺）；配好了就停在「功能」页。
  showSheetPane(configured ? 'paneFunc' : 'paneModel');
  if (!configured) $('sApiKey').focus();
}

function settingsPayload() {
  const payload = {
    base_url: $('sBaseUrl').value.trim(),
    model: $('sModel').value.trim(),
    // 文字模型与图片模型在页面上合并成一个框（用户 2026-10-10：「把模型框的『文字模型』
    // 和『图片模型』合并成一个」），所以两个键发同一个值。库里仍然分开存 ——
    // 图片解析读的是 `vision_model`，只是不再单独露一个框。
    vision_model: $('sModel').value.trim(),
    temperature: $('sTemp').value.trim(),
    web_token_required: $('sTokenRequired').checked,
    chat_enabled: $('sChat').checked,
    sayings_enabled: $('sSayings').checked,
    // 空串照发：服务端把空串当「回到默认分类」，和「留空 = 不改动」那几个
    // 秘密字段不一样（这里没有秘密，清空就是清空）。
    sayings_source: $('sSayingsSource').value.trim(),
    remind_enabled: $('sRemind').checked,
    remind_minutes: $('sRemindMinutes').value.trim(),
    domain_enabled: $('sDomain').checked,
    domain_url: $('sDomainUrl').value.trim(),
    login_enabled: $('sLogin').checked,
  };
  const key = $('sApiKey').value.trim();
  if (key) payload.api_key = key; // 空 = 不动已存的 key
  const webToken = $('sWebToken').value.trim();
  if (webToken) payload.web_token = webToken; // 空 = 不动已存的口令
  const loginKey = $('sLoginKey').value; // 秘钥**不 trim**：首尾空格也算字符
  if (loginKey) payload.login_key = loginKey; // 空 = 不动已存的秘钥
  return payload;
}

async function logout() {
  try {
    await api('/api/logout', { method: 'POST' });
  } catch (_) {
    // 本来就已经不认了也无所谓 —— 重新拉一次页面，服务端自然会发登录表单下来
  }
  location.reload();
}

// 「退出微信」（用户 2026-10-11 问「有没有必要增加退出微信的功能」）。
// 它只丢掉 NAS 本地那份会话（`data/weixin_state.json` 里的 token 和游标），
// **不会**在微信那边撤销授权 —— 所以这句话必须写在确认框里，否则用户会以为
// 点了它微信里那条绑定就没了。它的用处是把「解除连接后干等一小时冷却」变成几秒。
async function logoutWechat() {
  const ok = confirm(
    '退出后机器人收不到微信消息，要重新扫码才能用。\n\n'
    + '这一步只清掉 NAS 上存的登录，微信里那条授权还在 —— 想彻底解绑，'
    + '请到微信里「解除连接」。\n\n确定退出吗？'
  );
  if (!ok) return;
  const btn = $('btnWxLogout');
  btn.disabled = true;
  try {
    const body = await api('/api/wechat/logout', { method: 'POST' });
    toast(body.note || '已退出微信');
  } catch (err) {
    toast('退出失败：' + err.message);
  } finally {
    btn.disabled = false;
    setTimeout(pollBotStatus, 800); // 给机器人一点时间申请新码
  }
}

async function testModel() {
  const btn = $('btnTest');
  btn.disabled = true;
  $('testResult').textContent = '正在试…';
  $('testResult').className = 'dim';
  try {
    const body = await api('/api/settings/test', {
      method: 'POST',
      body: JSON.stringify(settingsPayload()),
    });
    $('testResult').textContent = '✅ 通了，模型回复：' + (body.reply || '（空）');
    $('testResult').className = 'ok';
    // 试通了就立刻把顶部红条刷掉 —— 不然「⚠️ 模型用不了」会和这句「✅ 通了」
    // 并排显示到用户关掉页面为止（pollSettings 只在打开页面和保存时跑）。
    $('sKeyError').hidden = true; // 弹层里那行「上次调用失败」同理，一起收掉
    pollSettings();
  } catch (err) {
    $('testResult').textContent = '❌ ' + err.message;
    $('testResult').className = 'warn';
  } finally {
    btn.disabled = false;
  }
}

async function saveSettings() {
  const btn = $('btnSettingsSave');
  btn.disabled = true;
  try {
    const body = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify(settingsPayload()),
    });
    fillSettingsForm(body.data || {});
    $('settingsModal').hidden = true;
    toast('已保存，立刻生效');
    pollSettings();
  } catch (err) {
    toast('保存失败：' + err.message);
  } finally {
    btn.disabled = false;
  }
}

// --- 机器人登录状态 -------------------------------------------------------
// 机器人跑在 NAS 上没有屏幕，二维码只能由这个页面递给用户扫。

let qrShown = null; // 记住已经显示过的二维码版本，避免每 5 秒重下一次图

async function pollBotStatus() {
  let body;
  try {
    body = await api('/api/status');
  } catch (err) {
    return; // 状态查不到不影响看日历，静默跳过
  }
  const data = body.data || {};
  const pill = $('botPill');
  const panel = $('loginPanel');
  const link = $('qrLink');
  const block = $('wxBlock');   // 整块（标签 + 胶囊 + 退出 + 二维码）
  const out = $('btnWxLogout'); // 「退出微信」：只有真有会话时才给
  link.hidden = true; // 只在未登录且有链接时才显示

  if (data.bot === 'off') {
    block.hidden = true;
    pill.hidden = true;
    panel.hidden = true;
    out.hidden = true;
    return;
  }

  block.hidden = false;
  pill.hidden = false;
  out.hidden = !data.logged_in;
  if (data.logged_in) {
    // 有 token 不等于收得到消息：token 失效后有 1 小时冷却，这期间消息全在
    // 队列里躺着，页面却写着「已连接」—— 那是最容易让人白等的一种错。
    if (data.polling === false) {
      pill.className = 'pill warn';
      pill.textContent = '⚠️ 微信连接异常';
      const ago = data.seconds_since_poll;
      pill.title = (data.last_error ? `原因：${data.last_error}。` : '') +
        (ago === null || ago === undefined
          ? '还没成功轮询过，机器人可能刚启动。'
          : `已经 ${ago} 秒没收到服务端回应了，这段时间的消息可能没被处理。`);
    } else {
      pill.className = 'pill on';
      pill.textContent = '微信已连接';
      pill.title = '长轮询正常，消息会被及时收到';
    }
    panel.hidden = true;
    return;
  }

  pill.className = 'pill off';
  pill.textContent = '⚠️ 微信未登录';

  if (data.qr_url) {
    // 扫不动二维码时的兜底：在手机上直接打开这个链接也能登录
    link.innerHTML = '';
    const a = document.createElement('a');
    a.href = data.qr_url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = '在手机上打开这个链接';
    link.append('扫不了？', a, '，一样能登录。');
    link.hidden = false;
  }

  if (!data.qr_ready) {
    panel.hidden = false;
    $('qrImg').removeAttribute('src');
    $('loginHint').textContent = data.qr_url
      ? '二维码图片没生成出来，用上面那个链接登录即可。'
      : '正在向微信申请二维码，请稍候…（没反应就刷新一下页面）';
    return;
  }

  panel.hidden = false;
  $('loginHint').textContent = '';
  if (qrShown === data.qr_token) return; // 还是同一张图，别每 5 秒重下一次
  $('qrImg').src = `/api/qr.png?t=${data.qr_token || Date.now()}`;
  qrShown = data.qr_token;
}

init();
