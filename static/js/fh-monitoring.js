(() => {
  const filters = document.querySelector('#fh-monthly-filters');
  if (!filters) return;
  const locations = [...filters.querySelectorAll('[data-fh-location]')];
  locations.forEach((field, index) => field.addEventListener('change', () => {
    locations.slice(index + 1).forEach(child => { child.value = ''; });
    filters.requestSubmit();
  }));
  const rules = JSON.parse(document.querySelector('#fh-rule-data').textContent);
  const targetRules = JSON.parse(document.querySelector('#fh-target-rule-data').textContent);
  const basis = document.querySelector('#fh-rule-basis');
  const selector = document.querySelector('#fh-rule-indicator');
  const updateRule = () => {
    const rule = (basis.value === 'target' ? targetRules : rules)[selector.value] || {};
    ['critical','good','excellent'].forEach(key => { document.querySelector('#fh-rule-' + key).value = rule[key] ?? ''; });
    document.querySelector('#fh-rule-direction').value = rule.direction || (['sam','mam','plw_sam','plw_mam','sam_male','sam_female','mam_male','mam_female'].includes(selector.value) ? 'lower' : 'higher');
  };
  selector.addEventListener('change', updateRule); updateRule();
  basis.addEventListener('change', updateRule);
  const data = JSON.parse(document.querySelector('#fh-trend-data').textContent);
  const svg = document.querySelector('#fh-trend-chart');
  const add = (name, attributes, text) => {
    const el = document.createElementNS('http://www.w3.org/2000/svg',name);
    Object.entries(attributes).forEach(([k,v]) => el.setAttribute(k,v));
    if (text !== undefined) el.textContent = text;
    svg.appendChild(el); return el;
  };
  if (!data.some(row => row.value !== null)) { add('text',{x:30,y:100,fill:'#64748b'},'No reported values for this indicator.'); return; }
  const maximum = Math.max(1,...data.map(row => row.value || 0));
  add('line',{x1:45,y1:180,x2:770,y2:180,stroke:'#94a3b8'});
  add('text',{x:5,y:20,fill:'#475569'},String(maximum));
  add('text',{x:15,y:180,fill:'#475569'},'0');
  let previous = null;
  data.forEach((row,i) => {
    const x = 55 + i * 700 / Math.max(1,data.length-1);
    if (i % Math.max(1,Math.ceil(data.length/8)) === 0 || i === data.length-1) add('text',{x,y:207,'text-anchor':'middle',fill:'#475569','font-size':12},row.month);
    if (row.value === null) { previous=null; return; }
    const y = 175-row.value/maximum*150;
    if (previous) add('line',{x1:previous.x,y1:previous.y,x2:x,y2:y,stroke:'#0f766e','stroke-width':3});
    const circle = add('circle',{cx:x,cy:y,r:5,fill:row.missing ? '#d97706' : '#0f766e'});
    const title=document.createElementNS('http://www.w3.org/2000/svg','title'); title.textContent=`${row.month}: ${row.value}${row.missing ? ' (partial)' : ''}`;circle.appendChild(title);
    previous={x,y};
  });
})();
