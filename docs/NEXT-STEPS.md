# 지금 할 일 — DB 복구 (데이터는 살아 있습니다)

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[WSL]** `/home/yrc/AI-Infra-Assistant`

> ## ⛔ 확정 전까지 실행 금지
> `docker compose up` · `down` · `db-init` · `docker volume prune` · `docker system prune`
>
> 익명 볼륨은 컨테이너를 다시 만들 때마다 하나씩 더 떨어져 나갑니다. 지금 후보가 3개인데
> 더 늘면 어느 게 진짜인지 구분하기 어려워집니다. prune 계열은 후보를 **영구 삭제**합니다.

**지금까지 확인된 것** (진단 결과)

| | |
|---|---|
| PG16 볼륨 3개 | `4f7c8f2e…` **2.4G** · `1dc7527f…` 137M · `553dd706…` 132M |
| postgres | `ai-infra-assistant-postgres-1` **Up (healthy)** — 떠 있습니다 |
| 무사한 것 | Open WebUI 계정·대화, 차트, 업로드 원본(`05_halo/datasets`) |

3개 중 하나는 **지금 돌고 있는 빈 DB**입니다. 나머지 둘이 떨어져 나간 것이고, 그중 하나가
원본입니다. 크기로 단정하지 않습니다 — 빈 클러스터도 130MB입니다.

---

## 1. [서버] 지금 postgres가 쓰는 볼륨 + 살아있는 DB 확인

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant

echo "--- 지금 postgres가 쓰는 볼륨 ---"
docker inspect -f '{{range .Mounts}}{{.Name}} -> {{.Destination}}
{{end}}' ai-infra-assistant-postgres-1

echo "--- 지금 DB 행 수 ---"
for p in platform_config:platform_settings manual_db:manual_files \
         manual_db:manual_chunks voc_db:voc_records command_db:execution_commands; do
  db=${p%%:*}; t=${p##*:}; echo -n "  $db.$t = "
  docker exec ai-infra-assistant-postgres-1 psql -U agent -d "$db" -tAc \
    "select count(*) from $t" 2>&1 | head -1
done
```

여기 나온 볼륨 이름이 **후보에서 빠집니다.** 행 수가 전부 0(또는 시드값)이면 초기화가 맞습니다.

## 2. [WSL] 검사 스크립트 받기

```bash
git -C /home/yrc/AI-Infra-Assistant fetch origin main
git -C /home/yrc/AI-Infra-Assistant reset --hard origin/main
bash /home/yrc/AI-Infra-Assistant/scripts/deploy-rsync.sh
```

rsync는 **저장소 디렉토리만** 건드립니다. 도커 볼륨과는 무관하니 지금 하셔도 안전합니다.
삭제될 파일을 먼저 보여주고 확인을 받습니다(`.env`·`secrets/`는 제외되어 있습니다).

## 3. [서버] 후보 볼륨 3개 내용 확인

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/inspect-pg-volume.sh 4f7c8f2ee8fef3ed6647aadfa6bd177b0e9008f35a3ea85622df664045693f8b
bash scripts/inspect-pg-volume.sh 1dc7527fd826d5a2afc08bd1b44e945219c2fd10da65c2747f49c2d367ab9198
bash scripts/inspect-pg-volume.sh 553dd7066a559e45d37bb0d7d7d4b47fadeff60309477e7b9a8ebe0d6a769448
```

각각 **표별 행 수 · 마지막 갱신 시각 · 등록된 커맨드 이름 · 매뉴얼 파일 이름**을 찍습니다.
임시 컨테이너는 포트를 열지 않고 끝나면 자동으로 지웁니다(원본 볼륨은 그대로 둡니다).
원본을 아예 건드리기 싫으면 앞에 `COPY_FIRST=yes` 를 붙이세요.

**1번과 3번 결과를 보내주세요.** 어느 볼륨을 되살릴지 확정하고 4번을 진행합니다.

---

## 4. [서버] 볼륨 교체 — **3번 결과 확인 후에만**

아직 실행하지 마세요. 어느 볼륨인지 확정되면 `<VOL>`을 채워서 안내하겠습니다.

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant

# (1) 먼저 원본을 파일로 뽑아 둔다 — 이후가 잘못돼도 되돌아올 자리
bash scripts/inspect-pg-volume.sh <VOL>          # 마지막으로 한 번 더 확인
docker run -d --name pg-rescue -v <VOL>:/var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=devpass $(docker images --format '{{.Repository}}:{{.Tag}}' | grep pgvector | head -1)
sleep 15
docker exec pg-rescue pg_dumpall -U agent > /home/gpu1/yr9.choi/05_halo/pg-rescue-$(date +%F).sql
ls -lh /home/gpu1/yr9.choi/05_halo/pg-rescue-*.sql
docker rm -f pg-rescue

# (2) 이름 있는 볼륨을 만들고 옛 데이터를 옮긴다
docker compose -f docker-compose.dev.yml up -d postgres    # pg_data_dev 생성
sleep 10
docker compose -f docker-compose.dev.yml down              # -v 절대 금지
NEW=$(docker volume ls -q | grep pg_data_dev); echo "새 볼륨: $NEW"
PGIMG=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep pgvector | head -1)
docker run --rm -v <VOL>:/from -v "$NEW":/to --entrypoint sh "$PGIMG" \
  -c 'rm -rf /to/* /to/..?* 2>/dev/null; cp -a /from/. /to/ && cat /to/PG_VERSION'

# (3) 기동 + 확인
docker compose -f docker-compose.dev.yml up -d --no-build
docker compose -f docker-compose.dev.yml exec -T postgres \
  psql -U agent -d manual_db -c "select count(*) from manual_files;"
```

매뉴얼 수가 예전대로면 복구 완료입니다. 그 뒤에 마이그레이션을 올립니다.

```bash
docker compose -f docker-compose.dev.yml run --rm db-init
docker compose -f docker-compose.dev.yml restart admin-console
```

## 5. [웹] 복구 후 설정 확인

콘솔(`http://202.20.183.30:8501`) 설정 탭에서 값이 mock으로 돌아가 있으면 다시 넣습니다.

| key | 값 |
|---|---|
| `vllm_llm_base_url` / `vllm_llm_model` | `http://75.23.32.41:8000/v1` / `qwen3-235b-a22b` |
| `vllm_embed_base_url` / `vllm_embed_model` | `http://75.23.32.41:8010/v1` / `bge-m3` |
| `rerank_provider` / `rerank_base_url` / `rerank_model` | `vllm` / `http://75.23.32.41:8020/v1` / `bge-reranker-v2-m3` |
| `execution_host` | `202.20.185.100` |
| `openwebui_public_url` | `http://202.20.183.30:8502` |
| `agent_api_key` | Open WebUI 연결(Connections)의 API 키와 같은 값 |

그리고 **`지시문을 최신 기본값으로 되돌리기`** → `agent-server 재시작`.

## 6. 앞으로 — 반영 전에 항상 백업

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/backup-db.sh
```

`05_halo/` 밑에 `pg-backup-<날짜>.sql`로 떨어집니다(저장소 밖이라 rsync가 못 건드립니다).
되돌릴 때: `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql`

---

## 복구가 끝난 뒤에 할 일 (지금 하지 마세요)

- 엑셀로 커맨드 재등록: 콘솔 커맨드 실행 탭 → `엑셀 양식 받기` → 채워서 업로드 →
  `execution-mcp 재시작`. `{option}`은 **선택형**으로, 선택지는 `-j: JSON 형식으로 반환`처럼
  `값: 설명`으로 적습니다(콜론 뒤 공백 필수, 한 줄에 하나씩).
- 남의 계정 차단 확인: `cocoa.song 계정이 어떤 gpu job 을 수행중이야?` → 한 줄 거절이어야 합니다.
- 1~3번 질문 소요 시간 측정.

## 문제가 계속될 때만

| 증상 | 조치 |
|---|---|
| 3번에서 postgres가 안 뜸 | 그 볼륨은 후보에서 제외. 스크립트가 로그 20줄을 찍어 줍니다 |
| 3번 결과가 전부 0행 | 세 개 다 빈 것입니다. 멈추고 알려주세요 |
| `inspect-pg-volume.sh: No such file` | 2번 rsync가 안 된 것입니다 |
| `docker compose exec`가 무응답 | 컨테이너가 죽은 것입니다. `docker ps -a`로 확인(`ps`엔 안 보입니다) |
