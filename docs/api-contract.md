# API contract (implementation contract)
All errors: `{error:{code,message}}`. Cookie authentication; POST JSON. IDs are strings. All times ISO 8601 Asia/Seoul. No student_id/real_name appears in any response.

## Participant
- POST `/api/session` `{kind:"STUDENT"|"GUEST",nickname,student_id?,real_name?,consent:true}` → `{participant_id,nickname,attempts_left}`. Existing student identity preserves original nickname. Cookie is HttpOnly.
- POST `/api/session/logout` `{}` → `{ok:true}` (for borrowed phone).
- GET `/api/state` → `{day_no,booth_state,open_now,next_open,attempts_left,mychew_left,participant_id:null|string,nickname:null|string,server_time,privacy_contact,cloud_mode,purged:false,threshold_bp}`.
- POST `/api/submissions` `{request_id,strategy:[5 integers],expected_day_no?:1|2|3}` → `{id,day_no,attempt_no,attempts_left,strategy,win_rate,points,opponents,threshold_bp_used,submitted_at,rank_estimate:{rank,of,is_provisional:true},reward:null|{entitlement_id,code,kind,premium_exhausted,choices,status,choice,claimed_at},nemesis:null|{strategy,lost_lanes},pool_size}`. Same request_id returns identical response. Do not change ID after ambiguous network errors.
- GET `/api/me/submissions` → `{items:[full submission results, newest first]}`; current reward status overrides original status; all operating days returned.
- POST `/api/entitlements/{id}/choice` `{choice:"MYCHEW"|"SESAME"|"ACE"}` → full public reward `{entitlement_id,code,kind,premium_exhausted,choices,status:"ISSUED",choice,claimed_at:null}`. Choice is persisted before claim; repeated same choice is idempotent. Choosing MYCHEW immediately moves the premium reservation; switching back validates premium cap and physical stock. Selected premium items reserve specific stock, preventing overbooking.
- POST `/api/entitlements/{id}/claim` `{choice:"MYCHEW"|"SESAME"|"ACE"}` → `{status:"CLAIMED",choice,claimed_at,code,entitlement_id}`.
- GET `/api/scoreboard` → `{rankings:[{rank,participant_id,submission_id,nickname,strategy,points,opponents,win_rate,submitted_at}],cycle:null|[same ranking row x3],recent:[{nickname,strategy,win_rate,submitted_at}],day_no,booth_state,mychew_left,pool_size,updated_at,version}`. Top10 rankings; cycle selected among top30, held >=60s.
- GET `/api/hall?scope=DAY1|DAY2|DAY3|OVERALL` → `{scope,frozen:boolean,frozen_at:null|string,rankings:[]}`.
- GET `/api/events` → SSE events `state.changed` (state JSON), `board.updated` (scoreboard JSON), `heartbeat`. Finite stream closes/reconnects about 25s. Fallback poll 5s.

## Admin
- POST `/api/admin/login` `{pin}` → `{ok:true}`; POST `/api/admin/logout` → `{ok:true}`.
- GET `/api/admin/dashboard` → `{day_no,day:{day_no,date,sessions:[["10:00","13:00"],["14:00","17:00"]],mychew_cap_cum,premium_cap_cum,close_rule,booth_state,ever_opened,frozen_at,cap_override},days:[same day objects],state:participant state without identity,counters:{mychew_used,premium_reserved},inventory:{MYCHEW:n,SESAME:n,ACE:n},pool_size,seed_count,participant_count,submission_count,today_submissions,threshold_bp,threshold_locked,threshold_applied,purged,db_mode}`.
- POST `/api/admin/day` `{day_no}` selects operating day, previous day must not OPEN/PAUSED. POST `/api/admin/day-config` `{day_no,date,sessions,mychew_cap_cum,premium_cap_cum,close_rule,reason}` edits unopened day. All fields required. min_seed=50/max_attempts=3 fixed.
- POST `/api/admin/booth` `{action:"OPEN"|"PAUSE"|"CLOSE"|"REOPEN",reason?}` → dashboard. OPEN resumes PAUSED, first open requires >=50 seeds + threshold applied. REOPEN requires reason; caps remain limits for individual reward issuance, cap auto-stop overridden for that day.
- GET `/api/admin/threshold/suggest` → `{value_bp,percentile:85,pool_size}`.
- POST `/api/admin/threshold` `{value_bp,source:"MANUAL"|"SUGGESTION",reason}` → `{value_bp}`. Once per day before first open; reject after ever_opened.
- GET `/api/admin/claims` → `{items:[{id,submission_id,participant_id,nickname,day_no,code,kind,premium_exhausted,choice,status,issued_at,claimed_at}],counts:{ISSUED:n,CLAIMED:n,VOID:n},by_item:{MYCHEW:n,SESAME:n,ACE:n}}` current day. Status filter optional `?status=ISSUED`.
- POST `/api/admin/entitlements/{id}/void` `{reason,return_stock:false}` → `{ok:true}`. return_stock true only physically returned CLAIMED item.
- GET `/api/admin/seeds` → `{items:[{id,strategy,tag,author,submitted_at}],count}`.
- POST `/api/admin/seeds` `{strategy,tag,author}` → `{id,count}`.
- POST `/api/admin/seeds/import` `{items:[{strategy,tag,author},...]}` OR `{csv:"l1,l2,l3,l4,l5,tag,author\n..."}` → `{imported,count}`. GET `/api/admin/seeds/export` → JSON `{items:[...]}`. No automatic seed generation.
- GET `/api/admin/inventory` → `{inventory:{MYCHEW:n,SESAME:n,ACE:n},counters:{mychew_used,premium_reserved},ledger:[]}`.
- POST `/api/admin/inventory/count` `{item:"MYCHEW"|"SESAME"|"ACE",count:integer,reason}` → same inventory response.
- GET `/api/admin/participants` → `{items:[{id,nickname,kind,name_hidden,submission_count}]}`.
- POST `/api/admin/participants/{id}/hide` `{reason}` → `{ok:true}`.
- POST `/api/admin/freeze` `{scope:"DAY1"|"DAY2"|"DAY3"|"OVERALL"}` → hall response. Must close day first; OVERALL requires day3 closed. Repeated calls return existing fixed result.
- GET `/api/admin/audit` → `{items:[{id,action,reason,details,at}]}` newest first, <=300.
- GET `/api/admin/export` → ZIP of CSV (no names, student IDs, tokens).
- GET `/api/admin/backup` → consistent anonymous JSON operational export (restore_supported:false). Full database backups require the supplied local backup script.
- GET `/api/admin/qr` → SVG QR for PUBLIC_BASE_URL or request origin.
- POST `/api/admin/purge` `{confirm:"PURGE",reason}` → `{ok:true,participants:n,backups_require_deletion:true}`. Deletes sessions, identifying fields and pseudonymizes all names including frozen rankings/cached responses; irreversible; booth closed.
- POST `/api/admin/reset` `{confirm:"RESET",reason}` → `{ok:true}`. Only before any operating day has ever opened.
