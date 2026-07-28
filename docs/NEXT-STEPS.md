# 지금 할 일

## 지금 당장

### 1) 일반 사용자에게 모델이 안 보임 — 모델 자체의 "공개 범위(Visibility)" 설정 필요

Connections에 연결을 등록해도, Open WebUI는 각 모델 항목마다 별도의 **공개 범위**가 있고
기본이 admin(또는 특정 그룹)에게만 보이는 상태일 수 있다(Open WebUI에 알려진 동작 —
아래 출처 참고). Connections 설정과는 별개의 화면이라 추가로 켜야 한다:

1. 관리자 패널(Admin Panel) → 설정(Settings) → **모델(Models)**
2. 목록에서 **"AI Infra Assistant"** 찾기
3. 편집(연필 아이콘) → **공개 범위(Visibility)를 "Public"으로 변경** (또는 특정 그룹에 read
   권한 부여)
4. 저장
5. 일반 사용자 계정으로 재로그인해서 모델이 보이는지, 채팅되는지 확인

그래도 안 보이면:
- Admin Panel → Settings → Users → **Default Permissions**에서 "Models Access"가 꺼져있는지도
  확인(이건 Workspace에서 모델 카드를 만들 수 있는 권한이라 이것만으론 안 될 수 있음 — 위 3번이
  핵심).
- 그래도 안 되면 `docker compose -f docker-compose.dev.yml logs open-webui --tail 50`을 일반
  계정으로 로그인한 직후에 받아서 같이 보내달라.

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
