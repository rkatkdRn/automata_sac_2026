# Automata · Colonel Blotto

첨부 개발설계서 v10과 참조 엔진을 바탕으로 만든 행사 운영용 웹 앱입니다. 병사 101명을 5개 전선에 배치하며, 참가자 화면·TV 스코어보드·명예의 전당·운영 콘솔을 하나의 서버에서 제공합니다.

**이번 수정:** 학생은 학교 Google 계정(`00-000@ksa.hs.kr`)으로 인증하고 계정 등록 이름을 표시합니다. 외부인만 닉네임으로 최대 12시간 임시 참여합니다. 기존 학번·이름 입력 로그인은 차단합니다. 실제 서비스 적용에는 새 소스 배포와 `GOOGLE_CLIENT_ID` 설정이 필요합니다. [학교 Google 로그인 설정 순서](docs/GOOGLE_LOGIN.md)를 먼저 확인하세요. 이전 Python 패키지 로딩 수정과 모바일 메뉴 표시 수정도 포함되어 있습니다.

**검증 상태:** 이번 인증 변경의 보안·회귀 시험과 기존 게임 엔진 검증은 [docs/VERIFICATION.md](docs/VERIFICATION.md)에 기록합니다. 실제 학교 Google 계정과 Vercel/PostgreSQL을 연결한 최종 인수시험은 운영진 설정 후 진행해야 합니다. 기존 대표 부하 시험은 로컬 p95 약 0.83초로 설계서의 500ms 목표에 미달했으며, 실제 서버 성능 보장을 의미하지 않습니다.

## 1. 구성과 주소

| 주소 | 용도 |
|---|---|
| `/` | 참가자 로그인, 게임 시작 |
| `/play` | 병사 배분 |
| `/result` | 제출 결과·간식 선택·수령 |
| `/board` | TV용 현재 순위·순환·남은 간식 |
| `/hall` | 일별·종합 확정 명예의 전당 |
| `/admin` | 운영진 콘솔 |
| `/privacy.html` | 공개 개인정보 처리 안내 |
| `/healthz` | 서버·DB 연결·한국 시각 확인 |

프론트엔드는 HTML/CSS/JavaScript 모듈이고, 학교 로그인에는 Google Identity Services 공식 스크립트를 불러옵니다. 서버는 FastAPI + NumPy이며 Google 공식 Python 인증 라이브러리로 토큰을 검증합니다. 게임 규칙은 서버에서 강제합니다. Vercel에서는 PostgreSQL을, 노트북에서는 SQLite를 사용합니다.

`Vercel 화면 + Vercel Python API → Neon PostgreSQL`

Vercel은 화면뿐 아니라 Python API도 실행할 수 있습니다. 다만 함수의 로컬 파일이나 메모리를 영구 DB로 사용할 수 없습니다. 그래서 Vercel에서 `DATABASE_URL`이 없으면 제출을 거절하며, 임시 SQLite로 조용히 대체하지 않습니다. 모든 서버 인스턴스가 같은 DB를 보고 제출·간식 예약을 트랜잭션으로 처리합니다.

## 2. 가장 쉬운 배포: GitHub + Vercel + Neon

### A. 소스 올리기

1. ZIP을 풀어 `blotto` 폴더를 엽니다. `app.py`, `requirements.txt`, `vercel.json`, `public`, `server`가 있는 위치가 프로젝트 루트입니다.
2. GitHub에서 **개인 계정 소유의 비공개 저장소**를 만듭니다. Vercel Hobby의 Git 조직 저장소 연결 제한을 피할 수 있습니다. 이미 적합한 저장소가 있으면 사용해도 됩니다.
3. 프로젝트 루트의 파일과 폴더를 업로드하거나 Git으로 push합니다. ZIP 자체만 업로드하면 실행되지 않습니다.
4. 실제 `.env`, DB, 백업, PIN을 저장소에 올리지 않습니다. `.env.example`은 빈 설정 예시이므로 포함해도 됩니다.

### B. 무료 PostgreSQL 만들기

1. [Neon](https://neon.com)에 가입하고 **Free** 프로젝트를 만듭니다. 유료 업그레이드를 선택할 필요는 없습니다.
2. Neon에는 일본·한국 지역이 없어 아시아에서는 Singapore (`aws-ap-southeast-1`)가 가장 가깝습니다. Vercel 함수도 같은 Singapore (`sin1`)로 설정되어 있습니다. 제출 한 건이 DB를 20여 번 왕복하므로 함수와 DB가 다른 지역이면 제출마다 수 초가 걸립니다(서울↔Singapore 약 100ms/왕복 측정). 다른 Neon 지역을 쓰면 `vercel.json`의 region도 그 지역으로 맞추세요.
3. 프로젝트의 **Connect** 화면에서 데이터베이스와 역할을 선택하고 **Connection pooling**을 켭니다.
4. `postgresql://...` 형식의 연결 문자열을 복사합니다. 보통 호스트에 `-pooler`가 들어가며 SSL 옵션이 붙어 있습니다. 이것이 `DATABASE_URL` 값입니다. 값 전체는 비밀번호이므로 채팅이나 저장소에 붙여넣지 마세요.
5. 백업용으로 pooling을 끈 직접 연결 문자열도 따로 보관합니다. 운영 노트북의 `DATABASE_URL_UNPOOLED`에만 설정하면 됩니다.

Vercel의 Storage/Marketplace에서 Neon을 연결하는 방식도 가능합니다. 이 경우 자동으로 추가된 환경 변수 중 pooled PostgreSQL URL을 이 앱이 읽는 **`DATABASE_URL`** 이름으로 지정하세요. 비슷한 이름의 변수만 있고 `DATABASE_URL`이 없으면 앱이 DB를 사용하지 못합니다.

### C. Vercel 프로젝트 가져오기

1. [Vercel](https://vercel.com)에서 **Add New → Project → Import Git Repository**를 선택합니다.
2. 위 저장소를 선택합니다. 프로젝트 생성 권한이 있는 개인 계정 또는 팀을 선택하세요. 이번 자동 배포 시도는 연결된 팀의 생성 권한 부족으로 실패했습니다.
3. **Framework Preset: FastAPI**로 설정합니다. 소스에 포함된 `vercel.json`도 같은 값을 지정합니다.
4. **Root Directory**는 `app.py`가 있는 폴더입니다. 저장소 최상위에 코드 파일을 올렸다면 기본값, 저장소 안에 `blotto/` 폴더째 올렸다면 `blotto`입니다.
5. **Build Command / Output Directory / Install Command**는 기본값을 사용합니다. 프론트엔드 npm build는 필요 없습니다. `requirements.txt`로 Python 의존성을 설치합니다.
6. 아래 환경 변수를 **Production**에 넣습니다. Preview에는 별도 테스트 DB를 사용하세요.

| 변수 | 필수 여부 | 넣을 값 |
|---|---|---|
| `DATABASE_URL` | Vercel 필수 | Neon의 pooled PostgreSQL URL 전체 |
| `ADMIN_PIN` | 필수 | 운영진만 아는 16자 이상 긴 비밀번호 |
| `GOOGLE_CLIENT_ID` | 학생 로그인 필수 | Google에서 생성한 Web Client ID (`…apps.googleusercontent.com`) |
| `PUBLIC_BASE_URL` | 배포 후 권장 | 실제 고정 주소, 예: `https://프로젝트명.vercel.app` (끝 `/` 없이) |
| `PRIVACY_CONTACT` | 운영 전 설정 | 개인정보 담당자·연락 경로. 로그인 처리 안내에 표시됨 |

`ADMIN_PIN`이라는 이름이지만 4자리 PIN이 아니라 긴 비밀번호를 사용합니다. 앱에 기본 관리자 암호는 들어 있지 않습니다. `DB_PATH`는 Vercel에서 설정하지 않습니다. `VERCEL`은 플랫폼이 자동 설정하므로 직접 입력할 필요 없습니다.

7. **Deploy**를 누릅니다. 처음에는 Python 패키지 설치 때문에 시간이 걸릴 수 있습니다.
8. 완료 후 고정 Production 주소를 `PUBLIC_BASE_URL`로 설정하고 **Redeploy**합니다. 환경 변수 변경은 기존 배포에 자동 반영되지 않으므로 재배포가 필요합니다.
9. 참가자에게는 매 배포마다 달라지는 Preview URL 대신 고정 Production 주소를 안내합니다.
10. 실제 참가자 폰의 시크릿 창에서도 주소가 열리는지 확인합니다. Vercel 계정 로그인을 요구한다면 프로젝트의 **Deployment Protection** 설정을 확인해 행사 Production URL이 참가자에게 공개되도록 설정합니다. `/admin`은 앱 자체의 관리자 로그인으로 보호됩니다.

### D. 서버가 정상인지 확인

`https://실제주소/healthz`를 열어 `ok: true`, `db: "ok"`와 한국 시각을 확인합니다. 이 확인만으로 행사 준비가 끝난 것은 아닙니다. `/admin`에서 재고·시드·운영일·임계값을 설정해야 합니다.

API 요청이 첫 실행될 때 테이블을 자동 생성합니다. 새 Neon 프로젝트에 SQL을 손으로 붙여넣을 필요가 없습니다. 임의의 기존 데이터베이스와 공유하지 말고 이 앱 전용 DB를 사용하세요.

### E. `No module named 'server'`가 나타날 때

이 오류는 DB 연결 이전에 Python이 프로젝트의 `server` 패키지를 찾지 못했다는 뜻입니다. 로그만으로는 GitHub 업로드 누락, 배포 파일 누락, Python 검색 경로 문제 중 어느 것인지 확정할 수 없습니다.

1. 이 수정본 ZIP을 새 폴더에 풀고, 안쪽 `blotto` 폴더를 엽니다.
2. GitHub의 기존 `app.py`가 보이는 위치에서 **Add file → Upload files**를 누릅니다.
3. 탐색기의 `blotto` 안에 있는 **파일과 폴더 모두**를 업로드 영역으로 끌어놓고 커밋합니다. `server`와 `public` 폴더도 반드시 포함합니다. ZIP 파일 자체나 바깥 `blotto` 폴더를 올리지 마세요.
4. GitHub에서 `app.py` 옆에 `server`와 `public` 폴더가 있는지 확인합니다. `server` 안에는 `__init__.py`, `app.py`, `db.py`, `engine.py`, `service.py`, `google_auth.py`가 있어야 합니다.
5. Vercel의 새 배포가 방금 커밋을 사용했는지 확인합니다. 자동 배포가 시작되지 않으면 최신 커밋으로 배포합니다. 과거 커밋의 Redeploy는 수정 내용을 반영하지 않습니다.
6. Production 주소의 `/healthz`에서 `ok: true`, `db: "ok"`를 확인하고 `/`, `/play`, `/admin` 화면을 확인합니다. `db: "unavailable"`이라면 서버를 불러오는 단계는 통과했으며, 다음으로 Production의 `DATABASE_URL`을 확인해야 합니다.

수정 파일: `app.py`(프로젝트 경로를 Python 검색 경로에 추가), `vercel.json`(`server/**` 포함), `server/__init__.py`(패키지 설명 추가). `server`는 외부 설치 라이브러리가 아니라 이 프로젝트에 포함된 코드입니다.

## 3. 배포 직후 운영 준비 순서

1. `/admin`에서 `ADMIN_PIN`으로 로그인합니다.
2. **운영일/설정**에서 날짜와 운영시간을 확인합니다. 기본은 한국 시각 2026-10-30, 10-31, 11-01이며, 매일 10:00–13:00 / 14:00–17:00입니다. 달력이 아니라 관리자가 선택한 운영일 기준으로 하루 3회가 계산되며, 선택된 날짜·시간도 제출 시 확인합니다.
3. **재고**에서 실제 마이쮸·참깨 크래커·에이스 수량을 각각 입력합니다. 두 고급 상품의 개별 수량은 첨부 자료에 없으므로 임의로 나누지 않았습니다. 실제 수량 입력 전에는 고급 간식을 발급할 수 없습니다.
4. 누적 상한은 설계서대로 마이쮸 **188 / 338 / 450**, 고급 합계 **40 / 72 / 95**입니다. 상한과 실물 재고는 서로 다른 값입니다. 실물 재고는 수령 시 줄고, 상한 카운터는 미수령 예약도 포함합니다.
5. **시드**에서 부원들이 직접 만든 전략을 최소 50개 입력합니다. 화면 입력, JSON 가져오기, CSV 가져오기를 지원합니다. 자동 시드 생성 기능은 없습니다. 가져오기는 추가 동작이므로 같은 파일을 반복 가져오지 마세요.
6. **임계값**에서 추천값을 확인하고 운영진이 승인한 값을 적용합니다. `7200`은 승률 `72.00%`입니다. 1일차 추천값은 시드 풀의 참고값이므로 계획서의 별도 사전 보정 판단을 대신하지 않습니다.
7. **개장**합니다. 시드 50개, 임계값 적용, 유효한 날짜·시간·재고가 갖춰져야 정상 진행됩니다. 임계값은 하루에 한 번, 첫 개장 전에만 적용할 수 있습니다.
8. TV에서 `/board`를 열어 전체화면으로 표시합니다. QR은 운영 콘솔에서 내려받습니다.

**테스트 모드:** 대시보드 → 운영일 설정에서 **테스트 모드**를 선택하면 날짜·운영 시간과 관계없이 개장해 제출·간식 선택·수령을 시험할 수 있습니다. 시드 50개와 임계값은 필요하지만 실물 재고는 필요 없고, 수령해도 재고 원장이 바뀌지 않습니다. 테스트 제출은 시드하고만 대결하며 1~3일차 순위·간식 상한·내보내기에 들어가지 않습니다. 테스트 모드를 개장해도 시드 입력·초기화·1~3일차 설정이 잠기지 않고, 테스트 임계값은 언제든 다시 바꿀 수 있습니다. 끝나면 테스트 모드를 마감하고 **테스트 기록 삭제** 후 1일차로 전환하세요. 실제 운영 중(개장·일시정지)에는 전환할 수 없습니다.

리허설은 Production과 다른 Neon 프로젝트/DB 또는 로컬 `data/dress.db`에서 하세요. 개장한 운영 DB에서는 RESET을 허용하지 않습니다. 행사 데이터를 지우고 다시 시작하는 버튼이 필요해지는 상황을 피하기 위한 제한입니다.

## 4. 참가와 간식 지급 흐름

재학생은 이름 공개·개인정보 이용에 동의한 뒤 학교 Google 계정으로 로그인합니다. 앱에서 임의로 학번·실명·닉네임을 입력하지 않습니다. `00-000@ksa.hs.kr` 형식과 학교 도메인을 서버에서 확인하고 계정에 등록된 이름을 화면에 표시합니다. 계정 프로필 이름은 실명으로 설정되어 있어야 하며, 학교 공식 명단과 이름을 대조하는 기능은 아닙니다. 다른 기기에서도 같은 Google 계정의 제출 횟수와 기록을 이어갑니다.

외부인은 닉네임과 동의로 최대 12시간 임시 참여합니다. 닉네임만으로 이전 계정을 복원할 수 없습니다. 쿠키 삭제·로그아웃·다른 기기로 재참가하면 같은 사람임을 판별할 수 없는 임시 계정의 한계가 있습니다. 기존 학생 데이터는 정확히 같은 학번의 학교 이메일을 인증하면 이어지며, 예전 학생 쿠키는 재인증이 필요합니다.

101명 배분을 제출하면, 누적 풀에서 본인의 모든 이전 제출을 뺀 상대들과 대결합니다. 같은 요청을 네트워크 오류 후 재전송해도 횟수와 재고는 한 번만 처리됩니다. 일일 첫 제출에만 간식을 판정하며, 2·3회차는 순위 경쟁용입니다.

TOP_CHOICE는 마이쮸·참깨·에이스 중 하나로 참가 간식을 **대체**합니다. 추가 지급이 아닙니다. 간식을 고르는 즉시 선택과 재고 예약이 서버에 저장되어 운영진의 미수령 목록에 표시됩니다. 선택 화면을 스태프에게 보여 주고 1초간 수령 버튼을 누릅니다. 성공 도장과 현재 움직이는 시계가 나타난 뒤 실제 간식을 지급합니다. 서버가 성공을 확인하기 전에 물품을 주지 마세요.

## 5. 설계서와 다르게 적용한 부분

| 원래 설계 | 이 구현 |
|---|---|
| 노트북 1대 + SQLite | 기본 배포는 Vercel + PostgreSQL. 별도 로컬 모드에서 SQLite 지원 |
| 인터넷 없이 사용 | 외부인 로컬 모드는 가능. 학교 Google 인증과 Vercel 모드는 인터넷 필요 |
| 외부 위탁 없음 | 클라우드 모드 안내에 Vercel·외부 DB 사용을 반영. 운영 전 담당자가 실제 계약·보관 정책을 확인해야 함 |
| `BEGIN IMMEDIATE`로 쓰기 직렬화 | SQLite는 동일, PostgreSQL은 트랜잭션 advisory lock 사용 |
| 메모리 순위 캐시 | 여러 함수 인스턴스가 공유하도록 DB에 캐시와 변경 버전 저장 |
| 장시간 SSE | 짧은 SSE 연결을 자동 재연결하고 5초 폴링으로 복구 |
| 서버가 5분마다 로컬 백업 | 운영 노트북의 백업 스크립트 + Neon 복구 기능. Vercel 내부 파일 백업 없음 |
| React + TypeScript 권장 | 빌드가 필요 없는 JavaScript 모듈. 학교 인증 시 Google 공식 스크립트 사용 |
| 학생 학번·이름·닉네임 입력 | 학교 Google 인증 후 계정 등록 이름 표시. 외부인만 임시 닉네임 |
| 제공 예정 로고 파일 | 첨부에 로고 원본이 없어 새 SVG 엠블럼 제작. 원본 확보 시 교체 가능 |

문서의 충돌은 최종 결정 사항(Q)을 우선했습니다. 개발설계서 4-4의 운영 중 임계값 상향 문구는 Q-5/R12와 충돌하므로 구현하지 않았습니다. T-07의 동시 제출 `TOP_CHOICE 1 + BASIC 1`은 EITHER 즉시 마감과 동시에 만족할 수 없습니다. 따라서 **EITHER에서는 마지막 고급 예약 다음 요청이 CAP_REACHED**, PREMIUM_SOFT에서는 고급 예약 1건 + BASIC 1건으로 검증합니다. 기본 설정은 최종 결정대로 EITHER입니다.

마감 후 사유를 남긴 REOPEN은 자동 상한 마감을 잠시 무시할 수 있지만, 개별 간식 재고·예약 상한을 넘는 발급은 허용하지 않습니다. 수령과 기존 기록 조회는 마감 후에도 가능합니다.

## 6. 무료 사용 범위와 비용 확인

Vercel Hobby는 개인·비상업 용도의 무료 플랜이며 사용량 한도가 있습니다. Neon에도 Free 플랜이 있습니다. 무료 플랜을 선택하고 그 한도 내에서 운영하는 구성이지, 어떤 사용량에서도 무료를 보장하는 구성은 아닙니다. 실제 계정이 Pro 팀인지도 대시보드에서 확인하세요. 앱에는 결제나 유료 서비스 자동 생성 기능이 없습니다.

2026-10-22 공식 문서 확인 기준, Hobby 함수 최대 실행시간은 300초이며 이 프로젝트는 요청당 최대 60초로 제한합니다. Hobby Cron은 하루 한 번만 실행할 수 있어 5분 백업을 예약하지 않았습니다. Neon Free의 시점 복구 창은 최대 6시간입니다. 긴 SSE 연결과 잦은 폴링도 사용량에 포함되므로 행사 중 필요한 TV·운영 화면만 켜두고 Usage를 확인하세요.

약 530명 규모라도 실제 네트워크·콜드 스타트·DB 지역·동시 사용 수에 따라 응답시간이 달라집니다. 로컬 측정값을 Vercel p95 보장으로 해석하면 안 됩니다.

## 7. 개발, 백업, 복구, 행사 후 정리

로컬 설치·Windows 실행·자동 재시작·5분 백업·복구는 [docs/OPERATIONS.md](docs/OPERATIONS.md)에 설명했습니다. 테스트 실행은 다음과 같습니다.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

시드 입력용 CSV 형식:

```csv
l1,l2,l3,l4,l5,tag,author
30,25,20,15,11,혼합형,부원A
```

위 한 줄은 형식 예시이며 50개 자동 시드를 제공하는 파일이 아닙니다. 각 행의 합은 반드시 101이어야 합니다.

행사 마지막 날에는 일별·종합 확정 → CSV 내보내기 → 전체 DB 백업 순서로 정리합니다. **2026-11-08까지** 개인정보 파기를 완료하는 것이 설계서의 행사 종료 후 7일 기준입니다. 앱의 PURGE만 누르면 노트북·USB·다운로드·Neon 복구 이력까지 즉시 지워지는 것은 아닙니다. 백업 사본 삭제와 제공자 보관 정책 확인까지 운영진이 처리해야 합니다. 익명화한 전략·순위 기록은 남길 수 있습니다.

## 8. 문제가 생겼을 때

| 증상 | 확인/해결 |
|---|---|
| 프로젝트 생성 403 | Vercel 팀의 프로젝트 생성 권한 또는 권한 있는 개인 계정 선택 |
| 홈페이지는 열리는데 게임 오류 | `/healthz`, `DATABASE_URL`, DB 연결 SSL, Vercel Function 로그 확인 |
| 관리자 로그인 불가 | Production `ADMIN_PIN` 16자 이상인지 확인 후 Redeploy. 실패 5회/분 제한이면 잠시 기다림 |
| 학생 로그인 설정 대기 / Google 오류 | [학교 Google 로그인 설정](docs/GOOGLE_LOGIN.md)의 Client ID·승인된 JavaScript 원본·학교 정책 확인 |
| 개장 불가 | 시드 50개, 임계값 적용, 운영일 날짜/시간, 재고 입력 확인 |
| 정상이었는데 제출 불가 | 일일 3회, 휴식시간, EITHER 상한 도달, 마감 여부 확인 |
| 고급을 못 고름 | 개별 참깨/에이스 실물 재고 및 예약량 확인 |
| 재배포했더니 DB가 비어 있음 | 다른 DB/branch URL을 연결했는지 확인. Vercel 배포 자체는 외부 DB를 초기화하지 않음 |
| SSE 연결이 주기적으로 재연결됨 | 유한한 함수 실행시간에 맞춘 정상 동작. 연결 실패 시 5초 폴링 |
| 화면 접속에 Vercel 로그인 요구 | 행사 Production의 Deployment Protection 확인 |
| 환경 변수를 바꿨는데 그대로 | Redeploy 필요. Preview와 Production의 변수를 구분 |

운영 DB를 직접 수정하지 말고 콘솔에서 사유를 기록해 조작하세요. DB 복구는 새 요청을 막고 백업을 별도로 보존한 뒤 수행합니다.

## 9. 파일 구조

`app.py` Vercel 진입점 · `server/` DB/API/채점 · `public/` 화면/폰트/엠블럼 · `tests/` 참조 교차 검증 및 통합 테스트 · `scripts/` 실행/백업/복구 · `docs/` API 계약/운영/검증 문서.

## 공식 참고 문서

- [Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi)
- [Python Runtime](https://vercel.com/docs/functions/runtimes/python)
- [Hobby 플랜](https://vercel.com/docs/plans/hobby)
- [Cron 사용 제한](https://vercel.com/docs/cron-jobs/usage-and-pricing)
- [Neon 연결 풀링](https://neon.com/docs/connect/connection-pooling)
- [Neon 요금제/복구 창](https://neon.com/docs/introduction/plans)
- [Google 로그인 설정](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid)
- [Google 토큰 서버 검증](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token)

제공된 원문 규칙에서 벗어나는 의도치 않은 동작을 발견하면 재현 순서와 함께 테스트로 추가하세요. 행사 전 실기기 리허설은 별도로 필요합니다.
