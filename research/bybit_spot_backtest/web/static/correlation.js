'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const PREFIX = document.body.dataset.prefix || '';
  const api = url => PREFIX + url;
  const base = inst => inst.replace(/-USDT$/, '');
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const coefficient = value => finite(value) ? value.toFixed(3) : '历史不足';
  const percent = value => finite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : '历史不足';
  const cst = value => new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  }).format(new Date(value));
  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  let analysis;
  const days = () => $('analysisWindow').value;
  const matrix = () => analysis.matrices[days()];
  const validPairs = () => analysis.pairs.filter(p => String(p.days) === days() && finite(p.pearson));
  function metric(label, value, className) {
    const el = node('div', undefined, 'research-stat');
    el.append(node('span', label), node('strong', value, className));
    return el;
  }
  function populateSelect(id, selected) {
    const select = $(id);
    select.replaceChildren(...analysis.coins.map(inst => {
      const option = node('option', base(inst));
      option.value = inst;
      return option;
    }));
    select.value = selected;
  }
  function renderRanking() {
    const d = days(), reference = analysis.coins.indexOf($('benchmarkCoin').value);
    $('returnHeading').textContent = `${d} 天价格收益`;
    $('corrHeading').textContent = `与 ${base($('benchmarkCoin').value)} 相关性`;
    $('rankingBody').replaceChildren(...analysis.ranking.map((coin, i) => {
      const tr = node('tr');
      const name = node('td');
      name.append(node('strong', coin.base));
      if (coin.base === 'XAUT') name.append(node('span', '黄金', 'asset-tag'));
      const ret = coin[`return_${d}d_pct`];
      const result = node('td', percent(ret), finite(ret) ? (ret >= 0 ? 'up' : 'down') : 'muted');
      const covered = coin[`complete_${d}d`];
      tr.append(node('td', String(coin.rank), 'muted'), name,
        node('td', (Number(coin.volume_24h_usdt) / 1e6).toFixed(2)), result,
        node('td', coefficient(matrix()[i][reference]), finite(matrix()[i][reference]) ? '' : 'muted'),
        node('td', `${coin[`samples_${d}d`].toLocaleString('zh-CN')} / ${Number(d) * 24} 小时${covered ? ' · 完整' : ' · 不足'}`, covered ? 'muted' : 'warning'));
      return tr;
    }));
  }
  function selectPair(a, b, scroll = false) {
    $('pairA').value = a;
    $('pairB').value = b;
    renderPair();
    highlightPair();
    if (scroll) $('pairSection').scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  function highlightPair() {
    document.querySelectorAll('.heatmap button').forEach(button => {
      const selected = (button.dataset.a === $('pairA').value && button.dataset.b === $('pairB').value)
        || (button.dataset.b === $('pairA').value && button.dataset.a === $('pairB').value);
      button.classList.toggle('selected-cell', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  }
  function renderHeatmap() {
    const head = node('thead'), header = node('tr');
    header.append(node('th', '币种'));
    analysis.coins.forEach(inst => { const th = node('th', base(inst)); th.scope = 'col'; header.append(th); });
    head.append(header);
    const body = node('tbody');
    analysis.coins.forEach((a, i) => {
      const tr = node('tr'), th = node('th', base(a)); th.scope = 'row'; tr.append(th);
      analysis.coins.forEach((b, j) => {
        const value = matrix()[i][j];
        const cell = node('td');
        const color = finite(value) ? `corr-${Math.round(Math.max(-1, Math.min(1, value)) * 10) + 10}` : 'corr-na';
        const button = node('button', finite(value) ? value.toFixed(2) : 'NA', color);
        button.type = 'button'; button.dataset.a = a; button.dataset.b = b;
        button.title = `${base(a)} / ${base(b)}：${coefficient(value)}`;
        button.setAttribute('aria-label', button.title);
        button.addEventListener('click', () => selectPair(a, b, true));
        cell.append(button); tr.append(cell);
      });
      body.append(tr);
    });
    $('heatmap').replaceChildren(head, body);
    highlightPair();
  }
  function renderPair() {
    const a = $('pairA').value, b = $('pairB').value, d = days();
    const i = analysis.coins.indexOf(a), j = analysis.coins.indexOf(b);
    const value = matrix()[i][j];
    const detail = $('pairDetails'); detail.replaceChildren();
    const title = node('h3', `${base(a)} / ${base(b)} · ${d} 天`);
    detail.append(title);
    if (!finite(value)) {
      detail.append(node('p', '所选币种在该固定窗口内历史不足，无法计算可比相关性。未缩短窗口，也未填补价格。', 'warning'));
      return;
    }
    const pair = validPairs().find(p => (p.a === a && p.b === b) || (p.a === b && p.b === a));
    const stats = node('div', undefined, 'research-stats');
    stats.append(metric('Pearson 收益相关性', coefficient(value)),
      metric('Spearman 秩相关', a === b ? '1.000' : coefficient(pair?.spearman)),
      metric('共同小时收益样本', (pair?.samples ?? Number(d) * 24).toLocaleString('zh-CN')));
    if (pair) {
      stats.append(metric('同涨同跌比例', finite(pair.direction_agreement_pct) ? `${pair.direction_agreement_pct.toFixed(1)}%` : '—'));
      if (finite(pair.rolling30_min)) stats.append(metric('滚动 30 天相关范围', `${pair.rolling30_min.toFixed(3)} ~ ${pair.rolling30_max.toFixed(3)}`));
    }
    const ar = analysis.ranking[i][`return_${d}d_pct`], br = analysis.ranking[j][`return_${d}d_pct`];
    stats.append(metric(`${base(a)} 累计价格收益`, percent(ar), ar >= 0 ? 'up' : 'down'),
      metric(`${base(b)} 累计价格收益`, percent(br), br >= 0 ? 'up' : 'down'),
      metric('A 相对 B 收益差', `${ar - br >= 0 ? '+' : ''}${(ar - br).toFixed(2)} 个百分点`));
    detail.append(stats);
    const interpretation = a === b ? '这是币种与自身的比较，相关系数为 1。'
      : value < 0 ? '在此窗口内呈反向收益关联；这种关系可能随时间变化。'
      : value >= .7 ? '在此窗口内呈较强同向关联；累计收益仍可能明显不同。'
      : value >= .4 ? '在此窗口内存在一定共同波动，也有各自的价格变化。'
      : '在此窗口内共同波动较弱；这不等于反向对冲或再平衡盈利。';
    detail.append(node('p', interpretation, 'pair-interpretation'));
    if (a === 'XAUT-USDT' || b === 'XAUT-USDT') detail.append(node('p', 'XAUT 是黄金支持代币，资产属性不同于一般加密币。', 'warning'));
  }
  function renderLists() {
    const pairs = validPairs();
    const strong = [...pairs].sort((a, b) => b.pearson - a.pearson).slice(0, 5);
    const weak = pairs.filter(p => !$('excludeGold').checked || (p.a !== 'XAUT-USDT' && p.b !== 'XAUT-USDT'))
      .sort((a, b) => a.pearson - b.pearson).slice(0, 5);
    for (const [id, list] of [['strongPairs', strong], ['weakPairs', weak]]) {
      $(id).replaceChildren(...list.map((p, i) => {
        const button = node('button', undefined, 'ranked-pair'); button.type = 'button';
        button.append(node('span', `${i + 1}. ${base(p.a)} / ${base(p.b)}`), node('strong', coefficient(p.pearson)));
        button.addEventListener('click', () => selectPair(p.a, p.b, true));
        return button;
      }));
    }
  }
  function renderWindow() {
    const d = days(), [start, end] = analysis.windows_cst[d];
    const pairs = validPairs(), negative = pairs.filter(p => p.pearson < 0).length;
    const complete = analysis.ranking.filter(row => row[`complete_${d}d`]).length;
    $('windowDescription').textContent = `${cst(start)} → ${cst(end)}（北京时间） · 每币 ${Number(d) * 24} 个小时收益样本`;
    $('researchSummary').replaceChildren(metric('观察币种', '20'), metric('完整窗口币种', `${complete} / 20`),
      metric('可比较币对', String(pairs.length)), metric('负相关币对', String(negative)));
    $('matrixDownload').href = `${api('/api/correlation/download/correlation-')}${d}.csv`;
    renderRanking(); renderHeatmap(); renderPair(); renderLists();
  }
  function renderNotes() {
    const notes = $('coverageNotes'); notes.replaceChildren();
    const incomplete = analysis.ranking.filter(row => !row.complete_90d);
    incomplete.forEach(row => {
      const short = analysis.common_short_window;
      const i = analysis.coins.indexOf(row.symbol), btc = analysis.coins.indexOf('BTCUSDT');
      const corr = short.pearson_matrix[i][btc];
      notes.append(node('p', `${row.base}：${row.hourly_candles} 根已完结小时 K 线，不足完整 30 / 90 天。单列共同短窗 ${cst(short.start_ms)} → ${cst(short.end_ms)}，共 ${short.samples} 个小时收益样本，与 BTC 相关 ${coefficient(corr)}。短窗结果不能与完整窗口直接排名。`, 'warning'));
    });
    notes.append(node('p', 'XAUT：黄金支持代币，按非稳定币成交热度保留。它的低相关需要结合黄金资产属性理解。'));
    notes.append(node('p', '数据口径：小时收益 r(t) = ln[C(t) / C(t−1)]；缺失项不填补，币对采用相同固定时间窗口。Pearson / Spearman 经独立复核，统计不包含手续费和滑点。'));
    notes.append(node('p', '排名与相关性来自已发布的固定快照，不随分钟行情自动变化。后续发布新快照时，本页会显示新的截止时间。'));
  }
  async function load() {
    $('researchError').classList.add('hidden');
    $('researchContent').classList.add('hidden');
    $('retryResearch').disabled = true;
    try {
      const response = await fetch(api('/api/correlation'), {headers: {'Accept': 'application/json'}, signal: AbortSignal.timeout(15000)});
      if (!response.ok) {
        if (response.status === 403) throw new Error('访问被拒绝，请检查 Tailscale 本人身份连接。');
        const error = await response.json(); throw new Error(error.error || '无法读取分析数据');
      }
      analysis = await response.json();
      $('snapshotDate').textContent = cst(analysis.snapshot.snapshot_ms).split(' ')[0];
      $('snapshotTime').textContent = `排名 ${cst(analysis.snapshot.snapshot_ms)} · 数据截至 ${cst(analysis.analysis_end_ms)}`;
      populateSelect('benchmarkCoin', 'BTC-USDT'); populateSelect('pairA', 'BTC-USDT'); populateSelect('pairB', 'ETH-USDT');
      renderWindow(); renderNotes();
      $('researchContent').classList.remove('hidden');
    } catch (error) {
      $('researchErrorText').textContent = error.name === 'TimeoutError' ? '读取超时，请重新加载。' : error.message;
      $('researchError').classList.remove('hidden'); $('snapshotTime').textContent = '分析暂时无法加载';
    } finally { $('retryResearch').disabled = false; }
  }
  $('analysisWindow').addEventListener('change', renderWindow);
  $('benchmarkCoin').addEventListener('change', renderRanking);
  ['pairA', 'pairB'].forEach(id => $(id).addEventListener('change', () => {renderPair(); highlightPair();}));
  $('excludeGold').addEventListener('change', renderLists);
  $('retryResearch').addEventListener('click', load);
  load();
})();
