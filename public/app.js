const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
export const itemNames = { MYCHEW: '마이쮸', SESAME: '참깨 크래커', ACE: '에이스' };
export const escapeHTML = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function allocation(strategy = []) {
  return `<div class="allocation" aria-label="${strategy.map((n,i) => `${i+1}전선 ${Number(n)}명`).join(', ')}">${strategy.map((n,i) => `<span style="--amount:${(1-Math.exp(-Math.max(Number(n)||0,0)/30)).toFixed(3)}"><small>${i+1}전선</small>${Number(n)}</span>`).join('')}</div>`;
}
const errorMessages = {
  INVALID_STRATEGY: '각 전선에 0 이상의 정수를 입력하고 합을 101명으로 맞춰 주세요.',
  LIMIT_REACHED: '오늘의 세 번의 도전을 모두 사용했어요.',
  BOOTH_CLOSED: '지금은 작전을 준비하고 있어요. 운영 시간을 확인해 주세요.',
  CAP_REACHED: '오늘의 간식 상한에 도달해 접수를 마감했어요.',
  ALREADY_CLAIMED: '이미 수령 완료된 간식이에요.',
  OUT_OF_STOCK_CHOICE: '이 간식은 모두 소진됐어요. 다른 간식을 선택해 주세요.',
  POOL_TOO_SMALL: '대결 상대를 준비 중이에요. 잠시 후 다시 참여해 주세요.',
  UNAUTHORIZED: '로그인이 만료됐어요. 다시 시작해 주세요.',
  NICKNAME_TAKEN: '이미 쓰고 있는 닉네임이에요. 다른 닉네임을 입력해 주세요.',
  NICKNAME_REQUIRED: '먼저 닉네임을 정해 주세요.',
  NICKNAME_LOCKED: '닉네임은 한 번만 정할 수 있어요.',
  STUDENT_ID_CONFLICT: '학번이 다른 이름으로 등록되어 있어요. 스태프에게 말씀해 주세요.',
  RATE_LIMITED: '요청이 잠시 몰렸어요. 조금 기다린 후 다시 눌러 주세요.',
  NOT_CONFIGURED: '서버를 준비 중이에요. 운영진에게 문의해 주세요.',
  SETUP_REQUIRED: '서버를 준비 중이에요. 운영진에게 문의해 주세요.',
  DAY_CHANGED: '운영일이 바뀌었어요. 오늘의 전략을 확인하고 새로 제출해 주세요.',
  DATABASE_NOT_CONFIGURED: '서버를 준비 중이에요. 운영진에게 문의해 주세요.',
};
export async function api(path, { method='GET', body, ...options }={}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 25000);
  try {
    const response = await fetch(path, { method, credentials:'same-origin', cache:'no-store', headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body), signal:controller.signal, ...options });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const code = data.error?.code || `HTTP_${response.status}`;
      const err = new Error(errorMessages[code] || data.error?.message || '처리하지 못했어요. 잠시 후 다시 시도해 주세요.');
      err.code=code;err.status=response.status;throw err;
    }
    return data;
  } catch(err) {
    if (!err.code) { err.code='NETWORK';err.message='연결이 불안정해요. 다시 눌러도 제출 횟수는 한 번만 써요.'; }
    throw err;
  } finally { clearTimeout(timeout); }
}
let toastTimer;
export function showToast(message, error=false) {
  const el=$('#toast');el.textContent=message;el.className=error?'error':'';el.hidden=false;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>{el.hidden=true;},5500);
}
const memoryStorage = new Map();
const storage = {
  get(k,fallback=null){if(memoryStorage.has(k))return memoryStorage.get(k);try{return JSON.parse(localStorage.getItem(`blotto:${k}`)) ?? fallback;}catch{return fallback;}},
  set(k,v){memoryStorage.set(k,v);try{localStorage.setItem(`blotto:${k}`,JSON.stringify(v));}catch{}},
  remove(k){memoryStorage.delete(k);try{localStorage.removeItem(`blotto:${k}`);}catch{}},
};
function requestId(){const b=new Uint8Array(16);crypto.getRandomValues(b);return [...b].map(n=>n.toString(16).padStart(2,'0')).join('');}
const rate=v=>`${(Number(v||0)*100).toFixed(1)}%`;
const time=v=>v ? new Date(v).toLocaleTimeString('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit'}) : '—';
const stateNames={OPEN:'운영 중',PAUSED:'잠시 휴식',CLOSED:'개장 준비',CAP_REACHED:'접수 마감',DONE:'오늘 운영 종료'};
let state={}, historyItems=[], currentResult=null, currentView='', hallScope='DAY1', currentBoard=null, pollTimer=null, events=null, lastHeartbeat=0, lastRefresh=0, refreshing=false, streamHealthy=false, userEditing=false, currentHoldCancel=null, submitting=false;
const app=$('#app');
function goto(path){window.history.replaceState({},'',path);}
function flags(){return `<div class="field-flags" aria-hidden="true">${['I','II','III','IV','V'].map((v,i)=>`<div class="flag" style="--lane-color:var(--lane-${i+1})"><span class="flag-label">FRONT</span><span class="flag-number">${v}</span><span class="flag-stripe"></span></div>`).join('')}</div>`;}
function gameFrame(content){return `<div class="game-layout"><aside class="desk-aside"><div class="eyebrow">FIELD MANUAL / 101 : 5</div><h1>같은 병력.<br>다른 전략.<br>예측할 수 없는 승부.</h1><p>강한 전선 하나보다<br>더 많은 전선의 작은 승리가 중요합니다.</p>${flags()}<div class="rule-divider"></div><p>101명을 다섯 전선에 배치하세요.<br>지금까지 모인 모든 전략이 상대가 됩니다.</p></aside><section class="game-card ${currentView==='result'||currentView==='closed'?'watermarked':''}">${content}</section></div>`;}
// The test operating day has no hall of fame and is labelled instead of numbered.
const dayLabel=d=>d.test_mode?'🧪 테스트 모드':`${d.day_no||1}일차`;
function topLine(){return `<div class="topline"><span>${dayLabel(state)} · <b class="topline-name">${escapeHTML(state.needs_nickname?'지휘관':state.nickname||'지휘관')}</b></span><button class="text-btn" id="logout">다른 사람으로 시작</button></div>`;}
function bindLogout(){const el=$('#logout');if(el)el.onclick=async()=>{if(!await confirmDialog('다른 사람으로 시작할까요?',state.kind==='GUEST'?'외부인 임시 로그인은 로그아웃 후 닉네임만으로 복구할 수 없어요. 계속할까요?':'같은 학번과 이름으로 다시 들어오면 기존 기록과 남은 횟수가 이어집니다.','로그아웃'))return;try{await api('/api/session/logout',{method:'POST',body:{}});storage.remove('draft');storage.remove('pending');currentResult=null;historyItems=[];state={...state,participant_id:null,nickname:null};goto('/');renderLogin();}catch(e){showToast(e.message,true);}};}
async function privacyDialog(){
  try { state = await api('/api/state'); } catch {}
  const modal=$('#modal');modal.innerHTML=`<h2>개인정보 처리 안내</h2><p>작전 현황과 명예의 전당에는 참가자가 정한 닉네임만 공개합니다. 학번과 이름은 공개하지 않으며, 닉네임에 실명이나 연락처를 쓰지 마세요.</p><dl class="privacy-list"><dt>수집하는 정보</dt><dd>재학생의 학번·이름, 참가자의 닉네임, 로그인 세션 토큰 해시, 동의 시각, 제출 전략과 시각.</dd><dt>이용 목적</dt><dd>재학생 확인과 기록 이어가기, 게임 진행, 하루 3회 제한, 간식 지급 판정, 공개 순위에 닉네임 표시.</dd><dt>보유 및 파기</dt><dd>행사 종료 후 7일 이내 학번·이름·닉네임·로그인 세션을 서버와 백업에서 삭제합니다. 이후 개인을 알 수 없는 전략 기록만 남깁니다.</dd><dt>로그인 유지</dt><dd>재학생 로그인은 최대 30일, 외부인 임시 로그인은 최대 12시간 유지됩니다. 재학생은 같은 학번과 이름으로 다시 들어오면 기록이 이어지고, 외부인은 로그아웃하거나 세션이 끝나면 닉네임만으로 기존 계정을 복구할 수 없습니다.</dd><dt>서비스 운영 환경</dt><dd>${state.cloud_mode?'이 서비스는 Vercel의 웹·API 호스팅과 운영진이 설정한 외부 데이터베이스를 사용합니다. 클라우드 처리 위치와 사업자는 운영진에게 확인할 수 있습니다.':'이 서비스는 운영진이 관리하는 서버에서 실행됩니다. 실제 보관 위치와 백업 관리 방법은 운영진에게 문의해 주세요.'}</dd><dt>동의 거부 및 어린이 참여</dt><dd>동의하지 않을 수 있으나 게임 참여는 어렵습니다. 만 14세 미만은 보호자와 함께, 보호자 동의 후 참여해 주세요.</dd><dt>문의</dt><dd>${escapeHTML(state.privacy_contact||'Automata 부스 운영진에게 문의해 주세요.')}</dd></dl><div class="dialog-actions"><a class="btn secondary" href="/privacy.html" target="_blank" rel="noopener">안내 페이지</a><button class="btn" id="close-privacy">확인했어요</button></div>`;modal.showModal();$('#close-privacy').onclick=()=>modal.close();}
function confirmDialog(title,message,yes='확인'){
  const modal=$('#modal');return new Promise(resolve=>{modal.innerHTML=`<h2>${escapeHTML(title)}</h2><p>${escapeHTML(message)}</p><div class="dialog-actions"><button class="btn secondary" data-no>다시 확인</button><button class="btn" data-yes>${escapeHTML(yes)}</button></div>`;let done=false;const finish=v=>{if(done)return;done=true;modal.close();resolve(v);};$('[data-no]',modal).onclick=()=>finish(false);$('[data-yes]',modal).onclick=()=>finish(true);modal.oncancel=()=>finish(false);modal.showModal();});}
let loginEpoch=0;
function renderLogin(){
  const epoch=++loginEpoch;currentView='login';
  app.innerHTML=`<div class="arrival"><section class="hero"><div class="eyebrow">AUTOMATA STRATEGY LAB · 2026</div><h1>병사 <em>101명.</em><br>전선 <em>다섯 곳.</em><br>당신의 선택은?</h1><p class="hero-intro">정답은 없습니다. 서로를 이기는 전략만 있을 뿐.<br>다섯 개의 전선에 병력을 배치하고<br>나만의 승리 공식을 찾아보세요.</p><div class="battle-illustration">${flags()}<p class="illustration-note">ONE ARMY. FIVE FRONTS. COUNTLESS POSSIBILITIES.</p></div><ol class="rules-mini"><li><b>01</b> 각 전선에서 병력이 많으면 승리</li><li><b>02</b> 더 많은 전선을 가져오면 대결 승리</li><li><b>03</b> 하루 세 번의 도전 · 첫 제출로 간식 결정</li></ol></section><section class="join-panel"><div class="panel-top"><div><div class="eyebrow">ENLISTMENT / 참가 등록</div><h2>작전에 참가하세요</h2></div><img src="/assets/logo-emblem.svg" alt="" width="54" height="54"></div><div class="join-tabs" role="tablist" aria-label="참가자 구분"><button type="button" id="student-tab" class="active" role="tab" aria-selected="true" aria-controls="student-panel" data-kind="STUDENT">KSA 재학생</button><button type="button" id="guest-tab" role="tab" aria-selected="false" aria-controls="guest-panel" tabindex="-1" data-kind="GUEST">외부인</button></div><section id="student-panel" role="tabpanel" aria-labelledby="student-tab"><form class="join-form" id="student-form"><label class="field"><span>학번</span><input name="student_id" autocomplete="off" inputmode="numeric" pattern="[0-9]{2}-?[0-9]{3}" maxlength="6" placeholder="예: 26-001" required></label><label class="field"><span>이름</span><input name="name" autocomplete="off" minlength="2" maxlength="30" placeholder="실명" required></label><label class="field"><span>닉네임 <small class="muted">화면에 표시돼요</small></span><input name="nickname" autocomplete="off" minlength="2" maxlength="10" placeholder="2~10글자, 나만의 호출명" required></label><p class="hint">작전 현황과 명예의 전당에는 닉네임만 표시돼요. 학번과 이름은 공개되지 않아요.</p><label class="consent"><input type="checkbox" name="consent" required><span>개인정보(학번·이름) 수집·이용 및 닉네임 공개에 동의합니다. (필수)<br><button type="button" class="text-btn" data-privacy>처리 안내 보기</button></span></label><p id="student-error" class="error-text" role="alert"></p><button type="submit" class="btn full" id="student-submit" disabled>재학생으로 시작하기 <span aria-hidden="true">→</span></button></form><p class="join-fineprint">같은 학번과 이름으로 다시 들어오면<br>기존 닉네임과 기록, 남은 도전 횟수가 이어집니다.</p></section><section id="guest-panel" role="tabpanel" aria-labelledby="guest-tab" hidden><form class="join-form" id="join-form"><label class="field"><span>외부인 닉네임 <small class="muted">화면에 표시돼요</small></span><input name="nickname" autocomplete="off" minlength="2" maxlength="10" placeholder="2~10글자, 나만의 호출명" required></label><p class="hint">외부인은 닉네임으로 임시 참여해요. 실명·연락처는 사용하지 마세요.</p><label class="consent"><input type="checkbox" name="consent" required><span>개인정보 수집·이용에 동의합니다. (필수)<br><button type="button" class="text-btn" data-privacy>처리 안내 보기</button></span></label><p id="join-error" class="error-text" role="alert"></p><button type="submit" class="btn full" id="join-submit" disabled>외부인으로 시작하기 <span aria-hidden="true">→</span></button></form><p class="join-fineprint">임시 로그인은 최대 12시간 동안 유지돼요.<br>로그아웃하거나 세션이 끝나면 닉네임만으로 복구할 수 없어요.</p></section></section></div>`;
  const panel=$('.join-panel'),forms=$$('form',panel);
  let kind='STUDENT',busy=false;
  const active=()=>epoch===loginEpoch&&currentView==='login'&&panel.isConnected;
  const valid=()=>{if(active())forms.forEach(f=>{$('[type=submit]',f).disabled=busy||!f.checkValidity();});};
  const lock=value=>{busy=value;if(!active())return;$$('[data-kind]',panel).forEach(b=>b.disabled=value);$$('input',panel).forEach(i=>i.disabled=value);valid();};
  const loginError=err=>err.code==='NETWORK'?'로그인 응답을 확인하지 못했어요. 연결을 확인한 뒤 다시 시도해 주세요.':err.message;
  const complete=async()=>{storage.remove('draft');storage.remove('pending');currentResult=null;state=await api('/api/state');historyItems=(await api('/api/me/submissions')).items;if(active())renderInitialParticipant();};
  const tabs=$$('[data-kind]',panel);
  function chooseTab(el){if(busy)return;kind=el.dataset.kind;tabs.forEach(b=>{const selected=b===el;b.classList.toggle('active',selected);b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;});$('#student-panel').hidden=kind!=='STUDENT';$('#guest-panel').hidden=kind!=='GUEST';valid();}
  tabs.forEach(el=>{el.onclick=()=>chooseTab(el);el.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const target=e.key==='Home'?tabs[0]:e.key==='End'?tabs[1]:tabs[el===tabs[0]?1:0];chooseTab(target);target.focus();}};});
  $$('[data-privacy]',panel).forEach(el=>el.onclick=privacyDialog);
  forms.forEach(f=>f.addEventListener('input',valid));
  // FormData is read before lock() disables the inputs, since disabled fields are left out of it.
  const join=(form,which,fields,label)=>{form.onsubmit=async e=>{e.preventDefault();if(busy||kind!==which||!form.reportValidity())return;const fd=new FormData(form);const button=$('[type=submit]',form),error=$('.error-text',form);lock(true);button.textContent='참가 등록 중…';error.textContent='';
    try{await api('/api/session',{method:'POST',body:{kind:which,...fields(fd),consent:fd.has('consent')}});if(active())await complete();}
    catch(err){if(active()){error.textContent=loginError(err);button.textContent=label;}}
    finally{if(active())lock(false);}
  };};
  join($('#student-form'),'STUDENT',fd=>({student_id:fd.get('student_id').trim(),name:fd.get('name').trim(),nickname:fd.get('nickname').trim()}),'재학생으로 시작하기 →');
  join($('#join-form'),'GUEST',fd=>({nickname:fd.get('nickname').trim()}),'외부인으로 시작하기 →');
}
function renderInitialParticipant(){
  if(state.needs_nickname)return renderNickname();
  const today=historyItems.filter(r=>r.day_no===state.day_no);const pending=storage.get('pending');
  if(pending && pending.participant_id!==state.participant_id)storage.remove('pending');
  if((location.pathname==='/result' || today.some(r=>r.reward?.status==='ISSUED')) && today.length){currentResult=today.find(r=>r.reward?.status==='ISSUED')||today[0];renderResult();}
  else if(!state.open_now || state.attempts_left<=0)renderClosed();else renderPlay();
}
// Students from before nicknames existed pick one once before playing.
function renderNickname(){
  currentHoldCancel?.();currentView='nickname';goto('/');
  app.innerHTML=gameFrame(`${topLine()}<form class="join-form" id="nickname-form"><div class="eyebrow">CALL SIGN / 닉네임</div><h2>작전에서 쓸 닉네임을 정하세요</h2><label class="field"><span>닉네임 <small class="muted">작전 현황과 명예의 전당에 표시돼요</small></span><input name="nickname" autocomplete="off" minlength="2" maxlength="10" placeholder="2~10글자, 나만의 호출명" required></label><p class="hint">닉네임은 한 번 정하면 바꿀 수 없어요. 실명·학번·연락처는 쓰지 마세요.</p><p id="nickname-error" class="error-text" role="alert"></p><button type="submit" class="btn full" id="nickname-submit">이 닉네임으로 시작하기 <span aria-hidden="true">→</span></button></form>`);
  bindLogout();const form=$('#nickname-form');let busy=false;
  form.onsubmit=async e=>{e.preventDefault();if(busy||!form.reportValidity())return;busy=true;const button=$('#nickname-submit');button.disabled=true;$('#nickname-error').textContent='';
    try{await api('/api/me/nickname',{method:'POST',body:{nickname:new FormData(form).get('nickname').trim()}});state=await api('/api/state');if(currentView==='nickname')renderInitialParticipant();}
    catch(err){if(currentView==='nickname'){$('#nickname-error').textContent=err.message;button.disabled=false;}}
    finally{busy=false;}};
}
function renderPlay(){
  currentHoldCancel?.();currentView='play';goto('/play');const draft=storage.get('draft');const pending=storage.get('pending');
  let values=pending?.participant_id===state.participant_id ? pending.strategy : draft?.participant_id===state.participant_id&&draft.day_no===state.day_no?draft.strategy:[20,20,20,20,21];
  const first=state.attempts_left===3;
  app.innerHTML=gameFrame(`${topLine()}<div class="eyebrow">DEPLOYMENT / 병력 배치</div><h2>${first?'첫 제출로 간식이<br>결정됩니다':'이번 제출은<br>순위만 반영돼요'}</h2><p class="attempts"><span class="attempt-dots" aria-hidden="true">${Array.from({length:3},(_,i)=>`<i class="${i<state.attempts_left?'available':''}"></i>`).join('')}</span>오늘 3회 중 <b>${state.attempts_left}회</b> 남음</p><form id="play-form"><div class="lane-grid">${values.map((v,i)=>`<div class="lane-field" style="--lane-color:var(--lane-${i+1})"><label for="lane-${i}">전선 ${i+1}</label><input id="lane-${i}" name="lane${i}" type="number" inputmode="numeric" min="0" max="101" step="1" value="${escapeHTML(v)}" required aria-label="전선 ${i+1} 병력"></div>`).join('')}</div><div class="sum-display" id="sum-display"><span>총 배치 병력</span><span><strong id="sum-value">101</strong> / 101</span></div><div class="sum-meter"><span id="sum-meter"></span></div><p class="sum-caption" id="sum-caption"></p><div class="input-actions"><button type="button" class="btn secondary" id="equal">균등 배분</button><button type="button" class="btn secondary" id="clear">초기화</button></div><p class="error-text" id="submit-error" role="alert"></p>${pending?'<div class="notice pending-notice">아직 응답을 확인하지 못한 제출이 있어요. 같은 요청으로 안전하게 다시 확인합니다.</div>':''}<button class="btn full submit-btn" id="submit-strategy" type="submit">${pending?'제출 결과 다시 확인':'작전 개시 · 제출하기'} <span aria-hidden="true">→</span></button></form><div class="below-action"><button type="button" class="text-btn" id="my-history">내 작전 기록</button><a href="/board">실시간 순위 보기</a></div>`);
  bindLogout();const inputs=$$('.lane-field input');
  const update=()=>{values=inputs.map(i=>i.value===''?'':Number(i.value));const integer=values.every(v=>v!==''&&Number.isInteger(v)&&v>=0&&v<=101);const sum=values.reduce((a,b)=>a+Number(b||0),0);const good=integer&&sum===101;$('#sum-value').textContent=String(sum);$('#sum-display').classList.toggle('valid',good);$('#sum-meter').style.width=`${Math.max(0,Math.min(100,sum/101*100))}%`;$('#sum-caption').textContent=!integer?'각 칸에 0 이상의 정수를 입력해 주세요.':sum===101?'✓ 좋아요, 제출할 수 있어요.':sum<101?`병사 ${101-sum}명이 더 필요해요.`:`병사 ${sum-101}명이 너무 많아요.`;$('#sum-caption').classList.toggle('valid',good);$('#submit-strategy').disabled=!good||submitting;storage.set('draft',{participant_id:state.participant_id,day_no:state.day_no,strategy:values});};
  inputs.forEach(i=>{i.addEventListener('input',()=>{userEditing=true;update();});if(pending)i.readOnly=true;});
  $('#equal').onclick=()=>{inputs.forEach((el,i)=>el.value=i===4?21:20);update();};$('#clear').onclick=()=>{inputs.forEach(el=>el.value=0);update();};
  if(pending){$('#equal').disabled=true;$('#clear').disabled=true;}
  $('#my-history').onclick=renderHistory;update();
  $('#play-form').onsubmit=async e=>{e.preventDefault();if(submitting)return;if(!pending&&first&&!await confirmDialog('첫 제출입니다.','제출하면 수정할 수 없고, 지금의 전략으로 오늘의 간식이 결정됩니다. 제출할까요?','제출하기'))return;let req=storage.get('pending');if(!req){req={request_id:requestId(),strategy:values,participant_id:state.participant_id,day_no:state.day_no};storage.set('pending',req);}submitting=true;$('#submit-strategy').disabled=true;$('#submit-strategy').textContent='대결 결과 계산 중…';$('#submit-error').textContent='';
    try{const result=await api('/api/submissions',{method:'POST',body:{request_id:req.request_id,strategy:req.strategy,expected_day_no:req.day_no}});storage.remove('pending');currentResult=result;historyItems=[result,...historyItems.filter(r=>r.id!==result.id)];state={...state,attempts_left:result.attempts_left};renderResult();refreshState().catch(()=>{});}
    catch(err){const ambiguous=err.code==='NETWORK'||err.status>=500;if(!ambiguous)storage.remove('pending');if(['BOOTH_CLOSED','CAP_REACHED','LIMIT_REACHED'].includes(err.code)){await refreshState().catch(()=>{});renderClosed();showToast(err.message,true);}else if(err.code==='UNAUTHORIZED'){state.participant_id=null;renderLogin();showToast(err.message,true);}else{renderPlay();$('#submit-error').textContent=err.message;}}
    finally{submitting=false;const button=$('#submit-strategy');if(button){button.disabled=false;button.textContent=storage.get('pending')?'제출 결과 다시 확인 →':'작전 개시 · 제출하기 →';}}
  };
}
function renderResult(){
  if(!currentResult)return renderInitialParticipant();currentHoldCancel?.();currentView='result';goto('/result');const r=currentResult;const rank=r.rank_estimate;const reward=r.reward;const winRate=(Number(r.win_rate)*100).toFixed(1);const top=r.top_bp!=null?Math.max(1,Math.ceil(r.top_bp/100)):rank?.rank&&rank.of?Math.max(1,Math.round(rank.rank/rank.of*100)):null;
  app.innerHTML=gameFrame(`${topLine()}<div class="result-summary"><div class="eyebrow">AFTER ACTION REPORT / ${r.attempt_no}차 작전</div>${top?`<div class="result-rate"><span class="prefix">상위</span>${top}<span>%</span></div>`:''}<div class="result-rate"><span class="prefix">승률</span>${winRate}<span>%</span></div><p class="result-rank">${rank?.rank?`임시 순위 <b>${rank.rank}위</b> / ${rank.of}명`:'순위를 계산하고 있어요'} · 상대 ${Number(r.opponents)}개 전략</p><p class="hint">제출 순간의 승률 · 순위는 이후 달라질 수 있어요.</p></div>${reward?rewardMarkup(reward):'<div class="notice">이번 제출은 순위만 반영돼요.<br>오늘의 간식은 첫 제출 결과에서 확인해 주세요.</div>'}<div class="nemesis"><h3>${r.nemesis?'이 전략이 당신의 천적':'천적 없음!'}</h3>${r.nemesis?`${allocation(r.nemesis.strategy)}<p>전선 ${r.nemesis.lost_lanes.length}개를 내주고 있어요 (${r.nemesis.lost_lanes.join('·')}번).</p>`:'<p>제출할 때 나를 이긴 상대가 없었어요.</p>'}</div><button class="btn full" id="retry-play" ${state.attempts_left<=0?'disabled':''}>${state.attempts_left>0?`다시 도전 · 오늘 ${state.attempts_left}회 남음`:'오늘의 도전을 모두 사용했어요'}</button><div class="below-action"><button type="button" class="text-btn" id="my-history">내 작전 기록</button><a href="/hall">명예의 전당</a></div>`);
  bindLogout();$('#my-history').onclick=renderHistory;$('#retry-play').onclick=()=>{if(state.open_now)renderPlay();else renderClosed();};if(reward)bindReward(reward);tickClocks();
}
function rewardMarkup(r){
  if(r.status==='VOID')return '<div class="notice">이 수령권은 무효 처리됐어요. 스태프에게 문의해 주세요.</div>';
  if(r.status==='CLAIMED')return `<div class="receipt"><div class="seal"><img src="/assets/logo-emblem.svg" alt="">수령 완료</div><p>${escapeHTML(itemNames[r.choice]||r.choice)}</p><code>${escapeHTML(r.code)}</code><time data-clock></time><span class="live-label">실시간 수령 확인 화면</span><p class="hint">${time(r.claimed_at)} 수령 완료 · 스태프에게 보여 주세요.</p></div>`;
  const top=r.kind==='TOP_CHOICE';
  const selected=top?r.choice:'MYCHEW';
  return `<section class="reward-card"><div class="eyebrow">SUPPLY / 오늘의 간식</div><h3>${top?'상위권! 간식을 골라 주세요':r.premium_exhausted?'고급 소진 · 마이쮸를 드려요':'참여 감사! 마이쮸 1개'}</h3><p>간식 전담 스태프 앞에서 화면을 보여 주세요.</p>${top?`<div class="choice-options" aria-label="간식 선택">${(r.choices||['MYCHEW','SESAME','ACE']).map(k=>`<button type="button" data-choice="${k}" aria-pressed="${r.choice===k}">${escapeHTML(itemNames[k])}</button>`).join('')}</div>`:'<p style="margin:12px 0">마이쮸 바구니에서 1개를 받으세요.</p>'}<p class="hint" id="choice-hint">${selected?`${escapeHTML(itemNames[selected])}를 골랐어요. 스태프 앞에서 1초간 눌러 주세요.`:'간식을 선택한 다음 스태프 앞에서 길게 눌러 주세요.'}</p><p class="error-text" id="claim-error" role="alert"></p><button type="button" id="hold-claim" class="btn full hold-btn" style="margin-top:13px" ${!selected?'disabled':''}><span>꾹 눌러 수령 완료</span></button><p class="hint" style="text-align:center;margin-top:8px">수령 코드 ${escapeHTML(r.code)} · 길게 누르기 1초</p></section>`;
}
function bindReward(reward){
  if(reward.status==='CLAIMED'||reward.status==='VOID')return;
  const resultId=currentResult.id;
  let choice=reward.kind==='BASIC'?'MYCHEW':reward.choice;
  let started=0,raf=null,busy=false,choiceConfirmed=Boolean(choice);
  const hold=$('#hold-claim');
  const choiceButtons=$$('[data-choice]');
  const cancel=()=>{started=0;if(raf)cancelAnimationFrame(raf);hold.style.setProperty('--hold-progress','0');};
  currentHoldCancel=cancel;
  const isCurrent=()=>currentView==='result'&&currentResult?.id===resultId;
  const applyReward=updated=>{
    historyItems=historyItems.map(r=>r.id===resultId?{...r,reward:{...r.reward,...updated}}:r);
    if(currentResult?.id===resultId)currentResult={...currentResult,reward:{...currentResult.reward,...updated}};
  };
  const recoverHistory=async()=>{
    const res=await api('/api/me/submissions');historyItems=res.items;
    const fresh=historyItems.find(r=>r.id===resultId);
    if(!fresh)throw new Error('결과를 다시 확인해 주세요.');
    if(currentResult?.id===resultId)currentResult=fresh;
    return fresh;
  };
  const refreshAfterChoice=async()=>{
    await recoverHistory().catch(()=>{});
    await refreshState().catch(()=>{});
    if(isCurrent())renderResult();
  };
  choiceButtons.forEach(button=>button.onclick=async()=>{
    const requested=button.dataset.choice;
    if(busy||(choiceConfirmed&&choice===requested))return;
    busy=true;cancel();hold.disabled=true;
    choiceButtons.forEach(b=>b.disabled=true);
    $('#choice-hint').textContent='간식 선택을 저장하고 있어요…';
    $('#claim-error').textContent='';
    try{
      const saved=await api(`/api/entitlements/${encodeURIComponent(reward.entitlement_id)}/choice`,{method:'POST',body:{choice:requested}});
      applyReward(saved);choice=saved.choice;choiceConfirmed=true;
      await refreshAfterChoice();
    }catch(err){
      if(err.code==='NETWORK'||err.status>=500||err.code==='ALREADY_CLAIMED'){
        try{
          const fresh=await recoverHistory();
          choice=fresh.reward?.choice;choiceConfirmed=Boolean(choice);
          await refreshState().catch(()=>{});
          if(isCurrent())renderResult();
          if(choice!==requested&&fresh.reward?.status==='ISSUED')showToast('변경되지 않았어요. 원하는 간식을 다시 골라 주세요.',true);
        }catch{
          // Until the server choice is known, neither the old nor new choice may be claimed.
          choice=null;choiceConfirmed=false;
          if(isCurrent()){
            choiceButtons.forEach(b=>{b.disabled=false;b.setAttribute('aria-pressed','false');});
            $('#choice-hint').textContent='선택 저장 여부를 확인하지 못했어요. 원하는 간식을 다시 눌러 주세요.';
            $('#claim-error').textContent=err.message;
            hold.disabled=true;
          }
        }
      }else if(isCurrent()){
        renderResult();
        const error=$('#claim-error');if(error)error.textContent=err.message;
      }
    }finally{busy=false;}
  });
  const claim=async()=>{
    if(busy||!choice||!choiceConfirmed)return;
    busy=true;cancel();hold.disabled=true;choiceButtons.forEach(b=>b.disabled=true);
    hold.querySelector('span').textContent='수령 기록 중…';
    try{
      const res=await api(`/api/entitlements/${encodeURIComponent(reward.entitlement_id)}/claim`,{method:'POST',body:{choice}});
      applyReward(res);if(isCurrent())renderResult();
    }catch(err){
      if(err.code==='ALREADY_CLAIMED'||err.code==='NETWORK'||err.status>=500){
        try{const fresh=await recoverHistory();if(fresh.reward?.status==='CLAIMED'){if(isCurrent())renderResult();return;}}catch{}
      }
      if(isCurrent()){
        $('#claim-error').textContent=err.message;
        hold.disabled=false;hold.querySelector('span').textContent='꾹 눌러 수령 완료';
        choiceButtons.forEach(b=>b.disabled=false);
      }
    }finally{busy=false;}
  };
  const frame=t=>{if(!started)return;const progress=Math.min(1,(t-started)/1000);hold.style.setProperty('--hold-progress',String(progress));if(progress>=1)claim();else raf=requestAnimationFrame(frame);};
  const start=e=>{if(hold.disabled||busy||started)return;e.preventDefault();started=performance.now();raf=requestAnimationFrame(frame);};
  hold.addEventListener('pointerdown',e=>{if(e.button!==0)return;hold.setPointerCapture?.(e.pointerId);start(e);});
  hold.addEventListener('pointerup',cancel);hold.addEventListener('pointercancel',cancel);hold.addEventListener('pointerleave',cancel);
  hold.addEventListener('contextmenu',e=>e.preventDefault());
  hold.addEventListener('keydown',e=>{if((e.key===' '||e.key==='Enter')&&!e.repeat)start(e);});
  hold.addEventListener('keyup',e=>{if(e.key===' '||e.key==='Enter')cancel();});hold.addEventListener('blur',cancel);
}
function renderClosed(){
  currentView='closed';goto('/closed');const exhausted=state.attempts_left<=0;const title=exhausted?'오늘의 작전을 마쳤어요':state.booth_state==='CAP_REACHED'?'오늘의 접수가 마감됐어요':state.booth_state==='PAUSED'?'지금은 쉬는 시간이에요':state.booth_state==='DONE'?'오늘의 운영을 마쳤어요':'다음 작전을 준비 중이에요';
  const desc=exhausted?'오늘 주어진 세 번의 도전을 모두 사용했어요. 다른 지휘관들의 전략을 살펴보세요.':state.next_open?`${new Date(state.next_open).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'})}에 다시 만나요.`:'운영진이 개장하면 이 화면에서 자동으로 참여할 수 있어요.';
  app.innerHTML=gameFrame(`${topLine()}<div class="closed-view"><img src="/assets/logo-emblem.svg" alt=""><div class="eyebrow">FIELD STATUS</div><h2>${title}</h2><p>${escapeHTML(desc)}</p><a class="btn full" href="/board">실시간 작전 현황 보기</a><button type="button" class="text-btn" id="my-history" style="margin-top:12px">내 작전 기록 · 간식 수령 확인</button></div>`);bindLogout();$('#my-history').onclick=renderHistory;
}
async function renderHistory(){
  currentView='history';try{historyItems=(await api('/api/me/submissions')).items;}catch(e){showToast(e.message,true);return;}
  app.innerHTML=gameFrame(`${topLine()}<div class="eyebrow">OPERATION LOG</div><h2>내 작전 기록</h2><p class="hint">각 제출 당시의 승률입니다. 간식 수령 상태는 실시간으로 확인합니다.</p><div class="history-list">${historyItems.length?historyItems.map((r,i)=>`<article class="history-item"><div class="row"><span>${r.day_no}일차 · ${r.attempt_no}차 작전</span><strong>${rate(r.win_rate)}</strong></div>${allocation(r.strategy)}<button type="button" class="btn secondary" data-history="${i}">결과 보기 ${r.reward?.status==='ISSUED'?'· 미수령 간식':''}</button></article>`).join(''):'<p class="no-result">아직 제출한 작전이 없어요.</p>'}</div><button class="btn full" id="back-play" style="margin-top:20px">돌아가기</button>`);bindLogout();$$('[data-history]').forEach(b=>b.onclick=()=>{currentResult=historyItems[Number(b.dataset.history)];renderResult();});$('#back-play').onclick=()=>state.open_now&&state.attempts_left>0?renderPlay():renderClosed();
}
function renderBoard(data){
  currentView='board';currentBoard=data;document.body.classList.add('is-board');const rows=data.rankings||[];
  app.innerHTML=`<div class="board-shell"><div class="board-title"><div><div class="eyebrow">LIVE OPERATIONS / 실시간 작전 현황</div><h1>1등 전략은 존재하지 않는다.</h1></div><div class="board-meta"><span>${dayLabel(data)} · <time data-clock data-short></time></span><span class="badge"><i class="status-dot"></i>${escapeHTML(stateNames[data.booth_state]||'준비 중')}</span><div class="sync-status" id="sync-status"></div></div></div><div class="board-grid"><section class="board-panel"><div class="panel-title"><h2>상위 10개 전략의 배분</h2><span class="caption">현재 누적 풀 ${Number(data.pool_size||0)}개 · 임시 순위</span></div>${rows.length?`<table class="rank-table"><thead><tr><th>RANK</th><th>지휘관</th><th>병력 배분 / 전선 1–5</th><th>승률</th></tr></thead><tbody>${rows.map((r,i)=>`<tr><td>${String(r.rank||i+1).padStart(2,'0')}</td><td>${escapeHTML(r.nickname)}</td><td>${allocation(r.strategy)}</td><td>${rate(r.win_rate)}</td></tr>`).join('')}</tbody></table>`:'<div class="board-empty">아직 기록된 작전이 없습니다.<br>첫 번째 지휘관을 기다립니다.</div>'}</section><aside class="board-side"><section class="board-panel stock-panel"><div class="eyebrow">SUPPLY REMAINING</div><h2>오늘 남은 마이쮸</h2><div class="stock-number">${Number(data.mychew_left||0)}<small>개</small></div><p>마이쮸 또는 고급 간식 상한 도달 시 접수 마감</p></section><section class="board-panel cycle-panel"><h2>${data.cycle?'비추이 순환 발견!':'전략의 순환'}</h2>${data.cycle?cycleMarkup(data.cycle):'<div class="cycle-empty">아직 순환이 없어요.<br>새로운 전략을 기다리고 있습니다.</div>'}<p>하나의 최강 전략은 없다 · A → B → C → A</p></section></aside></div><div class="ticker"><b>RECENT</b><span>${(data.recent||[]).slice(0,5).map(r=>`<span>${escapeHTML(r.nickname)} &nbsp; ${rate(r.win_rate)}</span>`).join('')||'첫 작전이 시작되면 여기에 표시됩니다.'}</span></div></div>`;tickClocks();updateSync();
}
function cycleMarkup(cycle){return `<div class="cycle-graph"><svg viewBox="0 0 340 245" aria-hidden="true"><defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 10 5 0 10z" fill="#B7791F"/></marker></defs><path d="M141 45H195M270 100 210 178M125 180 70 100" fill="none" stroke="#B7791F" stroke-width="2" marker-end="url(#arrow)"/></svg>${cycle.map((r,i)=>`<div class="cycle-node"><span>${'ABC'[i]} · ${escapeHTML(r.nickname)}</span>${allocation(r.strategy)}</div>`).join('')}</div>`;}
async function loadHall(scope=hallScope){
  hallScope=scope;currentView='hall';document.body.classList.remove('is-board');try{const d=await api(`/api/hall?scope=${scope}`);renderHall(d);}catch(e){renderConnectionError(e);}
}
function renderHall(d){
  const rows=d.rankings||[];app.innerHTML=`<div class="hall-shell"><header class="hall-heading"><img src="/assets/logo-emblem.svg" alt=""><div class="eyebrow">HALL OF FAME / AUTOMATA</div><h1>명예의 전당</h1><p>${d.frozen?`${time(d.frozen_at)} 확정된 기록 · 흔들리지 않는 당신의 한 수`:'하루의 모든 작전이 끝나면, 최고의 전략이 기록됩니다.'}</p></header><div class="hall-tabs" role="tablist" aria-label="명예의 전당 운영일">${['DAY1','DAY2','DAY3','OVERALL'].map((s,i)=>`<button type="button" role="tab" aria-selected="${s===hallScope}" data-scope="${s}" class="btn ${s===hallScope?'':'secondary'}">${i===3?'종합':`${i+1}일차`}</button>`).join('')}</div>${d.frozen&&rows.length?`<div class="podium">${rows.slice(0,3).map((r,i)=>`<article class="podium-card ${['first','second','third'][i]}"><div class="rank-label">${i+1}위</div><div class="medal" aria-label="${i+1}위 훈장">${'★'.repeat(3-i)}</div><h2>${escapeHTML(r.nickname)}</h2><div class="rate">${rate(r.win_rate)}</div>${allocation(r.strategy)}</article>`).join('')}</div><div class="hall-list">${rows.slice(3,10).map((r,i)=>`<article class="hall-row"><span class="muted">${i+4}</span><span>${escapeHTML(r.nickname)}</span>${allocation(r.strategy)}<strong>${rate(r.win_rate)}</strong></article>`).join('')}</div>`:`<div class="hall-empty"><h2>${d.frozen?'기록된 참가자가 없어요':'아직 순위가 확정되지 않았어요'}</h2><p>${d.frozen?'다른 운영일의 기록을 확인해 주세요.':'마감 후 운영진이 확정한 상위 10명을 공개합니다.'}</p><a href="/board" class="btn secondary" style="margin-top:20px">실시간 순위 보기</a></div>`}</div>`;$$('[data-scope]').forEach(b=>b.onclick=()=>loadHall(b.dataset.scope));
}
function renderConnectionError(e){app.innerHTML=`<div class="loading-screen"><img src="/assets/logo-emblem.svg" alt="" width="72" height="72"><div style="text-align:center"><h2>잠시 연결을 확인해 주세요</h2><p class="muted" style="margin:15px 0;max-width:440px">${escapeHTML(e.message)}</p><button type="button" class="btn" id="reconnect">다시 연결</button></div></div>`;$('#reconnect').onclick=()=>location.reload();}
function tickClocks(){$$('[data-clock]').forEach(el=>el.textContent=new Date().toLocaleTimeString('ko-KR',{timeZone:'Asia/Seoul',hour12:false,hour:'2-digit',minute:'2-digit',...(el.hasAttribute('data-short')?{}:{second:'2-digit'})}));}
function updateSync(){const el=$('#sync-status');if(!el)return;const delayed=!streamHealthy;el.textContent=delayed?'갱신 지연 · 5초 간격 확인':'실시간 연결됨';el.classList.toggle('is-delayed',delayed);}
async function refreshState(){const previous=state;state=await api('/api/state');if(!state.participant_id&&previous.participant_id&&currentView!=='board'&&currentView!=='hall'&&currentView!=='admin'){renderLogin();return;}if(currentView==='closed'&&state.participant_id&&state.open_now&&state.attempts_left>0)renderPlay();else if(currentView==='play'&&(!state.open_now||state.attempts_left<=0)&&!submitting&&!storage.get('pending'))renderClosed();return state;}
async function refreshLive(){if(refreshing)return;refreshing=true;try{if(currentView==='board'){renderBoard(await api('/api/scoreboard'));}else if(currentView==='hall'){await loadHall();}else if(currentView!=='admin'){await refreshState();}lastRefresh=Date.now();}catch{streamHealthy=false;updateSync();}finally{refreshing=false;}}
function startLive(){
  if(events)events.close();if(pollTimer)clearInterval(pollTimer);
  const connect=()=>{try{events=new EventSource('/api/events');events.onopen=()=>{lastHeartbeat=Date.now();streamHealthy=true;updateSync();};events.onerror=()=>{streamHealthy=false;updateSync();};events.addEventListener('heartbeat',()=>{lastHeartbeat=Date.now();streamHealthy=true;updateSync();});events.addEventListener('board.updated',e=>{lastHeartbeat=Date.now();streamHealthy=true;if(currentView==='board'){try{renderBoard(JSON.parse(e.data));lastRefresh=Date.now();}catch{refreshLive();}}});events.addEventListener('state.changed',()=>{lastHeartbeat=Date.now();streamHealthy=true;if(currentView!=='admin')refreshLive();});}catch{streamHealthy=false;}};
  connect();pollTimer=setInterval(()=>{if(document.hidden)return;if(Date.now()-lastHeartbeat>45000){streamHealthy=false;events?.close();connect();}if(!streamHealthy||Date.now()-lastRefresh>30000)refreshLive();updateSync();},5000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshLive();});
}
async function boot(){
  $('#privacy-link').onclick=privacyDialog;setInterval(tickClocks,1000);
  try{
    const route=location.pathname;
    if(route.startsWith('/admin')){currentView='admin';const module=await import('/admin.js?v=student-id-20261029-r9');await module.mountAdmin(app);return;}
    if(route==='/board'){renderBoard(await api('/api/scoreboard'));startLive();try{await navigator.wakeLock?.request('screen');}catch{}return;}
    state=await api('/api/state');
    if(route==='/hall'){hallScope=state.test_mode?'DAY1':`DAY${state.day_no||1}`;await loadHall();startLive();return;}
    if(state.participant_id){historyItems=(await api('/api/me/submissions')).items;renderInitialParticipant();}else renderLogin();startLive();
  }catch(e){renderConnectionError(e);}
}
boot();
