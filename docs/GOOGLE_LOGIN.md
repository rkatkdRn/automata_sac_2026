# 학교 Google 로그인 설정 · 2026-10-22

이 수정본은 재학생의 학번·이름 입력 로그인을 없애고 **Google 학교 계정 인증**을 사용합니다. 외부인만 닉네임으로 임시 참여합니다. 소스를 올리는 것과 Google 설정을 모두 완료해야 학생 로그인이 작동합니다.

## 변경되는 로그인 방식

| 구분 | 로그인 | 화면에 보이는 이름 | 유지 기간 |
|---|---|---|---|
| KSA 학생 | Google에서 `00-000@ksa.hs.kr` 학교 계정 선택 | 그 학교 Google 계정에 등록된 이름 | 앱 세션 최대 30일, 로그아웃하면 즉시 종료 |
| 외부인 | 닉네임 입력과 동의 | 입력한 닉네임 | 최대 12시간, 로그아웃하면 종료 |
| 운영진 | 기존 `/admin` 비밀번호 | 기존 운영 콘솔 | 기존과 동일 |

학생은 이 앱에서 이름·학번·닉네임을 직접 입력하지 않습니다. 서버가 Google의 서명·대상 앱·유효기간·학교 도메인·인증된 이메일·일회용 로그인 요청을 확인합니다. 이메일이 `23-103@ksa.hs.kr`처럼 숫자 2자리-숫자 3자리 형식인 학교 계정만 허용합니다. 일반 Gmail 계정이나 학교 이메일 문자열만 보내는 요청으로는 학생 로그인이 되지 않습니다.

표시 이름은 **학교 Google 계정의 프로필 이름**입니다. 학교 계정에 실명이 등록되어 있어야 실명으로 표시됩니다. Google이 전달한 이름을 표시하는 것이며 주민등록 이름을 별도 대조하는 기능은 아닙니다. 프로필 이름이 별칭으로 되어 있다면 학교 계정의 이름을 실명으로 수정하거나 학교 관리자에게 문의하세요. 학교의 공식 명단과 강제 대조하려면 별도 명단 연동이 필요합니다.

## 1. Google Cloud 프로젝트 준비

1. [Google Cloud Console](https://console.cloud.google.com/)에 접속합니다.
2. 위쪽 프로젝트 선택 메뉴에서 사용할 프로젝트를 선택하거나 **New Project / 새 프로젝트**로 `Automata Blotto` 프로젝트를 만듭니다.
3. 메뉴나 검색창에서 **Google Auth Platform**을 엽니다. 처음이면 **Get started / 시작하기**를 누릅니다.
4. **Branding / 브랜딩**에서 앱 이름을 `Automata Blotto`, 지원 이메일과 연락처를 실제 운영진 주소로 설정합니다.
5. **Audience / 대상**을 설정합니다. 개인 Google 계정에서 만든 프로젝트는 **External / 외부**를 사용합니다. 이는 Google의 대상 구분이며, 앱은 서버에서 KSA 학생 계정만 허용합니다. 학교 조직에 속한 프로젝트에서 권한이 있고 **Internal / 내부**를 선택할 수 있다면 그 조직의 계정만 사용하도록 설정할 수 있습니다.
6. 접근 범위는 로그인에 필요한 기본 정보인 `openid`, `email`, `profile`만 사용합니다. Gmail 편지 읽기나 Drive 접근 권한은 필요 없습니다.

학교 계정에서 프로젝트 생성이 제한되면 운영진 개인 계정의 프로젝트를 사용해도 됩니다. 다만 학생의 Google Workspace 조직이 외부 앱 로그인을 차단한 경우에는 학교 관리자의 허용이 필요합니다.

## 2. 웹 Client ID 만들기

1. **Google Auth Platform → Clients / 클라이언트 → Create client / 클라이언트 만들기**로 이동합니다.
2. **Application type / 애플리케이션 유형**을 **Web application / 웹 애플리케이션**으로 선택합니다.
3. 이름은 `Blotto Web`처럼 정합니다.
4. **Authorized JavaScript origins / 승인된 JavaScript 원본**에 다음 주소를 추가합니다.

```text
https://sac-five-chi.vercel.app
```

경로(`/play`, `/api/...`)나 끝의 `/`는 붙이지 않습니다. 실제 참가자에게 안내할 도메인이 바뀌면 그 원본도 추가해야 합니다. Vercel의 매번 달라지는 배포별 주소는 자동 허용되지 않습니다.

5. 이 구현은 Google 버튼의 팝업과 JavaScript 콜백을 사용하므로 **Authorized redirect URIs / 승인된 리디렉션 URI는 비워 둡니다.** `/api/auth/google`를 리디렉션 주소로 넣을 필요가 없습니다.
6. **Create / 만들기**를 누르고 다음 형태의 **Client ID / 클라이언트 ID**를 복사합니다.

```text
숫자-문자열.apps.googleusercontent.com
```

이 앱은 **Client Secret을 사용하지 않습니다.** Client ID는 브라우저에도 제공되는 앱 식별자이며 비밀번호가 아닙니다.

로컬 시험을 하려면 승인된 JavaScript 원본에 `http://localhost`와 `http://localhost:8000`도 추가합니다. 로그인 시험은 `http://localhost:8000`에서 합니다. 일반 LAN IP의 HTTP 주소는 Google 로그인 시험 주소로 적합하지 않습니다.

## 3. Vercel 환경 변수 추가

1. [sac 환경 변수 설정](https://vercel.com/automata9/sac/settings/environment-variables)을 엽니다.
2. 새 변수를 추가합니다.

| 항목 | 값 |
|---|---|
| Key / Name | `GOOGLE_CLIENT_ID` |
| Value | 위에서 복사한 `…apps.googleusercontent.com` 전체 |
| Environment | `Production` |
| Type을 선택하는 화면인 경우 | `Config` |

3. `PUBLIC_BASE_URL`이 `https://sac-five-chi.vercel.app`인지 확인합니다.
4. 저장합니다. `DATABASE_URL`, `ADMIN_PIN`은 기존 값을 유지합니다. 새 DB를 만들거나 기존 DB를 초기화할 필요가 없습니다.

`GOOGLE_CLIENT_ID`가 없으면 학생 로그인은 설정 대기 안내를 보여 주고 차단합니다. 외부인·관리자 로그인과 공개 순위 화면은 계속 사용할 수 있습니다. 환경 변수 값을 바꾸면 새 배포가 필요합니다.

## 4. 수정 소스 올리기

1. 수정본 ZIP을 풀고 안쪽 `blotto` 폴더를 엽니다.
2. GitHub 저장소에서 기존 `app.py`, `requirements.txt`, `public`, `server`가 보이는 위치로 갑니다. 이 위치가 프로젝트 루트입니다.
3. **Add file → Upload files**에서 `blotto` 안의 파일과 폴더를 함께 올립니다. 바깥 `blotto` 폴더나 ZIP 파일 자체를 올리지 않습니다.
4. 특히 `server/google_auth.py`, `server/service.py`, `server/db.py`, `server/app.py`, `requirements.txt`, `public/app.js`, `public/admin.js`, `public/styles.css`, `public/index.html`, `public/privacy.html`, `vercel.json`이 포함되는지 확인합니다. 새 의존성 설치가 필요하므로 프론트엔드 파일만 교체하면 안 됩니다.
5. **Commit changes**로 새 커밋을 만듭니다. Vercel에서 그 커밋의 Production 배포가 **Ready**가 될 때까지 기다립니다.

과거 커밋의 Redeploy는 새 소스를 포함하지 않습니다. 새 소스 배포 이후 환경 변수를 추가했다면 그 최신 커밋의 배포를 Redeploy합니다.

## 5. Google 앱 공개 정보와 학교 허용

배포 후 **Branding**에서 홈페이지를 `https://sac-five-chi.vercel.app`, 개인정보처리방침을 `https://sac-five-chi.vercel.app/privacy.html`로 설정할 수 있습니다. 홈페이지 하단의 처리 안내에는 `PRIVACY_CONTACT`에 설정한 운영진 연락처가 표시됩니다. Google이 도메인 소유 확인이나 브랜드 검증을 요구하면 화면에 표시되는 절차를 따르세요.

학교 전체 행사 전에 **Audience**의 공개 상태를 확인하고 필요하면 **Publish app / 앱 게시**를 선택합니다. 기본 `openid/email/profile` 및 Sign in with Google만 쓰는 앱에는 Google의 일반적인 Testing 100명/7일 제한에 예외가 있습니다. 다만 Google Workspace의 외부 앱 접근 정책은 별도 적용됩니다. 이 앱에 불필요한 Gmail·Drive 등 추가 범위를 넣지 마세요.

`admin_policy_enforced`, `access_denied` 또는 학교 정책 차단 안내가 뜨면 학교 전산 담당자에게 **이 앱의 Client ID와 기본 로그인 정보만 사용하는 목적**을 알려 허용을 요청해야 할 수 있습니다. 프로그램에서 학교 보안 정책을 우회하지 않습니다.

## 6. 실제 확인 순서

1. [인증 설정 확인](https://sac-five-chi.vercel.app/api/auth/config): `configured: true`, `school_domain: "ksa.hs.kr"`, 올바른 `google_client_id`가 보여야 합니다. 이 응답은 공개 설정입니다.
2. [홈페이지](https://sac-five-chi.vercel.app/)를 새로 엽니다. 학생 탭에 학번·이름·닉네임 입력칸이 없어야 합니다.
3. 개인정보 및 이름 공개 동의 후 Google 로그인 버튼을 누르고 학교 계정을 선택합니다. 개인 Gmail로 로그인하면 거절되어야 합니다.
4. 로그인 후 학교 계정 이름이 표시되는지 확인합니다. 다른 기기에서 같은 계정으로 들어가도 기록과 남은 횟수가 같아야 합니다. 운영 중인 행사에서 검증만을 위해 전략을 제출할 필요는 없습니다.
5. 로그아웃하고 외부인 탭에서 닉네임으로 참여되는지 확인합니다. 외부인 로그인은 최대 12시간이며, 로그아웃/쿠키 삭제 후 닉네임만 입력해서 이전 계정을 되찾을 수는 없습니다.
6. 휴대폰에서 `작전 현황`, `명예의 전당` 메뉴와 로그인 버튼을 확인합니다. 이번 소스에는 앞서 수정한 모바일 메뉴 표시도 포함되어 있습니다.

## 7. 예전 로그인 코드가 있는 배포 정리

새 Production에서 학교 로그인이 정상 작동하는 것을 확인한 뒤, Vercel **Deployments**에서 예전 코드로 만든 배포의 개별 주소가 외부에 공개되어 있는지 확인하세요. 예전 배포도 같은 Neon DB를 사용하므로, 공개된 채 남아 있으면 그 주소에서는 예전 학번·이름 로그인이 계속 실행될 수 있습니다.

이전 배포 주소를 보호 설정으로 차단하거나, 더 이상 필요 없는 이전 배포를 삭제해 구 로그인 경로를 종료하세요. 삭제할 때는 **새 학교 로그인 배포와 현재 Production을 제외**하고 이전 배포인지 확인합니다. 현재 서비스에서만 쿠키를 무효화하는 것으로 예전 서버 코드까지 중지되는 것은 아닙니다. 이 수정본을 적용한 뒤 예전 인증 코드로 롤백하지 마세요.

## 기존 참가자 데이터 처리

- 기존 `student_id`가 인증된 학교 이메일 앞부분과 정확히 일치하면 같은 참가자 ID로 연결합니다. 기존 제출·횟수·간식 기록을 유지합니다. 예: 기존 학번 `23-103` → `23-103@ksa.hs.kr`.
- 이전에는 이름을 임의로 입력할 수 있었으므로, 기존 이름과 Google 이름이 다르다는 이유만으로 학교 계정 소유자의 로그인을 막지 않습니다.
- 예전 학생 로그인 쿠키는 새 인증의 증거로 인정하지 않습니다. 학생은 Google로 다시 로그인해야 합니다.
- 기존 실명은 새 인증과 이름 공개 동의 전까지 자동으로 공개하지 않습니다. 재인증 이후 그 학생의 공개 표시 이름이 학교 계정 이름으로 바뀝니다.
- `23103`처럼 기존 학번을 다른 형식으로 입력한 데이터는 자동으로 추측하여 합치지 않습니다. 이런 기록이 있다면 운영진이 소유관계를 확인한 뒤 별도 이관해야 합니다.
- 동명이인도 각각의 Google 계정으로 구분하므로 실명이 같아도 다른 참가자로 로그인됩니다.
- 외부인은 신원 인증이 없는 임시 참가입니다. 새 기기/쿠키 삭제/새 닉네임을 통한 재참가를 사람 단위로 완전히 막을 수 없습니다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| `/api/auth/config`가 404 | 새 `server/app.py`가 배포되지 않음. 현재 커밋과 파일 위치 확인 |
| `configured: false` | Production `GOOGLE_CLIENT_ID` 저장 후 최신 소스로 재배포 |
| 학번 입력 화면이 그대로 | `public/app.js`, `public/index.html` 교체 여부와 현재 Production 커밋 확인 |
| `origin_mismatch` / 등록되지 않은 원본 | 접속 중인 주소의 `https://도메인` 전체를 Google Clients의 승인된 JavaScript 원본과 대조 |
| `invalid_client` | Web application 유형의 Client ID인지, 복사 오류가 없는지 확인 |
| Google 버튼이 안 불러와짐 | 동의 여부, 네트워크/콘텐츠 차단 확장, Google 접속 가능 여부 확인. 외부 앱 안의 브라우저라면 Safari/Chrome에서 열기 |
| 만료되었거나 사용된 로그인 요청 | 화면의 로그인 준비/다시 시도 버튼으로 새 요청을 만들고 다시 Google 로그인 |
| 일반 Gmail/다른 학교 계정 거절 | 정상 동작. 정확한 KSA 학생 학교 계정 선택 |
| 학교 계정인데 차단 | `00-000@ksa.hs.kr` 형식인지, 학교의 외부 앱 허용 정책 확인 |
| 이름이 영문/별칭으로 표시 | 학교 Google 프로필에 등록된 값. 학교 계정의 이름 수정 후 로그아웃·재로그인 |

## 공식 근거

- [Google Identity Services 설정 및 웹 Client ID](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid)
- [서버에서 Google ID 토큰 검증](https://developers.google.com/identity/gsi/web/guides/verify-google-id-token)
- [Google OAuth 앱 대상·Testing 예외·공개 상태](https://support.google.com/cloud/answer/15549945)
- [Google ID 토큰과 프로필 이름](https://developers.google.com/identity/openid-connect/openid-connect#an-id-tokens-payload)
