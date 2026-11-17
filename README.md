# PC 없이 받는 무료 경제·세무 브리핑

2026-11-17 GitHub 클라우드 배포와 카카오 본인 동의를 완료했습니다. 본인의 나와의 채팅방으로 실제 테스트 한 건을 전송했고 카카오 API 성공 응답을 확인했습니다. 아래 최초 설치 절차는 재설치 참고용이며 현재 운영 계정에서 다시 할 필요는 없습니다.

뉴스 RSS는 Google News와 Bing News를 순서대로 조회합니다. 요청 제한이 생기면 경제 2일·세무 7일 이내에 이전 조회에서 확인한 자료만 재사용하고, 그림·본문에 **이전 조회 자료**와 원래 게시일을 표시합니다. 기한이 지난 자료는 재사용하지 않습니다. 본인 수신 단말에서 실제 읽었는지는 API 성공 응답으로 확인할 수 없습니다.

한국시간 평일 오전 9시에 GitHub의 Linux 컴퓨터가 실행되며, 한국 공휴일·대체공휴일에는 발송하지 않습니다. 집·사무실 PC나 카카오톡 앱을 켜둘 필요가 없습니다. 마우스·클립보드·데스크톱 제어는 사용하지 않습니다.

공휴일 판정은 매 실행 때 설치하는 최신 `holidays` 한국 달력을 사용합니다. 라이브러리에 반영된 임시공휴일·선거일도 제외합니다. 정부가 새로 지정한 휴일은 달력 라이브러리 반영 시점에 따라 갱신이 늦을 수 있습니다. ‘나에게 보내기’는 별도 푸시 알림이 뜨지 않으므로 나와의 채팅에서 확인하세요.

## 만들어 둔 것

- 미국주식·경제 6개, 공식 기관의 부동산·세무 소식 4개 내외의 한 줄 뉴스.
- S&P500·나스닥100·원달러·미국 10년 금리·VIX·금 선물과 최근 일별 추이 그림.
- 한글 PNG 카드와 출처 링크가 있는 보고서. 카톡에는 이미지 카드 한 건과 원문 보기 버튼으로 전송.
- 카카오 공식 `나에게 보내기` API만 사용. 다른 사람에게 발송하는 기능 없음.
- 토큰 자동 갱신, 새 리프레시 토큰 암호화 저장, 중복·불확실한 발송 자동 재시도 금지.
- 최초 설치 프로그램: 사용자 본인 로그인 후 파일·GitHub Secrets 업로드와 테스트 실행 요청까지 처리.

유료 AI·유료 데이터 API·유료 서버를 사용하지 않습니다. **뉴스 제목 기반 요약**이며 원문 전문 분석·세무 자문 자동화는 아닙니다. 세무 뉴스는 공식 기관 출처로 제한하되 개편안 통과 여부·시행일을 자동 확정하지 않고 원문 확인 대상으로 표시합니다. 데이터 API는 Yahoo Finance 공개 차트 API로, 거래소 공식 실시간 API가 아니며 지연·차단될 수 있습니다. 뉴스는 Google News RSS로 확인합니다. 조회 실패·휴장 등은 기준 시각과 함께 표시합니다. 모든 소스가 실패하면 발송하지 않고 Actions를 실패 처리합니다.

## 본인이 해야 하는 것 — 최초 한 번

### 1. GitHub에 전용 공개 저장소와 일회용 설치 토큰 준비

1. [새 저장소](https://github.com/new)에서 `daily-kakao-briefing` 이름의 **Public** 저장소를 만들고 **Add a README file**을 선택합니다. 기본 브랜치는 `main`입니다. 다른 파일이 있는 기존 저장소는 사용하지 않습니다.
2. [Fine-grained personal access tokens](https://github.com/settings/personal-access-tokens)에서 새 토큰을 만듭니다. Resource owner는 본인, Repository access는 **Only select repositories → 방금 만든 저장소만** 선택합니다.
3. Repository permissions에서 **Contents / Workflows / Secrets = Read and write**로 설정합니다. Metadata read는 자동 포함됩니다. 만료는 짧게 잡아도 됩니다. 설치 때 한 번 쓰고 최초 테스트 성공 후 폐기할 수 있습니다. 매일 실행에는 이 개인 토큰을 쓰지 않습니다.

공개 저장소는 무료 표준 GitHub Actions와 외부에서 읽을 수 있는 이미지 주소를 얻기 위해 필요합니다. **코드·일반 뉴스·시장 데이터·암호화된 토큰 파일은 공개**됩니다. 비밀키는 GitHub Secrets에만 들어가고, 보유 수량·계좌 금액·고객 정보·개인 세무 자료는 수집하거나 게시하지 않습니다. 이 공개 방식이 원치 않으면 설치를 진행하지 마세요.

### 2. 카카오 개발자 앱 준비

1. [Kakao Developers](https://developers.kakao.com/console/app)에 본인 계정으로 로그인하고 본인용 앱을 만듭니다.
2. 앱에서 **카카오 로그인 사용 설정 ON**을 켭니다.
3. **플랫폼 키 → REST API 키**에서 Redirect URI를 정확히 다음과 같이 등록합니다: `http://localhost:8765/callback`
4. **카카오 로그인 → 동의항목**에서 `카카오톡 메시지 전송`(`talk_message`)을 사용할 수 있도록 설정합니다. 이 항목을 제공하지 못하는 앱 상태라면 카카오에서 요구하는 앱 설정·본인 확인을 먼저 완료해야 합니다.
5. **제품 링크 관리**에 `https://github.com`과 `https://raw.githubusercontent.com`을 등록합니다. 웹사이트 도메인 입력을 요구하면 이 두 도메인을 사용합니다.
6. REST API 키와 **Client Secret**을 확인합니다. Client Secret은 ON을 유지합니다. 어드민 키는 사용하지 않습니다.

카카오 콘솔 메뉴 이름은 변경될 수 있습니다. [로그인 설정 문서](https://developers.kakao.com/docs/ko/kakaologin/prerequisite), [메시지 API 문서](https://developers.kakao.com/docs/ko/kakaotalk-message/rest-api)를 기준으로 확인하세요.

### 3. 설치 파일 한 번 실행하고 본인 로그인

1. 제공된 ZIP을 풀고 `install.cmd`를 더블클릭합니다. Python 3.11 이상이 필요합니다. 이 작업을 준비한 PC에는 Python이 있습니다.
2. 설치 창에 저장소 `아이디/daily-kakao-briefing`, GitHub 설치 토큰, 카카오 REST API 키, Client Secret을 입력합니다. 비밀값은 화면에 표시되지 않습니다. **이 채팅에 키나 토큰을 붙이지 마세요.**
3. 공개 뉴스 게시에 동의하면 설치 창에 `PUBLIC`을 입력합니다.
4. 열리는 카카오 공식 로그인 페이지에서 직접 로그인하고 메시지 전송에 동의합니다. 비밀번호는 카카오에만 입력합니다.
5. 설치 프로그램이 파일·Secrets를 업로드하고 테스트 발송을 요청합니다. 토큰은 평문 파일로 저장하지 않습니다.

## 테스트 성공을 확인하는 방법

- GitHub 저장소 → **Actions → Daily economic and tax briefing**의 첫 실행을 엽니다.
- 마지막 단계에 **`Kakao API result_code=0: one message sent.`**가 나와야 API 발송 성공입니다.
- 휴대폰의 **나와의 채팅**에서 그림 카드가 실제로 도착하고 버튼으로 출처가 열리는지 확인합니다. 녹색 작업 표시만 보고 수신을 단정하지 마세요.
- 카톡 미리보기에서는 긴 이미지 일부가 잘릴 수 있습니다. 그림 또는 버튼을 누르면 전체 그림과 약 10개 요약을 볼 수 있습니다.
- 이후 한국 공휴일을 제외한 평일 오전 9시(KST)에 예약됩니다. **GitHub의 무료 예약은 지연·누락 가능성이 있어 정각 도착을 보장하지 않습니다.**
- 테스트는 하루 한 번만 발송합니다. 같은 날 재실행하면 중복 방지를 위해 발송하지 않습니다. Actions에서 Run workflow → send=false이면 미리보기 보고서만 새로 만듭니다.
- 첫 실행에서 인증·도메인·권한 오류가 나거나 결과가 불명확하면 자동 재발송하지 않습니다. 실패 원인을 먼저 확인하세요. `.state/날짜.json`을 지워 무작정 재전송하지 마세요.

## 유지와 비용

- 표준 `ubuntu-latest` 공개 저장소 실행을 사용합니다. larger runner·유료 API·결제 등록을 요구하지 않습니다. [GitHub 요금 기준](https://docs.github.com/en/billing/concepts/product-billing/github-actions)을 확인하세요. 서비스의 무료 정책·쿼터가 변경되거나 데이터가 차단되면 보장된 발송은 불가능합니다.
- GitHub의 공개 저장소 예약 워크플로는 저장소 활동이 오랫동안 없으면 비활성화될 수 있습니다. 정상적인 매일 보고서 커밋은 활동이지만, 장기 실패 때 Actions 상태를 확인하세요.
- GitHub Actions 이메일 실패 알림을 켜두면 인증 만료·조회 실패를 알아차릴 수 있습니다.
- 카카오가 동의를 철회하거나 토큰을 폐기하면 재로그인이 필요합니다. 정상 실행 중에는 리프레시 토큰을 갱신해 암호화 보관합니다. 재인증은 설치 폴더에서 `.venv\Scripts\python.exe setup.py --reauthorize`를 실행합니다. 저장소 토큰은 다시 새로 발급할 수 있습니다.
- 중단: GitHub Actions에서 이 워크플로를 **Disable workflow**로 변경합니다. 계정을 탈퇴하거나 다른 예약을 삭제할 필요가 없습니다.

## 검증 명령

```text
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m unittest -v
.venv\Scripts\python.exe briefing.py
```

마지막 명령은 실제 공개 데이터를 조회해 `preview/`에 그림과 보고서만 만듭니다. 카톡을 보내거나 외부에 게시하지 않습니다. 실제 카카오 인증·GitHub 저장소 권한 없이 수행한 로컬 테스트는 클라우드 발송 테스트를 대신하지 않습니다.
