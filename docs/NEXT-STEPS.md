# 지금 할 일 — DB 복구가 최우선

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[WSL]** `/home/yrc/AI-Infra-Assistant`

> ## ⛔ 먼저 읽으세요
> **`docker volume prune` / `docker system prune` / `docker compose down -v` 를 실행하지 마세요.**
> 데이터는 **지워진 게 아니라 연결만 끊긴** 상태일 가능성이 높습니다. 위 세 명령만이
> 그것을 영구 삭제합니다. 1번으로 복구 가능 여부부터 확인합니다.

**무슨 일이 있었나**: `docker-compose.dev.yml`의 postgres에 이름 있는 볼륨이 없어서 익명
볼륨이 붙어 있었습니다. 익명 볼륨은 **컨테이너를 다시 만들면 떨어져 나갑니다.** 지난 반영에서
postgres의 `ports:` 를 `127.0.0.1:` 로 바꿨는데, 포트가 바뀌면 compose가 컨테이너를 재생성합니다.
그래서 빈 볼륨이 새로 붙고 initdb → 기본값 시드가 돌아 전부 초기화된 것처럼 보입니다.

**살아 있는 것**: Open WebUI 계정·대화(`open_webui_dev_data`), 차트(`chart_files`),
업로드 원본 파일(`05_halo/datasets` — 저장소 밖이라 rsync가 건드리지 않음).
**사라진 것**: 설정값·매뉴얼·VOC·등록 커맨드(전부 postgres 안).

---

## 1. [서버] 떨어져 나간 볼륨 찾기 — 복구 가능한지부터

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
for v in $(docker volume ls -qf dangling=true); do
  ver=$(docker run --rm -v "$v":/v alpine cat /v/PG_VERSION 2>/dev/null)
  [ -n "$ver" ] && echo "$v  PG_VERSION=$ver  $(docker run --rm -v "$v":/v alpine du -sh /v | cut -f1)"
done
```

`PG_VERSION=16` 이 찍히는 볼륨이 나오면 **데이터가 살아 있습니다.** 그 이름을 적어 두세요
(64자리 16진수입니다). 두 개 이상 나오면 용량이 가장 큰 것이 찾는 볼륨입니다.

**아무것도 안 나오면 여기서 멈추고 알려주세요.** 그다음은 재등록 절차라 방법이 달라집니다.

## 2. [서버] 내용 확인 + 백업 파일 뽑기

`<VOL>` 자리에 1번에서 찾은 이름을 넣으세요.

```bash
docker run -d --name pg-rescue -v <VOL>:/var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=devpass pgvector/pgvector:pg16
sleep 10
docker exec pg-rescue psql -U agent -d platform_config -c \
  "select count(*) from platform_settings;"
docker exec pg-rescue psql -U agent -d manual_db -c \
  "select count(*) from manuals;"
docker exec pg-rescue psql -U agent -d command_db -c \
  "select count(*) from execution_commands;"
```

숫자가 예전 그대로면 맞게 찾은 것입니다. **결과를 보내주세요.** 이어서 백업 파일을 뽑습니다.

```bash
docker exec pg-rescue pg_dumpall -U agent > /home/gpu1/yr9.choi/05_halo/pg-backup-$(date +%F).sql
ls -lh /home/gpu1/yr9.choi/05_halo/pg-backup-*.sql
docker stop pg-rescue && docker rm pg-rescue
```

이 `.sql` 파일은 **이후 단계가 잘못돼도 되돌릴 수 있는 보험**입니다. 지우지 마세요.

## 3. [WSL] 고친 코드 받아서 서버로

`docker-compose.dev.yml`에 `pg_data_dev` 이름 있는 볼륨을 넣었습니다. 이제 컨테이너를 다시
만들어도 DB가 사라지지 않습니다(회귀 테스트도 걸어 뒀습니다).

```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
bash /home/yrc/AI-Infra-Assistant/scripts/deploy-rsync.sh
```

이제 rsync 커맨드를 손으로 치지 않습니다. 스크립트가 제외 목록(`.env`·`secrets/`)을 갖고
있고, **서버에서 지워질 파일을 먼저 보여준 뒤 확인을 받습니다**(#137이 그렇게 났습니다).

## 4. [서버] 새 볼륨을 만들고 **옛 데이터를 그대로 옮긴다**

`down`에 **`-v`를 붙이지 마세요.**

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml up -d postgres     # 새 이름 볼륨 생성
sleep 10
docker compose -f docker-compose.dev.yml down               # -v 금지
NEW=$(docker volume ls -q | grep pg_data_dev)
echo "새 볼륨: $NEW"
```

`$NEW`가 한 줄 나오는지 확인한 뒤, 옛 데이터를 덮어씁니다(`<VOL>`은 1번의 이름).

```bash
docker run --rm -v <VOL>:/from -v "$NEW":/to alpine \
  sh -c 'rm -rf /to/* /to/..?* 2>/dev/null; cp -a /from/. /to/ && ls /to/PG_VERSION'
```

마지막에 `/to/PG_VERSION`이 찍히면 복사 성공입니다.

## 5. [서버] 기동 — **`db-init`은 아직 돌리지 마세요**

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml up -d --no-build
docker compose -f docker-compose.dev.yml ps
docker compose -f docker-compose.dev.yml exec -T postgres \
  psql -U agent -d manual_db -c "select count(*) from manuals;"
```

매뉴얼 수가 예전대로면 복구 완료입니다. **그 결과를 보내주세요.**
확인된 뒤에 마이그레이션을 올립니다(새 설정 키가 필요합니다).

```bash
docker compose -f docker-compose.dev.yml run --rm db-init
docker compose -f docker-compose.dev.yml restart admin-console
```

`db-init`은 기존 행을 덮어쓰지 않고 없는 것만 넣습니다. 그래도 **복구 확인 후에** 도는 것이
안전해서 순서를 나눴습니다.

## 6. [웹] 콘솔에서 값 확인

복구가 끝나면 설정 탭에서 아래를 확인하세요(값이 mock으로 돌아가 있으면 다시 넣습니다).

| key | 값 |
|---|---|
| `vllm_llm_base_url` | `http://75.23.32.41:8000/v1` |
| `vllm_llm_model` | `qwen3-235b-a22b` |
| `vllm_embed_base_url` | `http://75.23.32.41:8010/v1` |
| `vllm_embed_model` | `bge-m3` |
| `rerank_provider` / `rerank_base_url` / `rerank_model` | `vllm` / `http://75.23.32.41:8020/v1` / `bge-reranker-v2-m3` |
| `execution_host` | `202.20.185.100` |
| `openwebui_public_url` | `http://202.20.183.30:8502` |
| `agent_api_key` | Open WebUI 연결(Connections)의 API 키와 같은 값 |

그리고 **`지시문을 최신 기본값으로 되돌리기`** → `agent-server 재시작`.

## 7. 복구 뒤에 — 백업을 습관으로

**반영 작업 전에는 항상** 이 한 줄을 먼저 돌리세요. 같은 일이 나도 5분이면 되돌아옵니다.

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/backup-db.sh
```

`05_halo/` 밑에 `pg-backup-<날짜>.sql`로 떨어집니다(저장소 밖이라 rsync가 못 건드립니다).
14개까지 보관하고 오래된 것은 알아서 지웁니다. 덤프가 비어 있으면 성공으로 처리하지 않습니다.

되돌릴 때:

```bash
ls -1t ../pg-backup-*.sql | head
DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql
bash scripts/restart-mounted.sh
```

---

## 복구가 끝난 뒤에 할 일 (지금 하지 마세요)

1번이 성공했는지 확인되면 그때 안내하겠습니다. 대기 중인 것: 엑셀로 커맨드 재등록
(`현재 등록분 내보내기` → 수정 → 업로드), 남의 계정 차단 동작 확인
(`cocoa.song 계정이 어떤 gpu job 을 수행중이야?` → 한 줄 거절), 소요 시간 측정.

---

## 문제가 계속될 때만

| 증상 | 조치 |
|---|---|
| 1번에서 `PG_VERSION` 볼륨이 안 나옴 | 멈추고 알려주세요. `docker ps -a`에 옛 postgres 컨테이너가 남아 있을 수 있습니다 |
| 4번에서 `$NEW`가 비어 있음 | 3번 rsync가 안 된 것입니다. 서버에서 `grep pg_data_dev docker-compose.dev.yml` 확인 |
| 5번에서 postgres가 안 뜸 | `docker compose -f docker-compose.dev.yml logs postgres` 를 보내주세요 |
| `docker compose exec` 가 아무것도 안 뱉음 | 컨테이너가 죽은 것입니다. `ps -a`로 확인(`ps`에는 안 보입니다) |
