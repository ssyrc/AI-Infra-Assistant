# 지금 할 일 — DB 복구 (원본 볼륨 확정)

**[서버]** 202.20.183.30 · `/home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant`
**[웹]** 관리자 콘솔 `http://202.20.183.30:8501`

> ## ⛔ `docker volume prune` · `docker system prune` · `down -v` 금지
> 복구가 끝나고 **며칠 지나 정상 동작을 확인할 때까지** 세 후보 볼륨을 그대로 둡니다.
> 지금 지우면 되돌아올 자리가 없어집니다.

## 되살릴 볼륨 (확정)

```
4f7c8f2ee8fef3ed6647aadfa6bd177b0e9008f35a3ea85622df664045693f8b
```

| | 원본 `4f7c8f2e…` | 지금 붙어 있는 `553dd706…` | 옛 세대 `1dc7527f…` |
|---|---|---|---|
| 매뉴얼 파일 / 청크 | **5 / 542** | 0 / 0 | 1 / 175 |
| VOC | **48,314** | 0 | 0 |
| 등록 커맨드 | **3** | 0 | (표 없음) |
| 마지막 활동 | **08-06 02:59** | 08-06 06:31(재시드) | 07-28 |

등록 커맨드 3개(`myquota`·`s2_phd_info_job_id`·`s2_phd_list`)가 실제 등록 목록과 일치하고,
job_logs가 사고 당일 02:59까지 찍혀 있습니다.

---

## 1. [서버] 보험용 덤프 먼저

볼륨을 건드리기 전에 파일로 뽑아 둡니다. 이후가 잘못돼도 여기로 돌아옵니다.

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
VOL=4f7c8f2ee8fef3ed6647aadfa6bd177b0e9008f35a3ea85622df664045693f8b
PGIMG=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep pgvector | head -1)
echo "이미지: $PGIMG"

docker run -d --name pg-rescue -v "$VOL":/var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=devpass "$PGIMG"
sleep 15
docker exec pg-rescue pg_dumpall -U agent \
  > /home/gpu1/yr9.choi/05_halo/pg-rescue-$(date +%F).sql
docker rm -f pg-rescue
ls -lh /home/gpu1/yr9.choi/05_halo/pg-rescue-*.sql
grep -c "CREATE DATABASE" /home/gpu1/yr9.choi/05_halo/pg-rescue-*.sql
```

파일 크기가 수백 MB 이상이고 `CREATE DATABASE` 개수가 8 안팎이면 정상입니다.
**0바이트거나 grep이 0이면 여기서 멈추고 알려주세요.**

## 2. [서버] 이름 있는 볼륨으로 옮기기

`down`에 **`-v`를 붙이지 마세요.**

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
VOL=4f7c8f2ee8fef3ed6647aadfa6bd177b0e9008f35a3ea85622df664045693f8b
PGIMG=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep pgvector | head -1)

grep -q pg_data_dev docker-compose.dev.yml && echo "compose 최신 OK" || echo "!! rsync 먼저"

docker compose -f docker-compose.dev.yml down          # -v 금지
docker volume create ai-infra-assistant_pg_data_dev
docker run --rm -v "$VOL":/from -v ai-infra-assistant_pg_data_dev:/to \
  --entrypoint sh "$PGIMG" -c 'cp -a /from/. /to/ && cat /to/PG_VERSION'
```

- `compose 최신 OK`가 안 나오면 rsync가 안 된 것입니다. 먼저 [WSL]에서
  `bash scripts/deploy-rsync.sh` 를 돌리세요.
- 마지막 줄에 `16` 이 찍히면 복사 성공입니다.
- 새 볼륨은 방금 만든 빈 볼륨이라 지우는 동작이 없습니다(원본 `$VOL`은 그대로 남습니다).

## 3. [서버] 기동 + 확인

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
docker compose -f docker-compose.dev.yml up -d --no-build
sleep 15

docker inspect -f '{{range .Mounts}}{{.Name}} -> {{.Destination}}
{{end}}' ai-infra-assistant-postgres-1

for p in platform_config:platform_settings manual_db:manual_files manual_db:manual_chunks \
         voc_db:voc_records command_db:execution_commands; do
  db=${p%%:*}; t=${p##*:}; echo -n "  $db.$t = "
  docker exec ai-infra-assistant-postgres-1 psql -U agent -d "$db" -tAc \
    "select count(*) from $t" 2>&1 | head -1
done
```

**이렇게 나와야 성공입니다.**

```
ai-infra-assistant_pg_data_dev -> /var/lib/postgresql/data
  platform_config.platform_settings = 58
  manual_db.manual_files = 5
  manual_db.manual_chunks = 542
  voc_db.voc_records = 48314
  command_db.execution_commands = 3
```

`platform_settings`가 58인 것이 맞습니다(빈 것은 60이었습니다 — 새 설정 키 2개는 4번에서 들어갑니다).

## 4. [서버] 마이그레이션 — 3번이 위 숫자대로 나온 뒤에만

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/backup-db.sh                                    # 이제부터는 항상 먼저
docker compose -f docker-compose.dev.yml run --rm db-init
docker compose -f docker-compose.dev.yml restart admin-console
docker exec ai-infra-assistant-postgres-1 psql -U agent -d platform_config \
  -tAc "select count(*) from platform_settings;"             # 60 안팎으로 늘어납니다
```

`db-init`은 기존 행을 덮어쓰지 않고 없는 키만 넣습니다.

## 5. [웹] 콘솔 설정 확인

값이 mock으로 돌아가 있으면 다시 넣습니다.

| key | 값 |
|---|---|
| `vllm_llm_base_url` / `vllm_llm_model` | `http://75.23.32.41:8000/v1` / `qwen3-235b-a22b` |
| `vllm_embed_base_url` / `vllm_embed_model` | `http://75.23.32.41:8010/v1` / `bge-m3` |
| `rerank_provider` / `rerank_base_url` / `rerank_model` | `vllm` / `http://75.23.32.41:8020/v1` / `bge-reranker-v2-m3` |
| `execution_host` | `202.20.185.100` |
| `openwebui_public_url` | `http://202.20.183.30:8502` |
| `agent_api_key` | Open WebUI 연결(Connections)의 API 키와 같은 값 |

그리고 **`지시문을 최신 기본값으로 되돌리기`** → `agent-server 재시작`.
(이번에 지시문이 바뀌었습니다. 안 누르면 남의 계정 질문에 계속 가이드 문서를 안내합니다.)

## 6. [웹] Open WebUI 동작 확인

1. `S2 스케줄러 job list 확인해줘`
2. `내 홈 파일 리스트 보여줘`
3. `cocoa.song 계정이 어떤 gpu job 을 수행중이야?` → **한 줄 거절**이어야 합니다

> 본인(yr9.choi) 자원만 조회할 수 있어 cocoa.song의 job은 확인할 수 없습니다.

`ops_assistant`라는 말이나 "가이드 위치: 슈퍼컴 Portal > …"이 붙으면 5번 지시문 되돌리기를
안 한 것입니다.

## 7. 앞으로 — 반영 전에 항상 백업

```bash
cd /home/gpu1/yr9.choi/05_halo/AI-Infra-Assistant
bash scripts/backup-db.sh
```

`05_halo/` 밑에 떨어집니다(저장소 밖이라 rsync가 못 건드립니다). 14개까지 보관합니다.
되돌릴 때: `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-backup-<날짜>.sql`

---

## 복구 확인 후에 (며칠 뒤)

정상 동작이 확실해지면 남은 익명 볼륨을 정리합니다. **그 전에는 두세요.**

```bash
docker volume rm 553dd7066a559e45d37bb0d7d7d4b47fadeff60309477e7b9a8ebe0d6a769448
docker volume rm 1dc7527fd826d5a2afc08bd1b44e945219c2fd10da65c2747f49c2d367ab9198
```

그다음 밀린 작업: 엑셀로 커맨드 인자 다듬기(`{option}`을 **선택형**으로, 선택지는
`-j: JSON 형식으로 반환`처럼 `값: 설명`, 콜론 뒤 공백 필수) → `execution-mcp 재시작`.

## 문제가 계속될 때만

| 증상 | 조치 |
|---|---|
| 2번에서 `16`이 안 찍힘 | 복사 실패. `docker volume rm ai-infra-assistant_pg_data_dev` 후 다시 |
| 3번 볼륨이 `pg_data_dev`가 아님 | rsync가 안 된 것. compose에 `pg_data_dev`가 있는지 확인 |
| 3번 행 수가 전부 0 | 빈 볼륨이 붙은 것. 멈추고 알려주세요(원본 `$VOL`은 그대로 있습니다) |
| postgres가 안 뜸 | `docker compose -f docker-compose.dev.yml logs --tail=40 postgres` 를 보내주세요 |
| 최악의 경우 | 1번 덤프로 복구: `DROP_EXISTING=yes bash scripts/restore-db.sh ../pg-rescue-<날짜>.sql` |
