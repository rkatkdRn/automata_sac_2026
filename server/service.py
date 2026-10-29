"""Game rules and durable application state (R1–R20)."""
from __future__ import annotations
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import db, engine

ITEMS = ('MYCHEW', 'SESAME', 'ACE')
COOKIE = 'blotto_session'
ADMIN_COOKIE = 'blotto_admin'
GUEST_SESSION_HOURS = 12
STUDENT_SESSION_DAYS = 30
CONSENT_VERSION = '2026-10-student-id-v3'
TEST_DAY = db.TEST_DAY
REAL_DAYS = (1, 2, 3)

class GameError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def now(): return db.now()
def stamp(): return now().isoformat(timespec='microseconds')
def uid(): return uuid.uuid4().hex
def dumps(x): return json.dumps(x, ensure_ascii=False, separators=(',', ':'))
def digest(x): return hashlib.sha256(x.encode()).hexdigest()
def need(condition, code, message, status=409):
    if not condition: raise GameError(code, message, status)
def reason_text(payload):
    value = payload.get('reason','')
    need(isinstance(value,str) and 2 <= len(value.strip()) <= 300, 'REASON_REQUIRED','사유를 2~300자로 입력해 주세요.',400)
    return value.strip()
def audit(c, action, reason='', details=None):
    c.execute('INSERT INTO audit_log(id,action,reason,details_json,at) VALUES(?,?,?,?,?)',(uid(),action,reason,dumps(details or {}),stamp()))
def get_day(c):
    return c.one('SELECT * FROM day_config WHERE day_no=?',(int(db.config(c,'active_day','1')),))
def day_public(row):
    row=dict(row); row['sessions']=json.loads(row.pop('sessions_json'))
    row['ever_opened']=bool(row['ever_opened']); row['threshold_applied']=bool(row['threshold_applied']); row['cap_override']=bool(row['cap_override'])
    row['is_test']=is_test(row)
    return row

# Test mode is a fourth operating day isolated from days 1-3: its own pool
# (seeds + test submissions), caps and caches, no physical stock, no permanent locks.
def is_test(day): return day['day_no']==TEST_DAY
def cache_key(name, test): return name+'_test' if test else name
def pool_scope(test, prefix=''):
    return f'({prefix}day_no={TEST_DAY} OR {prefix}is_seed=1)' if test else f'{prefix}day_no!={TEST_DAY}'
def clear_caches(c, tests=(False, True), scores=False):
    """Display caches only by default; score caches stay valid across name changes."""
    for test in tests:
        for key,value in [('board_cache','{}'),('cycle','null'),('cycle_at','')]+([('score_cache','{}')] if scores else []):
            db.set_config(c,cache_key(key,test),value)

def participant(c, token, required=True):
    row = None
    if token:
        row = c.one("""SELECT p.*,i.google_sub AS verified_school_subject
            FROM sessions s JOIN participants p ON p.id=s.participant_id
            LEFT JOIN school_identities i ON i.participant_id=p.id
            WHERE s.token_hash=? AND s.expires_at>? AND (
                (p.kind='STUDENT' AND s.role IN ('STUDENT','SCHOOL')) OR
                (p.kind='GUEST' AND s.role IN ('GUEST','PLAYER') AND s.created_at>?)
            )""",(digest(token),stamp(),(now()-timedelta(hours=GUEST_SESSION_HOURS)).isoformat()))
    if required: need(row is not None,'UNAUTHORIZED','다시 로그인해 주세요.',401)
    return row

def require_admin(c, token):
    row=c.one("SELECT token_hash FROM sessions WHERE token_hash=? AND role='ADMIN' AND expires_at>?",(digest(token or ''),stamp()))
    need(row is not None,'UNAUTHORIZED','운영진 로그인이 필요해요.',401)

def rate_limit(key, limit, window):
    """Commit attempts separately so rejected login transactions cannot undo limits."""
    tick=time.time()
    with db.transaction(write=True) as c:
        c.execute('DELETE FROM rate_limits WHERE window_start<?',(tick-3600,))
        row=c.one('SELECT * FROM rate_limits WHERE key=?',(key,))
        if not row or tick-row['window_start']>=window:
            c.execute('INSERT INTO rate_limits(key,window_start,count) VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start,count=1',(key,tick))
            allowed=True
        else:
            allowed=row['count']<limit
            c.execute('UPDATE rate_limits SET count=count+1 WHERE key=?',(key,))
    need(allowed,'RATE_LIMITED','요청이 많아요. 잠시 후 다시 시도해 주세요.',429)

def display_name(row):
    if row.get('name_hidden'): return '익명 전술가'
    # Students are shown by the nickname they chose after Google login.
    if row.get('kind')=='STUDENT' and row.get('nickname_chosen'): return row['nickname']
    # A legacy student's self-entered name must stay private until Google login
    # and the new consent explicitly authorize displaying the account name.
    if row.get('verified_school_subject') and row.get('real_name'):
        return row['real_name']
    return row['nickname']

def needs_nickname(row):
    return bool(row and row['kind']=='STUDENT' and not row.get('nickname_chosen'))

def valid_nickname(nickname):
    need(isinstance(nickname,str) and 2<=len(nickname.strip())<=10,'INVALID_NICKNAME','닉네임은 2~10자예요.',400)
    nickname=nickname.strip()
    need(not any(ord(ch)<32 for ch in nickname) and not re.search(r'[<>@]|\d{7,}',nickname),'INVALID_NICKNAME','닉네임에 연락처나 특수 기호를 쓰지 마세요.',400)
    # 'school-' is the placeholder prefix for students who haven't chosen yet
    need(not nickname.lower().startswith('school-'),'INVALID_NICKNAME','다른 닉네임을 골라 주세요.',400)
    blocked=['시발','씨발','씨팔','병신','개새끼','fuck','shit']+[v.strip().lower() for v in os.getenv('BLOCKED_NICKNAMES','').split(',') if v.strip()]
    need(not any(v in nickname.lower().replace(' ','') for v in blocked),'INVALID_NICKNAME','다른 닉네임을 골라 주세요.',400)
    return nickname

def threshold(c, day_no):
    rule=pass_rule(c,day_no)
    return rule[1] if rule else None

# Snack pass rule per day. FIXED: win rate >= value_bp. LIVE: at submission, the play is in the
# top value_bp/10000 of the whole pool (seeds included), so the pass rate holds even as the crowd shifts.
def pass_rule(c, day_no):
    row=c.one('SELECT mode,value_bp FROM threshold_history WHERE day_no=? ORDER BY set_at DESC LIMIT 1',(day_no,))
    return (row['mode'],row['value_bp']) if row else None

def top_share_bp(rows, analysis, row_id):
    """Position in the pool as a rounded-up share in 1/10000 (1 = best); ties count in the player's favour."""
    mine=engine.rate_fraction(analysis[row_id])
    better=sum(1 for r in rows if engine.rate_fraction(analysis[r['id']])>mine)
    return -(-(better+1)*10000//len(rows))

def inventory(c):
    result={x:0 for x in ITEMS}
    for row in c.all('SELECT item,SUM(delta) AS n FROM inventory_ledger GROUP BY item'):
        result[row['item']]=row['n']
    return result

def counters(c, test=False):
    row=c.one(f"SELECT COALESCE(SUM(CASE WHEN e.kind='BASIC' OR e.choice='MYCHEW' THEN 1 ELSE 0 END),0) AS mychew_used, COALESCE(SUM(CASE WHEN e.kind='TOP_CHOICE' AND (e.choice IS NULL OR e.choice IN ('SESAME','ACE')) THEN 1 ELSE 0 END),0) AS premium_reserved FROM entitlements e JOIN submissions s ON s.id=e.submission_id WHERE e.status!='VOID' AND {pool_scope(test,'s.')}")
    return row

def pending(c):
    """Physical stock reservations; test entitlements never touch real stock."""
    row=c.one(f"SELECT COALESCE(SUM(CASE WHEN e.kind='BASIC' OR e.choice='MYCHEW' THEN 1 ELSE 0 END),0) AS mychew, COALESCE(SUM(CASE WHEN e.kind='TOP_CHOICE' AND (e.choice IS NULL OR e.choice IN ('SESAME','ACE')) THEN 1 ELSE 0 END),0) AS premium FROM entitlements e JOIN submissions s ON s.id=e.submission_id WHERE e.status='ISSUED' AND {pool_scope(False,'s.')}")
    return row

def schedule(day):
    if is_test(day): return True,None
    t=now(); date=day['date']; sessions=json.loads(day['sessions_json']); local=t.strftime('%H:%M')
    within=t.date().isoformat()==date and any(start<=local<end for start,end in sessions)
    upcoming=[]
    for start,_ in sessions:
        v=datetime.fromisoformat(date+'T'+start).replace(tzinfo=ZoneInfo('Asia/Seoul'))
        if v>t: upcoming.append(v.isoformat())
    return within,min(upcoming) if upcoming else None

def state_in(c, token=None):
    day=get_day(c); person=participant(c,token,False); count=0
    if person: count=c.scalar('SELECT COUNT(*) FROM submissions WHERE participant_id=? AND day_no=?',(person['id'],day['day_no']))
    within,next_open=schedule(day)
    purged=db.config(c,'purged')=='true'
    booth=day['booth_state']
    return {'day_no':day['day_no'],'booth_state':booth,'open_now':bool(booth=='OPEN' and within and not purged),
        'next_open':next_open,'attempts_left':max(0,3-count),'mychew_left':max(0,day['mychew_cap_cum']-counters(c,is_test(day))['mychew_used']),'test_mode':is_test(day),
        'participant_id':person['id'] if person else None,'nickname':display_name(person) if person else None,
        'kind':person['kind'] if person else None,'needs_nickname':needs_nickname(person),'auth_method':('STUDENT_ID' if person['kind']=='STUDENT' else 'GUEST') if person else None,
        'server_time':stamp(),'privacy_contact':os.getenv('PRIVACY_CONTACT','Automata 부스 운영진'),
        'cloud_mode':bool(os.getenv('DATABASE_URL')),'purged':purged,'threshold_bp':threshold(c,day['day_no']),'pass_mode':(pass_rule(c,day['day_no']) or (None,))[0]}

def state(token=None):
    with db.transaction() as c: return state_in(c,token)

def valid_student_id(value):
    match=re.fullmatch(r'([0-9]{2})-?([0-9]{3})',value.strip()) if isinstance(value,str) else None
    need(match is not None,'INVALID_STUDENT_ID','학번은 00-000 형식으로 입력해 주세요.',400)
    return match.group(1)+'-'+match.group(2)

def valid_real_name(value):
    name=' '.join(value.split()) if isinstance(value,str) else ''
    need(2<=len(name)<=30 and not any(ord(ch)<32 for ch in name) and not re.search(r'[<>@0-9]',name),'INVALID_NAME','이름을 2~30자로 정확히 입력해 주세요.',400)
    return name

def same_name(a, b):
    # spacing and letter case never decide who someone is: '홍 길동' is '홍길동'
    return ''.join((a or '').split()).casefold()==''.join((b or '').split()).casefold()

def login(payload, old_token=None):
    need(payload.get('consent') is True,'CONSENT_REQUIRED','개인정보 수집·이용 동의가 필요해요.',400)
    kind=payload.get('kind'); nickname=payload.get('nickname','')
    if kind=='STUDENT': return student_login(payload, old_token)
    need(kind=='GUEST','INVALID_INPUT','외부인 임시 참가를 선택해 주세요.',400)
    nickname=valid_nickname(nickname)
    with db.transaction(write=True) as c:
        need(db.config(c,'purged')!='true','EVENT_ENDED','개인정보 파기가 완료되어 참여가 종료됐어요.')
        person=participant(c,old_token,False)
        # A valid guest cookie, never a nickname, proves continuity. Reopening
        # this form cannot reset the same browser's daily submission allowance.
        existing_guest=bool(person and person['kind']=='GUEST')
        if not existing_guest:
            need(c.one('SELECT id FROM participants WHERE nickname=?',(nickname,)) is None,'NICKNAME_TAKEN','이미 쓰고 있는 닉네임이에요.')
            person={'id':uid(),'kind':'GUEST','nickname':nickname,'name_hidden':0}
            c.execute('INSERT INTO participants(id,kind,nickname,consent_at,consent_version) VALUES(?,?,?,?,?)',(person['id'],'GUEST',nickname,stamp(),CONSENT_VERSION))
        token=secrets.token_urlsafe(32)
        # Keep an existing guest's original expiry; repeated login does not turn
        # a temporary account into an indefinitely extended session.
        previous=c.one('SELECT created_at,expires_at FROM sessions WHERE token_hash=?',(digest(old_token),)) if old_token else None
        created=now();expires=created+timedelta(hours=GUEST_SESSION_HOURS)
        if previous and existing_guest:
            created=datetime.fromisoformat(previous['created_at'])
            expires=min(datetime.fromisoformat(previous['expires_at']),created+timedelta(hours=GUEST_SESSION_HOURS))
        c.execute('INSERT INTO sessions(token_hash,participant_id,role,expires_at,created_at) VALUES(?,?,?,?,?)',(digest(token),person['id'],'GUEST',expires.isoformat(),created.isoformat()))
        if old_token:c.execute('DELETE FROM sessions WHERE token_hash=?',(digest(old_token),))
        return session_public(c,person),token


def session_public(c,person):
    day=get_day(c)
    attempts=c.scalar('SELECT COUNT(*) FROM submissions WHERE participant_id=? AND day_no=?',(person['id'],day['day_no']))
    return {'participant_id':person['id'],'nickname':display_name(person),'kind':person['kind'],'needs_nickname':needs_nickname(person),
            'auth_method':'STUDENT_ID' if person['kind']=='STUDENT' else 'GUEST','attempts_left':max(0,3-attempts)}


# In-school students sign in with 학번, 이름 and a nickname. The same 학번 and 이름 continue the
# same record on any device; a different name never takes over a 학번 (staff sort that out).
def student_login(payload, old_token=None):
    student_id=valid_student_id(payload.get('student_id'))
    name=valid_real_name(payload.get('name'))
    with db.transaction(write=True) as c:
        need(db.config(c,'purged')!='true','EVENT_ENDED','개인정보 파기가 완료되어 참여가 종료됐어요.')
        person=c.one('SELECT * FROM participants WHERE student_id=?',(student_id,))
        returning=person is not None
        if returning:
            need(person['kind']=='STUDENT' and same_name(person['real_name'],name),'STUDENT_ID_CONFLICT','학번이 다른 이름으로 등록되어 있어요. 스태프에게 말씀해 주세요.')
        # a returning student keeps the nickname already on the boards; the typed one is ignored
        if not returning or not person.get('nickname_chosen'):
            nickname=valid_nickname(payload.get('nickname',''))
            need(c.one('SELECT id FROM participants WHERE nickname=? AND id!=?',(nickname,person['id'] if returning else '')) is None,'NICKNAME_TAKEN','이미 쓰고 있는 닉네임이에요.')
            if returning:
                c.execute('UPDATE participants SET nickname=?,nickname_chosen=1 WHERE id=?',(nickname,person['id']))
                person.update(nickname=nickname,nickname_chosen=1)
                clear_caches(c)
            else:
                person={'id':uid(),'kind':'STUDENT','student_id':student_id,'real_name':name,'nickname':nickname,'nickname_chosen':1,'name_hidden':0}
                c.execute('INSERT INTO participants(id,kind,student_id,real_name,nickname,nickname_chosen,consent_at,consent_version) VALUES(?,?,?,?,?,?,?,?)',
                          (person['id'],'STUDENT',student_id,name,nickname,1,stamp(),CONSENT_VERSION))
        if returning:
            c.execute('UPDATE participants SET consent_at=?,consent_version=? WHERE id=?',(stamp(),CONSENT_VERSION,person['id']))
        if old_token:c.execute('DELETE FROM sessions WHERE token_hash=?',(digest(old_token),))
        token=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions(token_hash,participant_id,role,expires_at,created_at) VALUES(?,?,?,?,?)',
                  (digest(token),person['id'],'STUDENT',(now()+timedelta(days=STUDENT_SESSION_DAYS)).isoformat(),stamp()))
        audit(c,'STUDENT_LOGIN',details={'participant_id':person['id'],'returning':returning});db.bump(c)
        return session_public(c,person),token


def set_nickname(token,payload):
    nickname=valid_nickname(payload.get('nickname',''))
    with db.transaction(write=True) as c:
        need(db.config(c,'purged')!='true','EVENT_ENDED','개인정보 파기가 완료되어 참여가 종료됐어요.')
        me=participant(c,token)
        need(me['kind']=='STUDENT','FORBIDDEN','외부인 닉네임은 참가 등록 때 정해요.',403)
        # chosen once, so the name on the leaderboard and hall of fame stays put
        need(not me.get('nickname_chosen'),'NICKNAME_LOCKED','닉네임은 한 번만 정할 수 있어요.',409)
        need(c.one('SELECT id FROM participants WHERE nickname=? AND id!=?',(nickname,me['id'])) is None,'NICKNAME_TAKEN','이미 쓰고 있는 닉네임이에요.')
        c.execute('UPDATE participants SET nickname=?,nickname_chosen=1 WHERE id=?',(nickname,me['id']))
        me.update(nickname=nickname,nickname_chosen=1)
        clear_caches(c);audit(c,'NICKNAME_SET',details={'participant_id':me['id']});db.bump(c)
        return session_public(c,me)

def logout(token):
    if token:
        with db.transaction(write=True) as c:
            c.execute('DELETE FROM sessions WHERE token_hash=?',(digest(token),))
    return {'ok':True}

def admin_login(pin, ip):
    key='admin:'+digest(ip)
    configured=os.getenv('ADMIN_PIN','')
    need(len(configured)>=16,'SETUP_REQUIRED','서버의 ADMIN_PIN을 16자 이상으로 설정해 주세요.',503)
    correct=isinstance(pin,str) and hmac.compare_digest(pin,configured)
    if not correct:
        rate_limit(key,5,60)
        raise GameError('UNAUTHORIZED','비밀번호를 확인해 주세요.',401)
    with db.transaction() as c:
        previous=c.one('SELECT window_start,count FROM rate_limits WHERE key=?',(key,))
        need(not previous or time.time()-previous['window_start']>=60 or previous['count']<5,'RATE_LIMITED','로그인 시도가 많아요. 잠시 후 다시 시도해 주세요.',429)
    token=secrets.token_urlsafe(32)
    with db.transaction(write=True) as c:
        c.execute('INSERT INTO sessions(token_hash,participant_id,role,expires_at,created_at) VALUES(?,NULL,?,?,?)',(digest(token),'ADMIN',(now()+timedelta(hours=12)).isoformat(),stamp()))
        audit(c,'ADMIN_LOGIN')
    return {'ok':True},token

def pool_rows(c, for_submission=False, test=False):
    if for_submission:
        # Submission responses need only a rank number and an anonymous nemesis.
        # Display names and historical snapshot scores are loaded by board reads.
        rows=c.all(f'SELECT id,participant_id,day_no,l1,l2,l3,l4,l5,is_seed,is_ranked,submitted_at FROM submissions WHERE {pool_scope(test)} ORDER BY submitted_at,id')
    else:
        rows=c.all(f'SELECT s.id,s.participant_id,s.day_no,s.l1,s.l2,s.l3,s.l4,s.l5,s.is_seed,s.is_ranked,s.points,s.opponents,s.submitted_at,p.nickname,p.name_hidden,p.real_name,p.kind,p.nickname_chosen,i.google_sub AS verified_school_subject FROM submissions s LEFT JOIN participants p ON p.id=s.participant_id LEFT JOIN school_identities i ON i.participant_id=p.id WHERE {pool_scope(test,"s.")} ORDER BY s.submitted_at,s.id')
    for row in rows:
        row['strategy']=[row['l1'],row['l2'],row['l3'],row['l4'],row['l5']]
        if row['participant_id'] is None: row['nickname']='시드'
        elif not for_submission: row['nickname']=display_name(row)
        row.pop('real_name',None);row.pop('verified_school_subject',None);row.pop('kind',None);row.pop('nickname_chosen',None)
    return rows

def analysis_in(c, rows, test=False):
    analysis=json.loads(db.config(c,cache_key('score_cache',test),'{}'))
    if set(analysis)!=set(r['id'] for r in rows):
        analysis=engine.analyze_pool(rows)
    return analysis

def incremental_analysis(rows, analysis, new):
    pts=0; opponents=0
    values=engine.score_vector(new['strategy'],[row['strategy'] for row in rows])
    for row, raw_value in zip(rows,values):
        if row['participant_id'] is not None and row['participant_id']==new['participant_id']: continue
        value=int(raw_value)
        pts+=value; opponents+=1
        old=analysis[row['id']]; old['points']+=2-value; old['opponents']+=1
        old['win_rate']=old['points']/(2*old['opponents'])
    analysis[new['id']]={'points':pts,'opponents':opponents,'win_rate':pts/(2*opponents) if opponents else 0.0}
    return analysis

def entitlement_public(e):
    if not e: return None
    return {'entitlement_id':e['id'],'code':e['code'],'kind':e['kind'],'premium_exhausted':bool(e['premium_exhausted']),
        'choices':list(ITEMS) if e['kind']=='TOP_CHOICE' else ['MYCHEW'],'status':e['status'],'choice':e['choice'],'claimed_at':e['claimed_at']}

def close_if_cap(c,day):
    if day['cap_override'] or day['booth_state']!='OPEN': return
    count=counters(c,is_test(day))
    hit=count['mychew_used']>=day['mychew_cap_cum'] or (day['close_rule']=='EITHER' and count['premium_reserved']>=day['premium_cap_cum'])
    if hit: c.execute("UPDATE day_config SET booth_state='CAP_REACHED',cap_closed_at=? WHERE day_no=?",(stamp(),day['day_no']))

def submit(token,payload):
    strategy=payload.get('strategy'); request_id=payload.get('request_id','')
    need(engine.validate_strategy(strategy),'INVALID_STRATEGY','0 이상인 정수 5개의 합을 101로 맞춰 주세요.',400)
    need(isinstance(request_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{8,100}',request_id) is not None,'INVALID_REQUEST_ID','제출 식별값이 잘못됐어요. 화면을 새로 열어 주세요.',400)
    with db.transaction(write=True) as c:
        me=participant(c,token)
        need(not needs_nickname(me),'NICKNAME_REQUIRED','먼저 닉네임을 정해 주세요.',409)
        saved=c.one('SELECT participant_id,response_json,l1,l2,l3,l4,l5 FROM submissions WHERE request_id=?',(request_id,))
        if saved:
            need(saved['participant_id']==me['id'],'FORBIDDEN','다른 참가자의 제출 요청이에요.',403)
            need([saved['l'+str(i)] for i in range(1,6)]==strategy,'REQUEST_ID_CONFLICT','같은 제출 번호의 배분을 바꿀 수 없어요.')
            return json.loads(saved['response_json'])
        day=get_day(c); st=state_in(c,token); test=is_test(day)
        expected=payload.get('expected_day_no',day['day_no'])
        need(type(expected) is int and expected==day['day_no'],'DAY_CHANGED','운영일이 바뀌었어요. 배분을 확인하고 새로 제출해 주세요.')
        need(day['booth_state']!='CAP_REACHED','CAP_REACHED','오늘 간식 상한에 도달했어요.')
        need(st['open_now'],'BOOTH_CLOSED','지금은 제출할 수 없어요. 운영 시간을 확인해 주세요.')
        done=c.scalar('SELECT COUNT(*) FROM submissions WHERE participant_id=? AND day_no=?',(me['id'],day['day_no']))
        need(done<3,'LIMIT_REACHED','오늘의 도전 3회를 모두 사용했어요.')
        rows=pool_rows(c,for_submission=True,test=test); analysis=analysis_in(c,rows,test)
        opponent_rows=[r for r in rows if r['participant_id']!=me['id']]
        # Test mode may run with any number of seeds, even none.
        need(test or len(opponent_rows)>=50,'POOL_TOO_SMALL','시드 준비 중이에요. 잠시 후 다시 와 주세요.',503)
        points=engine.score(strategy,[r['strategy'] for r in opponent_rows]); n=len(opponent_rows)
        rule=pass_rule(c,day['day_no']); need(rule is not None,'SETUP_REQUIRED','임계값 설정이 필요해요.',503); mode,bp=rule
        new={'id':uid(),'participant_id':me['id'],'day_no':day['day_no'],'strategy':strategy,'is_seed':0,'is_ranked':1,'nickname':display_name(me),'submitted_at':stamp()}
        analysis=incremental_analysis(rows,analysis,new); allrows=rows+[new]
        top_bp=top_share_bp(allrows,analysis,new['id'])
        reward=None
        if done==0:
            qualified=engine.qualified(points,n,bp) if mode=='FIXED' else n>0 and top_bp<=bp
            count=counters(c,test); stock=inventory(c); reserved=pending(c)
            premium_available=count['premium_reserved']<day['premium_cap_cum'] and (test or stock['SESAME']+stock['ACE']>reserved['premium'])
            kind='TOP_CHOICE' if qualified and premium_available else 'BASIC'
            if kind=='BASIC':
                need(count['mychew_used']<day['mychew_cap_cum'] and (test or stock['MYCHEW']>reserved['mychew']),'CAP_REACHED','지급 가능한 마이쮸가 없어요. 스태프에게 문의해 주세요.')
            reward={'id':uid(),'code':''.join(secrets.choice('23456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(6)),'kind':kind,'premium_exhausted':int(qualified and not premium_available),'choice':None,'status':'ISSUED','claimed_at':None}
            while c.one('SELECT id FROM entitlements WHERE code=?',(reward['code'],)):
                reward['code']=''.join(secrets.choice('23456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(6))
        c.execute('INSERT INTO submissions(id,request_id,participant_id,day_no,attempt_no,l1,l2,l3,l4,l5,is_seed,is_ranked,points,opponents,threshold_bp_used,top_bp,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,1,?,?,?,?,?)',
            (new['id'],request_id,me['id'],day['day_no'],done+1,*strategy,points,n,bp,top_bp,new['submitted_at']))
        if reward:
            c.execute('INSERT INTO entitlements(id,submission_id,code,kind,premium_exhausted,issued_at) VALUES(?,?,?,?,?,?)',(reward['id'],new['id'],reward['code'],reward['kind'],reward['premium_exhausted'],stamp()))
        ranking=engine.rank_rows(allrows,analysis=analysis)
        myrank=next((r['rank'] for r in ranking if r['participant_id']==me['id']),None)
        resp={'id':new['id'],'day_no':day['day_no'],'attempt_no':done+1,'attempts_left':2-done,'strategy':strategy,'win_rate':points/(2*n) if n else 0.0,'points':points,'opponents':n,'threshold_bp_used':bp,'pass_mode':mode,'top_bp':top_bp,'submitted_at':new['submitted_at'],
            'rank_estimate':{'rank':myrank,'of':len(ranking),'is_provisional':True},'reward':entitlement_public(reward),
            'nemesis':engine.nemesis(strategy,allrows,participant_id=me['id'],analysis=analysis),'pool_size':len(allrows)}
        c.execute('UPDATE submissions SET response_json=? WHERE id=?',(dumps(resp),new['id']))
        db.set_config(c,cache_key('score_cache',test),dumps(analysis)); close_if_cap(c,day); db.bump(c)
        return resp

def history(token):
    with db.transaction() as c:
        me=participant(c,token); rows=c.all('SELECT id,response_json FROM submissions WHERE participant_id=? ORDER BY submitted_at DESC',(me['id'],))
        items=[]
        for row in rows:
            data=json.loads(row['response_json']); data['reward']=entitlement_public(c.one('SELECT * FROM entitlements WHERE submission_id=?',(row['id'],)))
            items.append(data)
        return {'items':items}

def owned_entitlement(c,token,entitlement_id):
    me=participant(c,token)
    e=c.one('SELECT e.*,s.participant_id,s.day_no FROM entitlements e JOIN submissions s ON s.id=e.submission_id WHERE e.id=?',(entitlement_id,))
    need(e is not None and e['participant_id']==me['id'],'FORBIDDEN','내 수령권만 사용할 수 있어요.',403)
    need(e['status']!='CLAIMED','ALREADY_CLAIMED','이미 수령 완료된 간식이에요.')
    need(e['status']=='ISSUED','ENTITLEMENT_VOID','무효 처리된 수령권이에요.')
    return e


def select_in(c,e,choice):
    """Reserve a concrete item, including choice switches, under the game lock.

    Unchosen TOP_CHOICE reserves one generic premium unit. A selected premium
    item reserves that physical item too. Excluding this entitlement from the
    pending-item check makes retrying the same selection naturally idempotent.
    """
    need(choice in ITEMS and (e['kind']=='TOP_CHOICE' or choice=='MYCHEW'),'INVALID_CHOICE','간식을 골라 주세요.',400)
    # Caps follow the entitlement's own mode, even if the admin has since switched days.
    test=e['day_no']==TEST_DAY
    day=get_day(c);caps=day
    if test!=is_test(day):
        caps=c.one('SELECT * FROM day_config WHERE day_no=?',(TEST_DAY if test else int(db.config(c,'real_day','1')),))
    count=counters(c,test);stock=inventory(c)
    if not test:
        specific=c.scalar(f"SELECT COUNT(*) FROM entitlements e JOIN submissions s ON s.id=e.submission_id WHERE e.status='ISSUED' AND e.id!=? AND (e.choice=? OR (e.kind='BASIC' AND ?='MYCHEW')) AND {pool_scope(False,'s.')}",(e['id'],choice,choice))
        need(stock[choice]>specific,'OUT_OF_STOCK_CHOICE','고른 간식이 소진되었거나 예약 중이에요. 다른 간식을 골라 주세요.')
    old_group='MYCHEW' if e['kind']=='BASIC' or e['choice']=='MYCHEW' else 'PREMIUM'
    new_group='MYCHEW' if choice=='MYCHEW' else 'PREMIUM'
    if new_group=='MYCHEW':
        additional=int(old_group!='MYCHEW')
        need(count['mychew_used']+additional<=caps['mychew_cap_cum'],'OUT_OF_STOCK_CHOICE','마이쮸 상한에 도달했어요. 다른 간식을 골라 주세요.')
    else:
        additional=int(old_group!='PREMIUM')
        need(count['premium_reserved']+additional<=caps['premium_cap_cum'],'OUT_OF_STOCK_CHOICE','고급 간식 상한에 도달했어요. 마이쮸를 골라 주세요.')
        if not test:
            other_premium=c.scalar(f"SELECT COUNT(*) FROM entitlements e JOIN submissions s ON s.id=e.submission_id WHERE e.status='ISSUED' AND e.id!=? AND e.kind='TOP_CHOICE' AND (e.choice IS NULL OR e.choice IN ('SESAME','ACE')) AND {pool_scope(False,'s.')}",(e['id'],))
            need(stock['SESAME']+stock['ACE']>other_premium,'OUT_OF_STOCK_CHOICE','고급 간식 여유가 없어요. 마이쮸를 골라 주세요.')
    if e['choice']!=choice:
        c.execute('UPDATE entitlements SET choice=? WHERE id=?',(choice,e['id']))
        e['choice']=choice
    return day


def select_choice(token,entitlement_id,payload):
    with db.transaction(write=True) as c:
        e=owned_entitlement(c,token,entitlement_id)
        day=select_in(c,e,payload.get('choice'))
        close_if_cap(c,day);db.bump(c)
        return entitlement_public(e)


def claim(token,entitlement_id,payload):
    with db.transaction(write=True) as c:
        e=owned_entitlement(c,token,entitlement_id)
        choice=payload.get('choice',e['choice'] or ('MYCHEW' if e['kind']=='BASIC' else None))
        day=select_in(c,e,choice)
        claimed_at=stamp()
        c.execute("UPDATE entitlements SET status='CLAIMED',choice=?,claimed_at=? WHERE id=?",(choice,claimed_at,entitlement_id))
        if e['day_no']!=TEST_DAY:
            c.execute('INSERT INTO inventory_ledger(id,item,delta,reason,entitlement_id,at) VALUES(?,?,-1,?,?,?)',(uid(),choice,'CLAIM',entitlement_id,claimed_at))
        close_if_cap(c,day);db.bump(c)
        return {'status':'CLAIMED','choice':choice,'claimed_at':claimed_at,'code':e['code'],'entitlement_id':e['id']}

def board():
    with db.transaction() as c:
        version=int(db.config(c,'version','0')); test=is_test(get_day(c))
        cached=json.loads(db.config(c,cache_key('board_cache',test),'{}'))
        if cached.get('version')==version:
            return cached
    with db.transaction(write=True) as c:
        version=int(db.config(c,'version','0')); day=get_day(c); test=is_test(day)
        cached=json.loads(db.config(c,cache_key('board_cache',test),'{}'))
        if cached.get('version')==version:
            return cached
        rows=pool_rows(c,test=test); analysis=analysis_in(c,rows,test); ranked=engine.rank_rows(rows,analysis=analysis)
        cycle=json.loads(db.config(c,cache_key('cycle',test),'null')); cycle_at=db.config(c,cache_key('cycle_at',test),'')
        if not cycle or not cycle_at or (now()-datetime.fromisoformat(cycle_at)).total_seconds()>=60:
            cycle=engine.find_cycle(ranked[:30]); db.set_config(c,cache_key('cycle',test),dumps(cycle)); db.set_config(c,cache_key('cycle_at',test),stamp())
        if cycle:
            names={r['participant_id']:r['nickname'] for r in ranked}
            for row in cycle: row['nickname']=names.get(row['participant_id'],'익명 전술가')
        recent=[]
        for row in reversed(rows):
            if row['is_ranked'] and row['participant_id'] is not None:
                recent.append({'nickname':row['nickname'],'strategy':row['strategy'],'win_rate':row['points']/(2*row['opponents']) if row['opponents'] else 0,'submitted_at':row['submitted_at']})
                if len(recent)==6: break
        data={'rankings':ranked[:10],'cycle':cycle,'recent':recent,'day_no':day['day_no'],'booth_state':day['booth_state'],
            'mychew_left':max(0,day['mychew_cap_cum']-counters(c,test)['mychew_used']),'pool_size':len(rows),'updated_at':stamp(),'version':int(db.config(c,'version','0')),'test_mode':test}
        db.set_config(c,cache_key('board_cache',test),dumps(data))
        return data

def hall_in(c,scope):
    need(scope in ('DAY1','DAY2','DAY3','OVERALL'),'INVALID_SCOPE','순위 범위를 확인해 주세요.',400)
    row=c.one('SELECT * FROM rank_freeze WHERE scope=?',(scope,))
    if not row: return {'scope':scope,'frozen':False,'frozen_at':None,'rankings':[]}
    rankings=json.loads(row['rankings_json'])
    people={r['id']:display_name(r) for r in c.all('SELECT p.id,p.nickname,p.name_hidden,p.real_name,p.kind,p.nickname_chosen,i.google_sub AS verified_school_subject FROM participants p LEFT JOIN school_identities i ON i.participant_id=p.id')}
    for rank in rankings: rank['nickname']=people.get(rank['participant_id'],'익명 전술가')
    return {'scope':scope,'frozen':True,'frozen_at':row['frozen_at'],'rankings':rankings}

def hall(scope):
    with db.transaction() as c: return hall_in(c,scope)

def dashboard_in(c):
    day=get_day(c); test=is_test(day)
    return {'day_no':day['day_no'],'day':day_public(day),'days':[day_public(r) for r in c.all('SELECT * FROM day_config ORDER BY day_no')],
        'state':state_in(c),'counters':counters(c,test),'inventory':inventory(c),'pool_size':c.scalar(f'SELECT COUNT(*) FROM submissions WHERE {pool_scope(test)}'),
        'seed_count':c.scalar('SELECT COUNT(*) FROM submissions WHERE is_seed=1'),'participant_count':c.scalar('SELECT COUNT(*) FROM participants'),
        'submission_count':c.scalar(f'SELECT COUNT(*) FROM submissions WHERE is_seed=0 AND {pool_scope(test)}'),
        'today_submissions':c.scalar('SELECT COUNT(*) FROM submissions WHERE is_seed=0 AND day_no=?',(day['day_no'],)),
        'threshold_bp':threshold(c,day['day_no']),'pass_mode':(pass_rule(c,day['day_no']) or (None,))[0],'threshold_locked':not test and bool(day['ever_opened'] or day['threshold_applied']),
        'threshold_applied':bool(day['threshold_applied']),'purged':db.config(c,'purged')=='true','db_mode':'postgresql' if c.postgres else 'sqlite',
        'test_mode':test,'test_day':TEST_DAY,'test_submissions':c.scalar('SELECT COUNT(*) FROM submissions WHERE day_no=?',(TEST_DAY,))}

def dashboard(token):
    with db.transaction() as c: require_admin(c,token); return dashboard_in(c)

def choose_day(token,payload):
    value=payload.get('day_no')
    need(type(value) is int and value in REAL_DAYS+(TEST_DAY,),'INVALID_DAY','운영일은 1~3일차 또는 테스트 모드예요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token); current=get_day(c)
        need(current['booth_state'] not in ('OPEN','PAUSED'),'DAY_BUSY','현재 운영일을 마감한 뒤 전환해 주세요.')
        # Visiting test mode never counts as moving forward: compare with the last real day.
        last_real=int(db.config(c,'real_day','1')) if is_test(current) else current['day_no']
        if value!=TEST_DAY:
            need(value>=last_real or not c.scalar(f'SELECT COUNT(*) FROM submissions WHERE is_seed=0 AND {pool_scope(False)}'),'INVALID_DAY','제출 기록이 있으면 지난 운영일로 돌아갈 수 없어요.')
            last_real=value
        db.set_config(c,'real_day',last_real)
        db.set_config(c,'active_day',value); audit(c,'DAY_SELECTED',details={'day_no':value}); db.bump(c)
        return dashboard_in(c)

def configure_day(token,payload):
    reason=reason_text(payload); day_no=payload.get('day_no'); date=payload.get('date'); sessions=payload.get('sessions')
    need(type(day_no) is int and day_no in REAL_DAYS+(TEST_DAY,),'INVALID_DAY','운영일을 확인해 주세요.',400)
    try:
        parsed=datetime.strptime(date,'%Y-%m-%d').date(); need(parsed.isoformat()==date,'INVALID_INPUT','날짜를 확인해 주세요.',400)
    except (ValueError,TypeError): raise GameError('INVALID_INPUT','날짜를 확인해 주세요.',400)
    need(isinstance(sessions,list) and 1<=len(sessions)<=4,'INVALID_INPUT','운영 시간 구간을 입력해 주세요.',400)
    previous=''
    for pair in sessions:
        need(isinstance(pair,list) and len(pair)==2 and all(isinstance(x,str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',x) for x in pair),'INVALID_INPUT','시간은 HH:MM 형식이에요.',400)
        need(previous<=pair[0]<pair[1],'INVALID_INPUT','시간 구간은 겹치지 않고 순서대로 입력해야 해요.',400);previous=pair[1]
    chew=payload.get('mychew_cap_cum'); premium=payload.get('premium_cap_cum'); rule=payload.get('close_rule','EITHER')
    need(type(chew) is int and 1<=chew<=450 and type(premium) is int and 1<=premium<=95,'INVALID_INPUT','상한은 마이쮸 1~450, 고급 1~95예요.',400)
    need(rule in ('EITHER','PREMIUM_SOFT'),'INVALID_INPUT','마감 규칙을 확인해 주세요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token); day=c.one('SELECT * FROM day_config WHERE day_no=?',(day_no,))
        need(is_test(day) or not day['ever_opened'],'CONFIG_LOCKED','개장한 운영일의 설정은 바꿀 수 없어요.')
        other=[] if is_test(day) else c.all('SELECT * FROM day_config WHERE day_no!=? AND day_no!=?',(day_no,TEST_DAY))
        for d in other:
            if d['day_no']<day_no: need(d['mychew_cap_cum']<=chew and d['premium_cap_cum']<=premium,'INVALID_CAP','누적 상한은 전날보다 작을 수 없어요.',400)
            else: need(d['mychew_cap_cum']>=chew and d['premium_cap_cum']>=premium,'INVALID_CAP','누적 상한은 다음 날보다 클 수 없어요.',400)
        c.execute('UPDATE day_config SET date=?,sessions_json=?,mychew_cap_cum=?,premium_cap_cum=?,close_rule=? WHERE day_no=?',(date,dumps(sessions),chew,premium,rule,day_no))
        audit(c,'DAY_CONFIG',reason,{'day_no':day_no,'date':date,'sessions':sessions,'mychew_cap_cum':chew,'premium_cap_cum':premium,'close_rule':rule}); db.bump(c)
        return dashboard_in(c)

def booth(token,payload):
    action=payload.get('action'); need(action in ('OPEN','PAUSE','CLOSE','REOPEN'),'INVALID_ACTION','운영 동작을 확인해 주세요.',400)
    reason=reason_text(payload) if action=='REOPEN' else str(payload.get('reason',''))[:300]
    with db.transaction(write=True) as c:
        require_admin(c,token); day=get_day(c)
        need(db.config(c,'purged')!='true' or action=='CLOSE','EVENT_ENDED','파기 후에는 재개장할 수 없어요.')
        if action in ('OPEN','REOPEN'):
            need(not day['frozen_at'],'DAY_FROZEN','확정된 운영일은 재개장할 수 없어요.')
            need(is_test(day) or c.scalar('SELECT COUNT(*) FROM submissions WHERE is_seed=1')>=50,'POOL_TOO_SMALL','부원이 작성한 시드 50개 이상이 필요해요.',503)
            need(day['threshold_applied'],'THRESHOLD_REQUIRED','개장 전에 임계값을 적용해 주세요.')
            stock=inventory(c)
            has_stock=is_test(day) or ((stock['MYCHEW']>0 or stock['SESAME']+stock['ACE']>0) if action=='REOPEN' else (stock['MYCHEW']>0 and stock['SESAME']+stock['ACE']>0))
            need(has_stock,'STOCK_REQUIRED','지급 가능한 실물 재고를 입력해 주세요.')
            if action=='OPEN': need(day['booth_state'] in ('CLOSED','PAUSED','OPEN'),'REOPEN_REQUIRED','재개방 버튼과 사유를 사용해 주세요.')
            c.execute("UPDATE day_config SET booth_state='OPEN',ever_opened=1,cap_override=? WHERE day_no=?",(int(action=='REOPEN') or day['cap_override'],day['day_no']))
            if action=='OPEN': close_if_cap(c,get_day(c))
        elif action=='PAUSE':
            need(day['booth_state']=='OPEN','BOOTH_CLOSED','운영 중일 때 일시정지할 수 있어요.')
            c.execute("UPDATE day_config SET booth_state='PAUSED' WHERE day_no=?",(day['day_no'],))
        else: c.execute("UPDATE day_config SET booth_state='DONE' WHERE day_no=?",(day['day_no'],))
        audit(c,'BOOTH_'+action,reason,{'day_no':day['day_no']}); db.bump(c)
        return dashboard_in(c)

def suggest(token):
    with db.transaction() as c:
        require_admin(c,token); test=is_test(get_day(c)); rows=pool_rows(c,test=test)
        need(len(rows)>=2,'POOL_TOO_SMALL','추천값 계산에는 전략 2개 이상이 필요해요.',503)
        return {'value_bp':engine.threshold_suggestion(rows,analysis=analysis_in(c,rows,test)),'percentile':85,'pool_size':len(rows)}

def set_threshold(token,payload):
    value=payload.get('value_bp'); reason=reason_text(payload); source=payload.get('source','MANUAL'); mode=payload.get('mode','LIVE')
    need(mode in ('LIVE','FIXED'),'INVALID_INPUT','판정 방식을 확인해 주세요.',400)
    need(type(value) is int and 0<=value<=10000,'INVALID_INPUT','임계값은 0~10000 만분율 정수예요.',400)
    need(source in ('MANUAL','SUGGESTION'),'INVALID_INPUT','설정 출처를 확인해 주세요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token); day=get_day(c)
        need(is_test(day) or (not day['ever_opened'] and day['booth_state']=='CLOSED' and not day['threshold_applied']),'THRESHOLD_LOCKED','임계값은 개장 전 한 번만 적용할 수 있어요.')
        c.execute('INSERT INTO threshold_history(id,day_no,value_bp,source,reason,set_by,set_at,mode) VALUES(?,?,?,?,?,?,?,?)',(uid(),day['day_no'],value,source,reason,'ADMIN',stamp(),mode))
        c.execute('UPDATE day_config SET threshold_applied=1 WHERE day_no=?',(day['day_no'],))
        audit(c,'THRESHOLD_SET',reason,{'mode':mode,'value_bp':value,'day_no':day['day_no'],'source':source}); db.bump(c)
        return {'mode':mode,'value_bp':value}

def seed_list(c):
    rows=c.all('SELECT id,l1,l2,l3,l4,l5,tag,author,submitted_at FROM submissions WHERE is_seed=1 ORDER BY submitted_at,id')
    return [{'id':r['id'],'strategy':[r['l'+str(i)] for i in range(1,6)],'tag':r['tag'],'author':r['author'],'submitted_at':r['submitted_at']} for r in rows]

def seeds(token):
    with db.transaction() as c:
        require_admin(c,token); items=seed_list(c); return {'items':items,'count':len(items)}

def add_seeds(token,payload,single=False):
    items=[payload] if single else payload.get('items')
    if not single and 'csv' in payload:
        need(isinstance(payload['csv'],str) and len(payload['csv'])<=1000000,'INVALID_INPUT','CSV 크기를 확인해 주세요.',400)
        try:
            items=[{'strategy':[int(r['l'+str(i)]) for i in range(1,6)],'tag':r.get('tag',''),'author':r.get('author','')} for r in csv.DictReader(io.StringIO(payload['csv'].lstrip('\ufeff')))]
        except (ValueError,KeyError,TypeError): raise GameError('INVALID_INPUT','CSV 열은 l1,l2,l3,l4,l5,tag,author여야 해요.',400)
    need(isinstance(items,list) and 1<=len(items)<=500,'INVALID_INPUT','시드는 한 번에 1~500개를 입력해 주세요.',400)
    for item in items:
        need(isinstance(item,dict) and engine.validate_strategy(item.get('strategy')),'INVALID_STRATEGY','시드 각 행의 합을 101로 맞춰 주세요.',400)
        need(isinstance(item.get('tag',''),str) and len(item.get('tag',''))<=40 and isinstance(item.get('author',''),str) and len(item.get('author',''))<=40,'INVALID_INPUT','유형과 작성자는 40자 이내예요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token)
        need(not c.scalar('SELECT COUNT(*) FROM day_config WHERE ever_opened=1 AND day_no!=?',(TEST_DAY,)),'SEEDS_LOCKED','시드는 행사 최초 개장 전에만 넣을 수 있어요.')
        last_id=None
        for item in items:
            last_id=uid();c.execute('INSERT INTO submissions(id,request_id,participant_id,day_no,attempt_no,l1,l2,l3,l4,l5,is_seed,is_ranked,submitted_at,tag,author) VALUES(?,?,NULL,0,NULL,?,?,?,?,?,1,0,?,?,?)',(last_id,'seed-'+last_id,*item['strategy'],stamp(),item.get('tag',''),item.get('author','')))
        rows=pool_rows(c);db.set_config(c,'score_cache',dumps(engine.analyze_pool(rows)));audit(c,'SEEDS_ADDED',details={'count':len(items)});db.bump(c)
        count=c.scalar('SELECT COUNT(*) FROM submissions WHERE is_seed=1')
        return {'id':last_id,'count':count} if single else {'imported':len(items),'count':count}

def claims(token,status=None):
    with db.transaction() as c:
        require_admin(c,token); day=get_day(c)
        rows=c.all('SELECT e.*,s.participant_id,s.day_no,p.nickname,p.name_hidden,p.real_name,p.kind AS p_kind,p.nickname_chosen,i.google_sub AS verified_school_subject FROM entitlements e JOIN submissions s ON s.id=e.submission_id JOIN participants p ON p.id=s.participant_id LEFT JOIN school_identities i ON i.participant_id=p.id WHERE s.day_no=? ORDER BY e.issued_at DESC',(day['day_no'],))
        counts={'ISSUED':0,'CLAIMED':0,'VOID':0}; by_item={x:0 for x in ITEMS}; items=[]
        for row in rows:
            counts[row['status']]+=1
            if row['status']=='CLAIMED': by_item[row['choice']]+=1
            row['nickname']=display_name({**row,'kind':row.pop('p_kind')});row.pop('nickname_chosen',None);row.pop('name_hidden',None);row.pop('real_name',None);row.pop('verified_school_subject',None);row['premium_exhausted']=bool(row['premium_exhausted'])
            if not status or row['status']==status: items.append(row)
        return {'items':items,'counts':counts,'by_item':by_item}

def stock_in(c):
    return {'inventory':inventory(c),'counters':counters(c),'ledger':c.all('SELECT * FROM inventory_ledger ORDER BY at DESC LIMIT 300')}

def stock(token):
    with db.transaction() as c: require_admin(c,token);return stock_in(c)

def stock_count(token,payload):
    item=payload.get('item');count=payload.get('count');reason=reason_text(payload)
    need(item in ITEMS and type(count) is int and 0<=count<=10000,'INVALID_INPUT','품목과 0 이상의 정수 재고를 입력해 주세요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token);current=inventory(c)[item]
        c.execute('INSERT INTO inventory_ledger(id,item,delta,reason,at) VALUES(?,?,?,?,?)',(uid(),item,count-current,'COUNT_ADJUST',stamp()))
        audit(c,'INVENTORY_COUNT',reason,{'item':item,'before':current,'after':count});db.bump(c)
        return stock_in(c)

def void_entitlement(token,entitlement_id,payload):
    reason=reason_text(payload);return_stock=payload.get('return_stock',False)
    need(type(return_stock) is bool,'INVALID_INPUT','실물 반환 여부를 확인해 주세요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token);e=c.one('SELECT e.*,s.day_no FROM entitlements e JOIN submissions s ON s.id=e.submission_id WHERE e.id=?',(entitlement_id,))
        need(e is not None,'NOT_FOUND','수령권을 찾을 수 없어요.',404);need(e['status']!='VOID','ALREADY_VOID','이미 무효 처리됐어요.')
        if return_stock:
            need(e['status']=='CLAIMED','INVALID_INPUT','수령된 간식만 실물 반환할 수 있어요.',400)
            need(e['day_no']!=TEST_DAY,'INVALID_INPUT','테스트 수령권은 실물 재고와 관계없어요.',400)
            c.execute('INSERT INTO inventory_ledger(id,item,delta,reason,entitlement_id,at) VALUES(?,?,1,?,?,?)',(uid(),e['choice'],'VOID_RETURN',e['id'],stamp()))
        c.execute("UPDATE entitlements SET status='VOID' WHERE id=?",(e['id'],));audit(c,'ENTITLEMENT_VOID',reason,{'id':e['id'],'return_stock':return_stock});db.bump(c)
        return {'ok':True}

# Admin submission log: every play with its person, try number and snack, editable at any time.
# Admin edits skip cap/stock checks; real-day ledger rows keep physical stock truthful.
def submission_log(token,day=None):
    with db.transaction() as c:
        require_admin(c,token)
        day_no=get_day(c)['day_no'] if day in (None,'') else None if day=='ALL' else int(day) if str(day).isdigit() else -1
        need(day_no is None or day_no in REAL_DAYS+(TEST_DAY,),'INVALID_DAY','운영일을 확인해 주세요.',400)
        where='s.is_seed=0'+('' if day_no is None else ' AND s.day_no=?')
        rows=c.all(f'SELECT s.id,s.day_no,s.attempt_no,s.participant_id,s.l1,s.l2,s.l3,s.l4,s.l5,s.points,s.opponents,s.top_bp,s.submitted_at,p.kind,p.nickname,p.name_hidden,p.real_name,p.nickname_chosen,i.google_sub AS verified_school_subject,e.id AS e_id,e.code,e.kind AS e_kind,e.choice,e.status,e.issued_at,e.claimed_at,e.premium_exhausted FROM submissions s JOIN participants p ON p.id=s.participant_id LEFT JOIN school_identities i ON i.participant_id=p.id LEFT JOIN entitlements e ON e.submission_id=s.id WHERE {where} ORDER BY s.submitted_at DESC,s.attempt_no DESC,s.id',() if day_no is None else (day_no,))
        items=[]
        for r in rows:
            items.append({'id':r['id'],'day_no':r['day_no'],'attempt_no':r['attempt_no'],'participant_id':r['participant_id'],'nickname':display_name(r),'kind':r['kind'],
                'strategy':[r['l1'],r['l2'],r['l3'],r['l4'],r['l5']],'points':r['points'],'opponents':r['opponents'],'win_rate':r['points']/(2*r['opponents']) if r['opponents'] else 0.0,'top_bp':r['top_bp'],'submitted_at':r['submitted_at'],
                'entitlement':None if r['e_id'] is None else {'id':r['e_id'],'code':r['code'],'kind':r['e_kind'],'choice':r['choice'],'status':r['status'],'issued_at':r['issued_at'],'claimed_at':r['claimed_at'],'premium_exhausted':bool(r['premium_exhausted'])}})
        return {'day_no':day_no,'items':items}

def admin_submission(c,submission_id):
    sub=c.one('SELECT * FROM submissions WHERE id=? AND is_seed=0',(submission_id,))
    need(sub is not None,'NOT_FOUND','제출 기록을 찾을 수 없어요.',404)
    return sub,c.one('SELECT * FROM entitlements WHERE submission_id=?',(submission_id,))

def adjust_snack(token,submission_id,payload):
    reason=reason_text(payload);kind=payload.get('kind') or None;choice=payload.get('choice') or None;status=payload.get('status','ISSUED');return_stock=payload.get('return_stock',False)
    need(kind in (None,'BASIC','TOP_CHOICE') and status in ('ISSUED','CLAIMED','VOID') and type(return_stock) is bool,'INVALID_INPUT','간식 수정 값을 확인해 주세요.',400)
    need(choice is None or choice in ITEMS,'INVALID_CHOICE','간식을 골라 주세요.',400)
    need(kind!='BASIC' or choice in (None,'MYCHEW'),'INVALID_CHOICE','마이쮸 수령권에는 마이쮸만 고를 수 있어요. 고급 선택권으로 바꿔 주세요.',400)
    if kind=='BASIC': choice='MYCHEW'
    need(kind is None or status!='CLAIMED' or choice is not None,'INVALID_CHOICE','수령 완료로 바꾸려면 받은 간식을 골라 주세요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token);sub,e=admin_submission(c,submission_id);at=stamp();ent_id=e['id'] if e else uid()
        given_before=e['choice'] if e and e['status']=='CLAIMED' else None
        given_after=choice if kind and status=='CLAIMED' else None
        if sub['day_no']!=TEST_DAY and given_before!=given_after:
            # A corrected item (claimed -> claimed) was never handed out, so it returns automatically.
            if given_before and (given_after or return_stock):
                c.execute('INSERT INTO inventory_ledger(id,item,delta,reason,entitlement_id,at) VALUES(?,?,1,?,?,?)',(uid(),given_before,'ADMIN_ADJUST',ent_id,at))
            if given_after:
                c.execute('INSERT INTO inventory_ledger(id,item,delta,reason,entitlement_id,at) VALUES(?,?,-1,?,?,?)',(uid(),given_after,'ADMIN_ADJUST',ent_id,at))
        claimed_at=(e['claimed_at'] if e and e['status']=='CLAIMED' else at) if kind and status=='CLAIMED' else None
        if kind is None:
            if e: c.execute('DELETE FROM entitlements WHERE id=?',(e['id'],))
        elif e:
            c.execute('UPDATE entitlements SET kind=?,choice=?,status=?,claimed_at=? WHERE id=?',(kind,choice,status,claimed_at,e['id']))
        else:
            code=''.join(secrets.choice('23456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(6))
            while c.one('SELECT id FROM entitlements WHERE code=?',(code,)):
                code=''.join(secrets.choice('23456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(6))
            c.execute('INSERT INTO entitlements(id,submission_id,code,kind,choice,status,issued_at,claimed_at) VALUES(?,?,?,?,?,?,?,?)',(ent_id,submission_id,code,kind,choice,status,at,claimed_at))
        audit(c,'SNACK_ADJUSTED',reason,{'submission_id':submission_id,'before':None if not e else {k:e[k] for k in ('kind','choice','status')},'after':None if not kind else {'kind':kind,'choice':choice,'status':status},'return_stock':return_stock})
        clear_caches(c);db.bump(c)
        return {'ok':True}

def delete_submission(token,submission_id,payload):
    reason=reason_text(payload);return_stock=payload.get('return_stock',False)
    need(type(return_stock) is bool,'INVALID_INPUT','실물 반환 여부를 확인해 주세요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token);sub,e=admin_submission(c,submission_id)
        if e and e['status']=='CLAIMED' and return_stock and sub['day_no']!=TEST_DAY:
            c.execute('INSERT INTO inventory_ledger(id,item,delta,reason,entitlement_id,at) VALUES(?,?,1,?,?,?)',(uid(),e['choice'],'ADMIN_ADJUST',e['id'],stamp()))
        if e: c.execute('DELETE FROM entitlements WHERE id=?',(e['id'],))
        c.execute('DELETE FROM submissions WHERE id=?',(submission_id,))
        # The try is given back; remaining tries are renumbered 1..n (ascending, so the unique key never collides).
        for number,row in enumerate(c.all('SELECT id,attempt_no,response_json FROM submissions WHERE participant_id=? AND day_no=? ORDER BY submitted_at,attempt_no,id',(sub['participant_id'],sub['day_no'])),1):
            if row['attempt_no']!=number:
                saved=json.loads(row['response_json']) if row['response_json'] else None
                if saved: saved['attempt_no']=number
                c.execute('UPDATE submissions SET attempt_no=?,response_json=? WHERE id=?',(number,dumps(saved) if saved else row['response_json'],row['id']))
        audit(c,'SUBMISSION_DELETED',reason,{'submission_id':submission_id,'participant_id':sub['participant_id'],'day_no':sub['day_no'],'attempt_no':sub['attempt_no'],
            'strategy':[sub['l1'],sub['l2'],sub['l3'],sub['l4'],sub['l5']],'entitlement':None if not e else {k:e[k] for k in ('code','kind','choice','status')},'return_stock':return_stock})
        clear_caches(c,tests=(sub['day_no']==TEST_DAY,),scores=True);db.bump(c)
        return {'ok':True}

def participants(token):
    with db.transaction() as c:
        require_admin(c,token)
        rows=c.all('SELECT p.id,p.nickname,p.kind,p.name_hidden,p.real_name,p.nickname_chosen,i.google_sub AS verified_school_subject,COUNT(s.id) AS submission_count FROM participants p LEFT JOIN submissions s ON s.participant_id=p.id LEFT JOIN school_identities i ON i.participant_id=p.id GROUP BY p.id,p.nickname,p.kind,p.name_hidden,p.real_name,p.nickname_chosen,i.google_sub ORDER BY p.nickname')
        for r in rows:
            r['nickname']=display_name(r);r['name_hidden']=bool(r['name_hidden'])
            r['auth_method']='STUDENT_ID' if r['kind']=='STUDENT' else 'GUEST'
            r.pop('real_name',None);r.pop('verified_school_subject',None);r.pop('nickname_chosen',None)
        return {'items':rows}

def hide(token,participant_id,payload):
    reason=reason_text(payload)
    with db.transaction(write=True) as c:
        require_admin(c,token);need(c.one('SELECT id FROM participants WHERE id=?',(participant_id,)) is not None,'NOT_FOUND','참가자를 찾을 수 없어요.',404)
        c.execute('UPDATE participants SET name_hidden=1 WHERE id=?',(participant_id,));audit(c,'NAME_HIDDEN',reason,{'participant_id':participant_id});db.bump(c)
        return {'ok':True}

def freeze(token,payload):
    scope=payload.get('scope');need(scope in ('DAY1','DAY2','DAY3','OVERALL'),'INVALID_SCOPE','순위 범위를 확인해 주세요.',400)
    with db.transaction(write=True) as c:
        require_admin(c,token)
        if c.one('SELECT scope FROM rank_freeze WHERE scope=?',(scope,)):return hall_in(c,scope)
        day_no=3 if scope=='OVERALL' else int(scope[-1]);day=c.one('SELECT * FROM day_config WHERE day_no=?',(day_no,))
        need(day['ever_opened'] and day['booth_state'] in ('DONE','CAP_REACHED'),'BOOTH_NOT_CLOSED','해당 운영일을 마감한 뒤 확정해 주세요.')
        if scope=='OVERALL':need(int(db.config(c,'active_day'))==3,'INVALID_DAY','종합 순위는 3일차에 확정해 주세요.')
        rows=pool_rows(c);ranking=engine.rank_rows(rows,scope_day=None if scope=='OVERALL' else day_no,analysis=analysis_in(c,rows))[:10];when=stamp()
        c.execute('INSERT INTO rank_freeze(scope,rankings_json,frozen_at) VALUES(?,?,?)',(scope,dumps(ranking),when))
        if scope!='OVERALL':c.execute('UPDATE day_config SET frozen_at=? WHERE day_no=?',(when,day_no))
        audit(c,'RANK_FREEZE',details={'scope':scope});db.bump(c)
        return hall_in(c,scope)

def audits(token):
    with db.transaction() as c:
        require_admin(c,token);rows=c.all('SELECT * FROM audit_log ORDER BY at DESC LIMIT 300')
        for row in rows:row['details']=json.loads(row.pop('details_json'))
        return {'items':rows}

def purge(token,payload):
    need(payload.get('confirm')=='PURGE','CONFIRM_REQUIRED','PURGE를 직접 입력해 주세요.',400);reason_text(payload)
    with db.transaction(write=True) as c:
        require_admin(c,token);count=c.scalar('SELECT COUNT(*) FROM participants')
        # Every copy of a nickname, including freeze/cache and seed author, is removed.
        for index,row in enumerate(c.all('SELECT id FROM participants ORDER BY id'),1):
            c.execute('UPDATE participants SET student_id=NULL,real_name=NULL,nickname=?,name_hidden=1,consent_at=NULL WHERE id=?',('익명-'+str(index),row['id']))
        c.execute('DELETE FROM sessions');c.execute('DELETE FROM school_identities');c.execute('DELETE FROM google_login_challenges')
        c.execute('DELETE FROM rate_limits');c.execute('DELETE FROM audit_log')
        c.execute("UPDATE submissions SET author='' ")
        c.execute("UPDATE threshold_history SET reason='개인정보 파기',set_by='ADMIN'")
        for row in c.all('SELECT scope,rankings_json FROM rank_freeze'):
            entries=json.loads(row['rankings_json'])
            for entry in entries:entry['nickname']='익명 전술가'
            c.execute('UPDATE rank_freeze SET rankings_json=? WHERE scope=?',(dumps(entries),row['scope']))
        c.execute("UPDATE day_config SET booth_state='DONE'")
        clear_caches(c);db.set_config(c,'purged','true')
        audit(c,'PRIVACY_PURGED',details={'participants':count});db.bump(c)
        return {'ok':True,'participants':count,'backups_require_deletion':True}

def reset(token,payload):
    need(payload.get('confirm')=='RESET','CONFIRM_REQUIRED','RESET을 직접 입력해 주세요.',400);reason=reason_text(payload)
    with db.transaction(write=True) as c:
        require_admin(c,token);need(not c.scalar('SELECT COUNT(*) FROM day_config WHERE ever_opened=1 AND day_no!=?',(TEST_DAY,)),'RESET_LOCKED','개장 후에는 초기화할 수 없어요.')
        for table in ('school_identities','google_login_challenges','participants','submissions','entitlements','threshold_history','inventory_ledger','rank_freeze','rate_limits'):
            c.execute('DELETE FROM '+table)
        c.execute("DELETE FROM sessions WHERE role!='ADMIN'");c.execute("UPDATE day_config SET booth_state='CLOSED',ever_opened=0,threshold_applied=0,cap_override=0,cap_closed_at=NULL,frozen_at=NULL")
        for key,value in [('active_day','1'),('real_day','1'),('purged','false')]:db.set_config(c,key,value)
        clear_caches(c,scores=True)
        audit(c,'RESET',reason);db.bump(c);return {'ok':True}

def clear_test(token):
    """Erase test-mode play so the real days start clean. Seeds and days 1-3 are untouched."""
    with db.transaction(write=True) as c:
        require_admin(c,token)
        count=c.scalar('SELECT COUNT(*) FROM submissions WHERE day_no=?',(TEST_DAY,))
        # Guests who only ever played in test mode are test accounts; school accounts stay.
        guests=[r['id'] for r in c.all("SELECT p.id FROM participants p WHERE p.kind='GUEST' AND EXISTS(SELECT 1 FROM submissions s WHERE s.participant_id=p.id AND s.day_no=?) AND NOT EXISTS(SELECT 1 FROM submissions s WHERE s.participant_id=p.id AND s.day_no!=?)",(TEST_DAY,TEST_DAY))]
        c.execute('DELETE FROM entitlements WHERE submission_id IN (SELECT id FROM submissions WHERE day_no=?)',(TEST_DAY,))
        c.execute('DELETE FROM submissions WHERE day_no=?',(TEST_DAY,))
        c.execute('DELETE FROM threshold_history WHERE day_no=?',(TEST_DAY,))
        for pid in guests:
            c.execute('DELETE FROM sessions WHERE participant_id=?',(pid,));c.execute('DELETE FROM participants WHERE id=?',(pid,))
        c.execute("UPDATE day_config SET booth_state='CLOSED',ever_opened=0,threshold_applied=0,cap_override=0,cap_closed_at=NULL,frozen_at=NULL WHERE day_no=?",(TEST_DAY,))
        clear_caches(c,tests=(True,),scores=True)
        audit(c,'TEST_CLEARED',details={'submissions':count,'guests':len(guests)});db.bump(c)
        return dashboard_in(c)

def export_data(token):
    with db.transaction(write=True) as c:
        require_admin(c,token)
        # Exports are operational, not identity backups. No personal identity or session material.
        tables={}
        # Test-mode play is rehearsal data, not event records.
        tables['submissions']=c.all(f'SELECT id,day_no,attempt_no,l1,l2,l3,l4,l5,is_seed,is_ranked,points,opponents,threshold_bp_used,top_bp,submitted_at FROM submissions WHERE {pool_scope(False)} ORDER BY submitted_at')
        tables['entitlements']=c.all(f"SELECT e.* FROM entitlements e JOIN submissions s ON s.id=e.submission_id WHERE {pool_scope(False,'s.')} ORDER BY e.issued_at")
        tables['inventory_ledger']=c.all('SELECT * FROM inventory_ledger ORDER BY at')
        tables['threshold_history']=c.all('SELECT id,day_no,mode,value_bp,source,set_at FROM threshold_history WHERE day_no!=? ORDER BY set_at',(TEST_DAY,))
        tables['day_config']=c.all('SELECT * FROM day_config WHERE day_no!=? ORDER BY day_no',(TEST_DAY,))
        tables['rank_freeze']=[]
        for row in c.all('SELECT * FROM rank_freeze'):
            entries=json.loads(row['rankings_json'])
            for entry in entries:entry.pop('nickname',None);entry.pop('participant_id',None)
            tables['rank_freeze'].append({'scope':row['scope'],'rankings_json':dumps(entries),'frozen_at':row['frozen_at']})
        return {'format':'blotto-anonymous-export-v1','created_at':stamp(),'restore_supported':False,'tables':tables}
