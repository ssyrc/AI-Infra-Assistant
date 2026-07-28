# 지금 할 일

현재 알려진 블로킹 이슈 없음. 아래는 필요할 때 참고할 커맨드.

## Open WebUI 관리자 계정 이메일 직접 변경 (DB 수정)

관리자 패널에서 본인 이메일은 못 바꾸므로(Open WebUI 자체 제약), DB를 직접 고친다:
```bash
# 1) 현재 이메일 확인
docker compose -f docker-compose.dev.yml exec open-webui \
  sqlite3 /app/backend/data/webui.db "SELECT id, name, email, role FROM user;"

# 2) 이메일 변경 (기존 이메일 자리에 지금 쓰는 값을, 새 이메일 자리에 바꿀 값을 넣는다)
docker compose -f docker-compose.dev.yml exec open-webui \
  sqlite3 /app/backend/data/webui.db \
  "UPDATE user SET email='새이메일@example.com' WHERE email='기존이메일@example.com';"
```
바꾼 뒤에는 로그아웃하고 **새 이메일**로 다시 로그인해야 함(비밀번호는 그대로).

완료된 내역/원인 분석은 `docs/RUN-LOG.md` 참고.
